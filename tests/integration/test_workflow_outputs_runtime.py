"""Generic workflow outputs through the OS API and CLI over real Core and Plus.

The graph is the authored command fixture: `work` pauses its plane at the
`review` gate. An operator-side exporter pins that gate through bounded views,
then publishes a report and opaque JSON. Nothing here is workflow-specific.
"""

import hashlib
import json
import os
from uuid import uuid4

import pytest

GATE = {"pause": {"reason": "review", "resume": "review"}}
OUTPUTS = "/api/v1/router/outputs"
UNAVAILABLE = ("record_sha256", "producer", "label", "published_at", "invocation")


def view(h, path, **params):
    response = h.http.get(
        "/api/v1/router/views/" + path, params={"home": "default", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def pinned_gate(h, instance, run):
    """The single `work` completion routed to the authored gate at this pause."""
    path = f"instances/{instance}/runs/{run}"
    summary = view(h, path)
    plane = summary["data"]["planes"]["main"]
    assert (plane["status"], plane["pause_reason"]) == ("paused", "review")
    events = [
        event
        for event in view(h, path, section="transitions")["items"]
        if event["event_kind"] == "transition"
        and event["node_id"] == "work"
        and event["version"] <= summary["state_version"]
        and json.loads(event["action"]) == GATE
    ]
    assert len(events) == 1, events
    return events[0]


def descriptor(views, instance, run, event, output_id, payload, **changes):
    def representation(format_id, data):
        digest = hashlib.sha256(views.canonical_json_bytes(data)).hexdigest()
        return {
            "format": format_id,
            "media_type": "application/json",
            "sha256": digest,
            "data": data,
        }

    report = {
        "title": "Answer check",
        "paragraphs": ["Produced by an authored command node."],
        "facts": [{"label": "Answer", "value": payload["answer"]}],
        "tables": [
            {
                "caption": "Checks",
                "columns": ["Name", "Value"],
                "rows": [["answer", payload["answer"]]],
            }
        ],
        "sources": [{"label": "Workspace file", "url": "answer.txt"}],
    }
    record = {
        "format": views.OUTPUT_FORMAT,
        "output_id": output_id,
        "instance_id": instance,
        "run_id": run,
        "plane_id": event["plane_id"],
        "node_id": event["node_id"],
        "invocation_id": event["completion_id"],
        "producing_revision_id": event["producing_revision_id"],
        "producer": {"id": "example.answer-exporter", "version": "1.0.0"},
        "label": "Answer check",
        "published_at": "2026-11-07T12:00:00Z",
        "representations": {
            "report": representation(views.REPORT_FORMAT, report),
            "json": representation("example.answer/v1", payload),
        },
    }
    record.update(changes)
    return record


def test_generic_outputs_publish_list_and_read_exactly_across_a_revision(
    router_harness, capsys
):
    from millrace.router.execution import RouterError
    from millrace_plus import workflow_views as views
    from millrace_plus.router import control

    from millrace_os.cli import main

    h = router_harness
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    h.settled(instance, run, "paused")
    event = pinned_gate(h, instance, run)
    payload = {"answer": (project / "answer.txt").read_text().strip(), "note": "café"}
    record = descriptor(views, instance, run, event, "answer-check", payload)
    saved = h.root / "answer-check.json"
    saved.write_bytes(views.canonical_json_bytes(record))
    digest = hashlib.sha256(saved.read_bytes()).hexdigest()
    connection = [
        "--home",
        "default",
        "--port",
        str(h.config.port),
        "--config-dir",
        str(h.config.config_dir),
        "--state-dir",
        str(h.config.state_dir),
    ]

    # The exporter publishes its saved descriptor through the public CLI.
    command = ["router", "outputs", "publish", "--file", str(saved), *connection]
    assert main(command) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["contract"] == "millrace-os-workflow-output-publication/v1"
    assert published["authority"] == "workflow-produced-evidence"
    assert published["status"] == "published"
    assert published["reference"] == {
        "instance_id": instance,
        "run_id": run,
        "output_id": "answer-check",
        "record_sha256": digest,
    }
    run_dir = h.home / "workflow-views/instances" / instance / "runs" / run
    stored = run_dir / "answer-check.json"
    assert stored.read_bytes() == saved.read_bytes()

    def publish(value):
        return h.http.post(OUTPUTS, json={"home": "default", "record": value})

    def refused(value, status, code):
        response = publish(value)
        assert (response.status_code, response.json()["code"]) == (status, code), (
            response.text
        )

    again = publish(record)
    assert (again.status_code, again.json()["status"]) == (200, "already_published")
    changed = descriptor(
        views, instance, run, event, "answer-check", payload, label="Changed"
    )
    refused(changed, 409, "output_conflict")
    wrong = descriptor(views, instance, run, event, "wrong", payload, node_id="review")
    refused(wrong, 409, "output_identity_mismatch")
    invalid = descriptor(views, instance, run, event, "Bad", payload)
    refused(invalid, 422, "invalid_output_record")
    elsewhere = uuid4().hex
    with pytest.raises(RouterError) as core:
        control.inspect_run_page(instance, elsewhere, state_home=h.home)
    stray = descriptor(views, instance, elsewhere, event, "elsewhere", payload)
    refused(stray, 409, "core_" + core.value.code)
    # Over the record limit but within the 1 MiB body policy.
    def padded(extra):
        data = {**payload, "pad": "x" * extra}
        return descriptor(views, instance, run, event, "too-large", data)

    room = views.RECORD_LIMIT - len(views.canonical_json_bytes(padded(0)))
    oversized = padded(room + 1)
    assert len(views.canonical_json_bytes(oversized)) == views.RECORD_LIMIT + 1
    refused(oversized, 413, "output_too_large")
    assert sorted(path.name for path in run_dir.iterdir()) == ["answer-check.json"]

    route = f"{OUTPUTS}/instances/{instance}/runs/{run}"

    def read(name, output_id="answer-check", record_sha256=digest):
        response = h.http.get(
            f"{route}/{output_id}",
            params={
                "home": "default",
                "record_sha256": record_sha256,
                "representation": name,
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["contract"] == "millrace-os-workflow-output/v1"
        assert result["authority"] == "workflow-produced-evidence"
        return {key: value for key, value in result.items() if key != "observed_at"}

    listing = h.http.get(route, params={"home": "default"})
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert page["contract"] == "millrace-os-workflow-outputs/v1"
    assert "authority" not in page
    (item,) = page["items"]
    assert (item["availability"], item["record_sha256"]) == ("available", digest)
    assert item["invocation"] == {
        "plane_id": "main",
        "node_id": "work",
        "invocation_id": event["completion_id"],
        "producing_revision_id": event["producing_revision_id"],
    }
    before = {name: read(name) for name in ("report", "json")}
    assert before["json"]["representation"]["data"] == payload
    assert before["report"]["representation"]["data"] == (
        record["representations"]["report"]["data"]
    )
    reading = [
        "router",
        "outputs",
        "read",
        "--instance",
        instance,
        "--run",
        run,
        "--output",
        "answer-check",
        "--record-sha256",
        digest,
        "--representation",
        "json",
        *connection,
    ]
    assert main(reading) == 0
    assert json.loads(capsys.readouterr().out)["representation"]["data"] == payload

    # A revision and later progress leave the exact record and its identity.
    path = f"instances/{instance}/runs/{run}"

    def version():
        return view(h, path)["state_version"]

    paused = control.pause_run(instance, run, state_home=h.home, request_id=uuid4().hex)
    assert paused["status"] == "applied", paused
    review = workflow / "review.py"
    review.write_text(review.read_text() + "# revised by the operator\n")
    submitted = control.submit_revision(
        instance,
        run,
        state_home=h.home,
        request_id=uuid4().hex,
        expected_state_version=version(),
    )
    assert submitted["status"] == "applied", submitted
    accepted = control.accept_revision(
        instance,
        run,
        candidate_id=str(submitted["candidate_id"]),
        state_home=h.home,
        request_id=uuid4().hex,
        expected_state_version=version(),
    )
    assert accepted["status"] == "applied", accepted
    resumed = control.resume_plane(
        instance,
        run,
        plane_id="main",
        state_home=h.home,
        request_id=uuid4().hex,
        expected_state_version=version(),
    )
    assert resumed["status"] == "applied", resumed
    h.settled(instance, run, "succeeded")
    summary = view(h, path)
    assert summary["data"]["active_revision_id"] == accepted["revision_id"]
    assert accepted["revision_id"] != event["producing_revision_id"]
    exact = view(h, path, section="invocations", invocation_id=event["completion_id"])
    assert exact["items"][0]["producing_revision_id"] == event["producing_revision_id"]
    assert {name: read(name) for name in ("report", "json")} == before
    assert stored.read_bytes() == saved.read_bytes()

    # Discovery pages by output ID; an invalid stored file keeps only identity.
    copy = descriptor(
        views, instance, run, event, "answer-copy", payload, label="Answer copy"
    )
    assert publish(copy).status_code == 200
    corrupt = run_dir / "corrupt.json"
    corrupt.write_bytes(b'{"format":')
    os.chmod(corrupt, 0o600)
    command = ["router", "outputs", "list", "--instance", instance, "--run", run]
    assert main([*command, "--limit", "2", *connection]) == 0
    first = json.loads(capsys.readouterr().out)
    assert [item["output_id"] for item in first["items"]] == [
        "answer-check",
        "answer-copy",
    ]
    assert first["next_cursor"]
    following = h.http.get(
        route, params={"home": "default", "limit": 2, "cursor": first["next_cursor"]}
    )
    assert following.status_code == 200, following.text
    assert following.json()["items"] == [
        {
            "output_id": "corrupt",
            "availability": "unavailable",
            "reason": "output_integrity_error",
            **dict.fromkeys((*UNAVAILABLE, "representations")),
        }
    ]
    assert following.json()["next_cursor"] is None
    broken = h.http.get(
        f"{route}/corrupt",
        params={"home": "default", "record_sha256": digest, "representation": "report"},
    )
    assert (broken.status_code, broken.json()["code"]) == (
        409,
        "output_integrity_error",
    )
    missing = h.http.get(
        f"{route}/absent",
        params={"home": "default", "record_sha256": digest, "representation": "report"},
    )
    assert (missing.status_code, missing.json()["code"]) == (404, "output_not_found")
