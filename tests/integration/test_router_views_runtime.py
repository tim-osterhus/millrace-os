"""Bounded views over real public Core/Plus controls; strict versus lifecycle reads."""

import json
import threading
from uuid import uuid4

import pytest

FORWARDS = (
    "list_instances_page",
    "inspect_instance_page",
    "inspect_run_page",
    "inspect_revision_graph",
    "validate_revision_summary",
)
RESPONSE_LIMIT = 1_049_600


@pytest.fixture
def bounded_reads(monkeypatch):
    """Record every call that reaches the bounded reader through the Plus facade."""
    from millrace_plus.router import control

    names = []
    for name in FORWARDS:

        def spy(*args, _name=name, _original=getattr(control, name), **kwargs):
            names.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(control, name, spy)
    return names


def view(h, path, **params):
    response = h.http.get(
        "/api/v1/router/views/" + path, params={"home": "default", **params}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract"] == "millrace-os-router-view/v1"
    assert body["home"] == "default" and body["observed_at"]
    assert (body["source_provider"], body["authority"]) == ("core", "core-state-home")
    assert len(response.content) <= RESPONSE_LIMIT
    return body["data"]


def strict(h, request_id, instance, run=None, **headers):
    params = {"home": "default", "instance_id": instance}
    if run is not None:
        params["run_id"] = run
    return h.http.get(
        "/api/v1/router/views/requests/" + request_id, params=params, headers=headers
    )


def too_large(response):
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "core_inspection_too_large"
    return True


def test_views_return_the_plus_facade_results_unchanged(router_harness, monkeypatch):
    from millrace_plus.router import control

    h = router_harness
    workflow, project = h.flow()
    instance = h.create(workflow, project)
    start = h.request(
        "start_run",
        instance,
        version=h.read(instance)["state_version"],
        payload={"edit_mode": "locked"},
    )
    receipt = h.send(start)
    run = receipt["run_id"]
    legacy = h.settled(instance, run, "succeeded")
    state = {"state_home": h.home}
    homes = h.http.get("/api/v1/router/homes")
    assert homes.status_code == 200, homes.text
    assert homes.json()["items"] == [{"name": "default"}]
    assert homes.json()["next_cursor"] is None
    path = f"instances/{instance}/runs/{run}"
    assert view(h, "instances") == control.list_instances_page(**state)
    assert view(h, f"instances/{instance}") == control.inspect_instance_page(
        instance, **state
    )
    summary = view(h, path)
    assert summary == control.inspect_run_page(instance, run, **state)
    assert summary["kind"] == "run-summary"
    for section in ("invocations", "transitions", "controls"):
        assert view(h, path, section=section) == control.inspect_run_page(
            instance, run, section=section, **state
        )
    first = view(h, path, section="invocations", limit=1)
    assert first == control.inspect_run_page(
        instance, run, section="invocations", limit=1, **state
    )
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = view(h, path, section="invocations", limit=1, cursor=first["next_cursor"])
    assert second == control.inspect_run_page(
        instance,
        run,
        section="invocations",
        limit=1,
        cursor=first["next_cursor"],
        **state,
    )
    assert second["next_cursor"] is None
    ids = {item["invocation_id"] for item in first["items"] + second["items"]}
    assert ids == {item["id"] for item in legacy["invocations"]}
    exact_id = second["items"][0]["invocation_id"]
    exact = view(h, path, section="invocations", invocation_id=exact_id)
    assert exact["items"] == second["items"] and exact["next_cursor"] is None
    # Instance-targeted Start is found from its generated run and the instance.
    by_run = view(h, path, section="controls", request_id=start["request_id"])
    assert by_run["items"] == [receipt]
    revision = view(h, f"{path}/revisions/{legacy['revision_id']}")
    assert revision == control.inspect_revision_graph(
        instance, run, legacy["revision_id"], **state
    )
    for body, run_id in (({"run_id": run}, run), ({}, None)):
        response = h.http.post(
            "/api/v1/router/views/validate",
            json={"home": "default", "instance_id": instance, **body},
        )
        assert response.status_code == 200, response.text
        assert response.json()["contract"] == "millrace-os-router-view/v1"
        assert response.json()["data"] == control.validate_revision_summary(
            instance, run_id=run_id, **state
        )

    # Strict observation never uses the legacy full inspection, even as fallback.
    def forbidden(*args, **kwargs):
        raise AssertionError("strict observation used a legacy full inspection")

    monkeypatch.setattr(h.service.router_client, "inspect_run", forbidden)
    monkeypatch.setattr(h.service.router_client, "inspect_instance", forbidden)
    for target in ((instance,), (instance, run)):
        response = strict(h, start["request_id"], *target)
        assert response.status_code == 200, response.text
        assert response.json()["core_receipt"] == receipt
        assert response.json()["contract"] == "millrace-os-router-call/v1"
    v2 = strict(
        h,
        start["request_id"],
        instance,
        Accept="application/vnd.millrace.router-call.v2+json",
    )
    assert v2.json()["contract"] == "millrace-os-router-call/v2"
    assert v2.json()["core_receipt"] == receipt
    missing = strict(h, uuid4().hex, instance)
    assert missing.status_code == 404
    assert missing.json()["code"] == "observation_unavailable"


def test_valid_over_budget_instance_keeps_lifecycle_while_strict_views_refuse(
    router_harness, bounded_reads, monkeypatch
):
    import millrace.router.inspection as inspection

    h = router_harness
    workflow, project = h.flow(wait=True)
    instance = h.create(workflow, project)
    # A small valid instance exceeds every lowered view budget.
    monkeypatch.setattr(inspection, "INSTANCE_ROWS", 1)
    expected = []
    too_large(
        h.http.get(f"/api/v1/router/views/instances/{instance}?home=default")
    )
    expected.append("inspect_instance_page")

    # Start, receipt observation and the default watcher use lifecycle reads.
    start = h.request(
        "start_run",
        instance,
        version=h.read(instance)["state_version"],
        payload={"edit_mode": "locked"},
    )
    started = h.send(start)
    assert started["status"] == "applied"
    run = started["run_id"]
    h.until(lambda: (project / "started").exists())
    too_large(strict(h, start["request_id"], instance))
    expected.append("inspect_run_page")
    lifecycle = h.observe(start["request_id"], instance_id=instance, run_id=run)
    assert lifecycle.status_code == 200, lifecycle.text
    assert lifecycle.json()["core_receipt"] == started

    # A stop returns queued while Start holds the controller; the default
    # queued refresh still observes its later application.
    pause = h.request("pause_run", instance, run)
    call = h.service.router_workers.submit("default", pause)
    h.until(lambda: call.receipt and call.receipt["status"] == "queued")
    too_large(strict(h, pause["request_id"], instance, run))
    expected.append("inspect_run_page")
    queued = h.observe(pause["request_id"], instance_id=instance, run_id=run)
    assert queued.status_code in (200, 202), queued.text
    (project / "release").write_text("release the waiting invocation")

    def applied():
        response = h.observe(pause["request_id"], instance_id=instance, run_id=run)
        receipt = response.json()["core_receipt"]
        return receipt if receipt and receipt["status"] == "applied" else None

    h.until(applied)
    assert h.settled(instance, run, "paused")["count"] == 1
    # Joined calls released their slot; the same group admits another
    # foreground call.
    assert all(row["joined"] for row in h.service.router_workers.snapshot())
    resumed = h.send(
        h.request(
            "resume_run", instance, run, version=h.read(instance, run)["state_version"]
        )
    )
    assert resumed["status"] == "applied"
    assert h.settled(instance, run, "succeeded")["count"] == 2

    # Drain cancels a second waiting run through lifecycle receipt checks.
    (project / "release").unlink()
    (project / "started").unlink()
    second = h.start_run(instance)["run_id"]
    h.until(lambda: (project / "started").exists())
    h.service.request_shutdown(cancel=True)
    h.service.stop()
    assert all(row["joined"] for row in h.service.router_workers.snapshot())
    h.http.close()
    h.start()
    assert h.settled(instance, second, "cancelled")["status"] == "cancelled"
    # After restart only Core receipts remain: lifecycle reads them, strict
    # views still refuse with the typed budget error.
    retired = h.observe(start["request_id"], instance_id=instance, run_id=run)
    assert retired.status_code == 200, retired.text
    assert retired.json()["source"] == "core_receipt"
    assert retired.json()["core_receipt"] == started
    too_large(strict(h, start["request_id"], instance, run))
    expected.append("inspect_run_page")
    assert bounded_reads == expected


def test_over_budget_draft_apply_and_repair_use_lifecycle_receipts(
    router_harness, bounded_reads, workflow_files, monkeypatch
):
    import millrace.router.inspection as inspection

    import millrace_os.workflow_drafts as drafts

    h = router_harness
    workflow, _ = h.flow(pause=True)
    instance = h.create(workflow, h.projects[-1])
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    monkeypatch.setattr(inspection, "INSTANCE_ROWS", 1)
    files = workflow_files(pause=True)
    graph = json.loads(files["workflow.json"])
    graph["planes"]["main"]["nodes"]["review"]["invoke"]["timeout_seconds"] = 9
    files["workflow.json"] = json.dumps(graph)
    created = h.post("drafts", {"home": "default", "files": h.entries(files)})
    original = drafts._json

    def interrupt(path, record):
        receipts = record.get("receipts", [])
        if (
            path.name == "completed.json"
            and receipts
            and receipts[-1]["operation"] == "submit_revision"
        ):
            raise OSError("owned fault after actual control before durable record")
        return original(path, record)

    monkeypatch.setattr(drafts, "_json", interrupt)
    h.service.router_workers.observation_timeout = 2
    request_id = uuid4().hex
    failed = h.http.post(
        f"/api/v1/router/drafts/{created['draft_id']}/apply",
        json={
            "home": "default",
            "instance_id": instance,
            "run_id": run,
            "base_state_version": before["state_version"],
            "request_id": request_id,
        },
    )
    assert failed.status_code == 503, failed.text
    monkeypatch.setattr(drafts, "_json", original)
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    too_large(strict(h, request_id, instance, run))
    h.stop()
    h.start()
    h.service.router_workers.observation_timeout = 2
    repaired = h.http.post(
        "/api/v1/router/repair-publication",
        json={"home": "default", "instance_id": instance},
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"]["status"] == "paused_pending"
    marker = h.home / "os-client/publications" / instance / "in-progress.json"
    assert not marker.exists()
    assert bounded_reads == ["inspect_run_page"]


def test_queued_start_strict_observation_keeps_its_exact_association(
    router_harness, bounded_reads
):
    from millrace_plus.router import control

    h = router_harness
    workflow, project = h.flow(wait=True)
    instance = h.create(workflow, project)
    entered, release = threading.Event(), threading.Event()
    held, errors = {}, []

    def started(ids):
        held.update(ids)
        entered.set()
        assert release.wait(10)

    def hold():
        try:
            control.execute_request(
                h.request(
                    "start_run", instance, version=1, payload={"edit_mode": "locked"}
                ),
                state_home=h.home,
                on_started=started,
            )
        except BaseException as exc:
            errors.append(getattr(exc, "code", type(exc).__name__))

    holder = threading.Thread(target=hold, daemon=False)
    holder.start()
    request = h.request(
        "start_run", instance, version=1, payload={"edit_mode": "locked"}
    )
    try:
        assert entered.wait(3)
        h.send(request, wait=False)
        call = h.service.router_workers.submit("default", request)
        h.until(lambda: call.receipt and call.receipt["status"] == "queued")
        queued = strict(h, request["request_id"], instance)
        assert queued.status_code == 202, queued.text
        body = queued.json()
        assert body["source"] == "os_owned_call"
        assert body["core_receipt"]["status"] == "queued"
        assert body["core_receipt"]["request_digest"] == call.digest
        assert body["run_id"] is None
        # The holder's run did not record this request as its Start.
        other = strict(h, request["request_id"], instance, held["run_id"])
        assert other.status_code == 409, other.text
        assert other.json()["code"] == "core_request_target_mismatch"
    finally:
        release.set()
        (project / "release").write_text("queued Start fixture cleanup")
        holder.join(timeout=10)
    assert not holder.is_alive() and errors == []

    def final():
        response = strict(h, request["request_id"], instance)
        receipt = response.json()["core_receipt"]
        return receipt if receipt and receipt["status"] != "queued" else None

    resolved = h.until(final)
    lifecycle = h.observe(request["request_id"], instance_id=instance)
    assert lifecycle.json()["core_receipt"] == resolved
    assert set(bounded_reads) == {"inspect_run_page"}
    h.settled(instance, held["run_id"], "succeeded")
