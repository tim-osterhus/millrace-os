"""Manual authoring/publication uses real public command controls and frozen files."""

import base64
import json
from uuid import uuid4

import pytest


def draft(h, files):
    result = h.post("drafts", {"home": "default", "files": h.entries(files)})
    assert result["contract"] == "millrace-os-draft/v1"
    assert result["home"] == "default" and len(result["sha256"]) == 64
    return result["draft_id"]


def apply(h, identity, instance, run, version):
    h.service.router_workers.observation_timeout = 2
    return h.http.post(
        f"/api/v1/router/drafts/{identity}/apply",
        json={
            "home": "default",
            "instance_id": instance,
            "run_id": run,
            "base_state_version": version,
            "request_id": uuid4().hex,
        },
    )


def expanded(files):
    result = dict(files)
    graph = json.loads(result["workflow.json"])
    nodes = graph["planes"]["main"]["nodes"]
    nodes["review"]["on"]["ACCEPT"] = {"goto": "archive"}
    nodes["archive"] = {
        **nodes["review"],
        "invoke": {**nodes["review"]["invoke"], "config": {"script": "archive.py"}},
        "on": {"ACCEPT": {"finish": "succeeded"}},
    }
    result["archive.py"] = (
        "from pathlib import Path\n"
        "Path('archive.txt').write_text('archived 42\\n')\n"
        'print(\'{"outcome":"ACCEPT"}\')\n'
    )
    graph["control_files"].append("archive.py")
    result["workflow.json"] = json.dumps(graph)
    return result


def test_pause_prevalidate_publish_submit_leaves_explicit_continuation(
    router_harness, workflow_files, monkeypatch
):
    import millrace_os.workflow_drafts as writes

    h = router_harness
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    files = expanded(workflow_files(pause=True))
    identity = draft(h, files)
    validation = h.post(
        f"drafts/{identity}/validate",
        {"home": "default", "instance_id": instance, "run_id": run},
    )
    assert validation["data"]["valid"]
    assert (
        validation["data"]["validation_context"]["run_state_version"]
        == before["state_version"]
    )
    assert h.read(instance, run) == before
    watched = []
    original = writes._write

    def record(path, raw):
        if path.is_relative_to(workflow):
            current = h.service.router_client.inspect_run("default", instance, run)
            assert current["quiescent"] and not current["inflight_invocations"]
            assert all(
                row["writer_state"] == "stopped" for row in current["invocations"]
            )
            watched.append(path.relative_to(workflow).as_posix())
        return original(path, raw)

    monkeypatch.setattr(writes, "_write", record)
    response = apply(h, identity, instance, run, before["state_version"])
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "paused_pending"
    assert sorted(watched) == sorted(files)
    for name, content in files.items():
        assert (workflow / name).read_text() == content
    pending = h.read(instance, run)
    assert (
        pending["status"] == "paused"
        and pending["run_gate"]["reason"] == "operator_pause"
    )
    assert pending["count"] == 1 and pending["pending_revision"] is not None
    assert pending["pending_revision"]["candidate_id"] == result["candidate_id"]
    assert not (project / "checked.txt").exists()
    assert result["receipts"][-1]["operation"] == "submit_revision"
    accepted = h.send(
        h.request(
            "accept_revision",
            instance,
            run,
            version=pending["state_version"],
            payload={"candidate_id": result["candidate_id"]},
        )
    )
    assert accepted["status"] == "applied"
    # A final receipt can precede return/join of its foreground call.
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    held = h.read(instance, run)
    assert held["status"] == "paused" and held["count"] == 1
    resumed = h.send(
        h.request("resume_run", instance, run, version=held["state_version"])
    )
    assert resumed["status"] == "refused" and resumed["code"] == "run_not_paused"
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    assert held["run_gate"] is None
    # A graph pause is plane-local and survives the explicit run-gate resume.
    held = h.read(instance, run)
    assert held["planes"]["main"]["status"] == "paused"
    h.send(
        h.request(
            "resume_plane", instance, run, plane="main", version=held["state_version"]
        )
    )
    final = h.settled(instance, run, "succeeded")
    assert final["count"] == 3
    assert (project / "archive.txt").read_text() == "archived 42\n"
    assert (
        final["invocations"][0]["revision_id"]
        == before["invocations"][0]["revision_id"]
    )


def test_invalid_or_stale_draft_never_writes_watched_files(
    router_harness, workflow_files, monkeypatch
):
    import millrace_os.workflow_drafts as writes

    h = router_harness
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    original = {p.name: p.read_bytes() for p in workflow.iterdir()}
    invalid = workflow_files(pause=True)
    invalid["workflow.json"] = '{"format":"millrace-router/v3","planes":{}}'
    identity = draft(h, invalid)
    watched = []
    write = writes._write

    def record(path, raw):
        if path.is_relative_to(workflow):
            watched.append(path)
        return write(path, raw)

    monkeypatch.setattr(writes, "_write", record)
    stale = apply(h, identity, instance, run, before["state_version"] - 1)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_state_version"
    result = apply(h, identity, instance, run, before["state_version"])
    assert (
        result.status_code == 409
        and result.json()["code"] == "publication_validation_failed"
    )
    assert watched == []
    assert {p.name: p.read_bytes() for p in workflow.iterdir()} == original
    assert h.read(instance, run)["count"] == 1
    assert not (project / "checked.txt").exists()


@pytest.mark.parametrize(
    "terminal_cancel", [False, True], ids=["paused-repair", "terminal-cancel-repair"]
)
def test_partial_publication_restart_and_cancelled_terminal_repair(
    router_harness, workflow_files, monkeypatch, terminal_cancel
):
    import millrace_os.workflow_drafts as writes

    h = router_harness
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    files = expanded(workflow_files(pause=True))
    identity = draft(h, files)
    original = writes._write
    count = 0

    def interrupt(path, raw):
        nonlocal count
        if path.is_relative_to(workflow):
            count += 1
            if count == 2:
                raise OSError("test-owned publication interruption")
        return original(path, raw)

    monkeypatch.setattr(writes, "_write", interrupt)
    interrupted = apply(h, identity, instance, run, before["state_version"])
    assert interrupted.status_code == 503, interrupted.text
    assert count == 2
    monkeypatch.setattr(writes, "_write", original)
    # Subsequent mutable authoring cannot change the recorded frozen repair.
    changed = {**files, "archive.py": "raise RuntimeError('mutable newer draft')\n"}
    response = h.http.put(
        f"/api/v1/router/drafts/{identity}/files",
        json={"home": "default", "files": h.entries(changed)},
    )
    assert response.status_code == 200
    h.stop()
    h.start()
    h.service.router_workers.observation_timeout = 2
    refused = h.http.post(
        "/api/v1/router/requests",
        json={
            "home": "default",
            "request": h.request(
                "resume_run",
                instance,
                run,
                version=h.read(instance, run)["state_version"],
            ),
        },
    )
    assert (
        refused.status_code == 409
        and refused.json()["code"] == "publication_incomplete"
    )
    if terminal_cancel:
        cancelled = h.send(h.request("cancel_run", instance, run))
        assert cancelled["status"] == "applied"
        terminal = h.settled(instance, run, "cancelled")
    response = h.http.post(
        "/api/v1/router/repair-publication",
        json={"home": "default", "instance_id": instance},
    )
    assert response.status_code == 200, response.text
    repaired = response.json()["data"]
    for name, content in files.items():
        assert (workflow / name).read_text() == content
    repaired_run = h.read(instance, run)
    if terminal_cancel:
        assert repaired["status"] == "terminal_files_ready"
        assert repaired["terminal_evidence"]["status"] == "cancelled"
        assert all(r["operation"] != "submit_revision" for r in repaired["receipts"])
        # Live advisory draft changes; durable terminal history remains exact.
        assert {k: v for k, v in repaired_run.items() if k != "draft_revision"} == {
            k: v for k, v in terminal.items() if k != "draft_revision"
        }
        fresh = h.start_run(instance)
        assert fresh["run_id"] != run
        assert h.settled(instance, fresh["run_id"], "paused")["count"] == 1
    else:
        assert repaired["status"] == "paused_pending"
        assert repaired_run["status"] == "paused" and repaired_run["quiescent"]
        assert (
            repaired_run["pending_revision"]["candidate_id"] == repaired["candidate_id"]
        )
        assert repaired_run["count"] == 1
        assert repaired_run["invocations"] == before["invocations"]
        assert repaired["receipts"][-1]["operation"] == "submit_revision"
    assert not (project / "archive.txt").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "a/../b",
        "a//b",
        "a\\b",
        ".MILLRACE-ROUTER-OWNER",
        "sub/.millrace-router-owner",
    ],
)
def test_draft_path_refusals(router_harness, path):
    h = router_harness
    response = h.http.post(
        "/api/v1/router/drafts",
        json={"home": "default", "files": [{"path": path, "content_base64": "eA=="}]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_draft_path"
    assert not (h.root / "escape").exists()


def test_draft_path_and_streamed_body_boundaries(router_harness):
    h = router_harness
    duplicate = h.http.post(
        "/api/v1/router/drafts",
        json={"home": "default", "files": h.entries({"A": "one", "a": "two"})},
    )
    assert (
        duplicate.status_code == 422
        and duplicate.json()["code"] == "duplicate_draft_path"
    )
    invalid = h.http.post(
        "/api/v1/router/drafts",
        json={
            "home": "default",
            "files": [{"path": "work.py", "content_base64": "%%%"}],
        },
    )
    assert (
        invalid.status_code == 422
        and invalid.json()["code"] == "invalid_draft_encoding"
    )
    identity = draft(h, {})
    uploaded = h.http.put(
        f"/api/v1/router/drafts/{identity}/files",
        json={"home": "default", "files": h.entries({"large.txt": "x" * 6000})},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert (
        len(
            h.http.get(f"/api/v1/router/drafts/{identity}?home=default").json()[
                "files"
            ][0]["content_base64"]
        )
        > 4096
    )
    ordinary = h.http.post(
        "/api/v1/router/validate", json={"home": "default", "instance_id": "x" * 5000}
    )
    assert ordinary.status_code == 413
    encoded = json.dumps(
        {
            "home": "default",
            "files": [
                {
                    "path": "large.txt",
                    "content_base64": base64.b64encode(b"x" * 800000).decode(),
                }
            ],
        }
    ).encode()
    assert len(encoded) > 1024 * 1024
    oversized = h.http.put(
        f"/api/v1/router/drafts/{identity}/files",
        content=iter([encoded[:500000], encoded[500000:]]),
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    unchanged = h.http.get(f"/api/v1/router/drafts/{identity}?home=default").json()
    assert base64.b64decode(unchanged["files"][0]["content_base64"]) == b"x" * 6000
    root = h.config.state_dir / "drafts" / identity / "files"
    outside = h.root / "outside.txt"
    outside.write_text("untouched")
    (root / "link").symlink_to(outside)
    rejected = h.http.put(
        f"/api/v1/router/drafts/{identity}/file",
        json={"home": "default", "path": "link", "content_base64": "eA=="},
    )
    assert rejected.status_code in (403, 409, 422)
    assert outside.read_text() == "untouched"


def test_publication_serializes_other_services_and_late_admission(
    router_factory, workflow_files, tmp_path
):
    import threading

    from millrace_os.router_client import RouterClient

    class PrevalidationBarrier(RouterClient):
        reached = threading.Event()
        release = threading.Event()

        def validate_revision(
            self, home, instance_id, *, run_id=None, workflow_file=None
        ):
            value = super().validate_revision(
                home, instance_id, run_id=run_id, workflow_file=workflow_file
            )
            if workflow_file is not None and "frozen" in workflow_file.parts:
                self.reached.set()
                assert self.release.wait(5)
            return value

    homes = {"default": tmp_path / "shared-core"}
    client = PrevalidationBarrier(homes)
    first = router_factory("publisher", homes=homes, router_client=client)
    second = router_factory("observer", homes=homes)
    workflow, project = first.flow(pause=True)
    instance = first.create(workflow, project)
    run = first.start_run(instance)["run_id"]
    before = first.settled(instance, run, "paused")
    identity = draft(first, expanded(workflow_files(pause=True)))
    result = []

    def publish():
        result.append(apply(first, identity, instance, run, before["state_version"]))

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert client.reached.wait(3)
        state = second.read(instance, run)
        assert state["count"] == 1 and state["quiescent"]
        blocked = second.http.post(
            "/api/v1/router/requests",
            json={
                "home": "default",
                "request": second.request(
                    "resume_run", instance, run, version=state["state_version"]
                ),
            },
        )
        assert (
            blocked.status_code == 429 and blocked.json()["code"] == "publication_busy"
        )
        # Cancellation bypasses the publication lock. Its new version invalidates the
        # frozen prevalidation, so this Apply cannot silently refresh and write.
        pause = second.send(second.request("cancel_run", instance, run))
        assert pause["status"] == "applied"
    finally:
        client.release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert result[0].status_code == 409, result[0].text
    assert result[0].json()["code"] == "stale_state_version"
    assert not (workflow / "archive.py").exists()
    assert not (project / "checked.txt").exists()


@pytest.mark.parametrize(
    "damage",
    ["bitflip", "missing"],
    ids=["bitflipped-frozen-file", "missing-frozen-file"],
)
def test_corrupt_frozen_publication_refuses_before_watched_write(
    router_harness, workflow_files, monkeypatch, damage
):
    import millrace_os.workflow_drafts as writes

    h = router_harness
    workflow, _ = h.flow(pause=True)
    instance = h.create(workflow, h.projects[-1])
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    identity = draft(h, expanded(workflow_files(pause=True)))
    write = writes._write

    def fail_first(path, raw):
        if path.is_relative_to(workflow):
            raise OSError("owned interruption before any watched write")
        return write(path, raw)

    monkeypatch.setattr(writes, "_write", fail_first)
    failed = apply(h, identity, instance, run, before["state_version"])
    assert failed.status_code == 503
    monkeypatch.setattr(writes, "_write", write)
    watched_before = {p.name: p.read_bytes() for p in workflow.iterdir()}
    h.stop()
    marker = h.home / "os-client/publications" / instance / "in-progress.json"
    record = json.loads(marker.read_bytes())
    frozen = (
        h.config.state_dir
        / "drafts"
        / identity
        / "frozen"
        / record["frozen_token"]
        / "work.py"
    )
    if damage == "missing":
        frozen.unlink()
    else:
        frozen.write_bytes(frozen.read_bytes() + b"\n# owned bit flip\n")
    h.start()
    response = h.http.post(
        "/api/v1/router/repair-publication",
        json={"home": "default", "instance_id": instance},
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "publication_evidence_invalid"
    )
    assert {p.name: p.read_bytes() for p in workflow.iterdir()} == watched_before
    current = h.read(instance, run)
    assert current["status"] == "paused" and current["count"] == 1
    assert current["invocations"] == before["invocations"]
    assert marker.exists()
    blocked = h.http.post(
        "/api/v1/router/requests",
        json={
            "home": "default",
            "request": h.request(
                "resume_run", instance, run, version=current["state_version"]
            ),
        },
    )
    assert (
        blocked.status_code == 409
        and blocked.json()["code"] == "publication_incomplete"
    )


@pytest.mark.parametrize(
    "window",
    ["completion", "submit-receipt", "pause-receipt"],
    ids=["completion-record", "submit-receipt-record", "pause-receipt-record"],
)
def test_repair_recovers_exact_applied_submit_after_completion_record_fault(
    router_harness, workflow_files, monkeypatch, window
):
    import millrace_os.workflow_drafts as drafts

    h = router_harness
    workflow, _ = h.flow(pause=True)
    instance = h.create(workflow, h.projects[-1])
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    files = workflow_files(pause=True)
    graph = json.loads(files["workflow.json"])
    graph["planes"]["main"]["nodes"]["review"]["invoke"]["timeout_seconds"] = 9
    files["workflow.json"] = json.dumps(graph)
    identity = draft(h, files)
    original = drafts._json
    captured = []
    operation = "pause_run" if window == "pause-receipt" else "submit_revision"

    def interrupt(path, record):
        receipts = record.get("receipts", [])
        desired = "completed.json" if window == "completion" else "in-progress.json"
        if path.name == desired and receipts and receipts[-1]["operation"] == operation:
            assert receipts[-1]["status"] == "applied"
            captured.append(dict(receipts[-1]))
            raise OSError("owned fault after actual control before durable record")
        return original(path, record)

    monkeypatch.setattr(drafts, "_json", interrupt)
    h.service.router_workers.observation_timeout = 2
    request_id = uuid4().hex
    failed = h.http.post(
        f"/api/v1/router/drafts/{identity}/apply",
        json={
            "home": "default",
            "instance_id": instance,
            "run_id": run,
            "base_state_version": before["state_version"],
            "request_id": request_id,
        },
    )
    assert failed.status_code == 503, failed.text
    assert len(captured) == 1
    applied = captured[0]
    pending = h.read(instance, run)
    assert (
        next(r for r in pending["controls"] if r["request_id"] == applied["request_id"])
        == applied
    )
    assert pending["status"] == "paused"
    if operation == "submit_revision":
        assert pending["pending_revision"] is not None
        assert applied["request_id"] == request_id
    else:
        assert pending["pending_revision"] is None
    marker = h.home / "os-client/publications" / instance / "in-progress.json"
    assert marker.exists()
    monkeypatch.setattr(drafts, "_json", original)
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    h.stop()
    h.start()
    h.service.router_workers.observation_timeout = 2
    repaired = h.http.post(
        "/api/v1/router/repair-publication",
        json={"home": "default", "instance_id": instance},
    )
    assert repaired.status_code == 200, repaired.text
    result = repaired.json()["data"]
    assert result["status"] == "paused_pending"
    assert (
        next(r for r in result["receipts"] if r["request_id"] == applied["request_id"])
        == applied
    )
    after = h.read(instance, run)
    assert (
        next(r for r in after["controls"] if r["request_id"] == applied["request_id"])
        == applied
    )
    # Repair observes the exact already-applied step, rather than issuing a new
    # pause or rebuilding the same submit identity with a fresh version/body.
    assert [r for r in after["controls"] if r["operation"] == "pause_run"] == [
        r for r in pending["controls"] if r["operation"] == "pause_run"
    ]
    if operation == "submit_revision":
        assert after["controls"] == pending["controls"]
        assert (
            after["pending_revision"]["candidate_id"]
            == pending["pending_revision"]["candidate_id"]
        )
    else:
        assert (
            next(r for r in after["controls"] if r["request_id"] == request_id)[
                "status"
            ]
            == "applied"
        )
    assert after["invocations"] == pending["invocations"]
    assert after["count"] == 1 and after["status"] == "paused"
    assert not marker.exists()
    for name, content in files.items():
        assert (workflow / name).read_text() == content


def test_malformed_apply_request_id_rejects_before_any_publication_or_core_effect(
    router_harness, workflow_files
):
    h = router_harness
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    assert before["run_gate"] is None
    files = workflow_files(pause=True)
    graph = json.loads(files["workflow.json"])
    graph["planes"]["main"]["nodes"]["review"]["invoke"]["timeout_seconds"] = 9
    files["workflow.json"] = json.dumps(graph)
    identity = draft(h, files)
    watched = {p.name: p.read_bytes() for p in workflow.iterdir()}
    publication_root = h.home / "os-client/publications" / instance
    publication_before = {
        p.relative_to(publication_root).as_posix(): p.read_bytes()
        for p in publication_root.rglob("*")
        if p.is_file()
    }
    response = h.http.post(
        f"/api/v1/router/drafts/{identity}/apply",
        json={
            "home": "default",
            "instance_id": instance,
            "run_id": run,
            "base_state_version": before["state_version"],
            "request_id": "edit-1",
        },
    )
    after = h.read(instance, run)
    assert response.status_code == 422, response.text
    assert after == before
    assert {p.name: p.read_bytes() for p in workflow.iterdir()} == watched
    assert {
        p.relative_to(publication_root).as_posix(): p.read_bytes()
        for p in publication_root.rglob("*")
        if p.is_file()
    } == publication_before
    assert not (h.config.state_dir / "drafts" / identity / "frozen").exists()
    assert not any(
        row["observation"]["request_id"] == "edit-1"
        for row in h.service.router_workers.snapshot()
    )
    # The ordinary exact32hex path still reaches real public pause/submit.
    h.service.router_workers.observation_timeout = 2
    accepted = apply(h, identity, instance, run, before["state_version"])
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["status"] == "paused_pending"
    assert h.read(instance, run)["pending_revision"] is not None


def test_incomplete_apply_allows_public_writer_reconciliation_then_paused_repair(
    router_harness,
):
    import dataclasses
    import hashlib
    import os
    import signal
    import subprocess
    import sys
    from pathlib import Path

    import httpx

    from millrace_os.credentials import CredentialStore

    h = router_harness
    workflow, project = h.flow(wait=True)
    work = workflow / "work.py"
    work.write_text(
        "import os\nfrom pathlib import Path\n"
        "Path('writer.pid').write_text(str(os.getpid()))\n" + work.read_text()
    )
    instance = h.create(workflow, project)
    h.stop()
    (project / "release").unlink()
    launch = """
import sys,time
from pathlib import Path
from millrace_os.config import ApplicationConfig
from millrace_os.service import Service
root,port=sys.argv[1:]
root=Path(root)
s=Service(ApplicationConfig(config_dir=root/'config',state_dir=root/'os-state',credential_path=root/'config/credentials.json',port=int(port),router_state_homes={'default':root/'core'}))
s.start()
(root/'child-ready').write_text(str(__import__('os').getpid()))
while True:time.sleep(.05)
"""
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }
    child = subprocess.Popen(
        [sys.executable, "-c", launch, str(h.root), str(h.config.port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    h.http = httpx.Client(
        base_url=h.config.browser_endpoint,
        headers={
            "Authorization": "Bearer "
            + CredentialStore(h.config.credential_path).ensure()
        },
        timeout=3,
        trust_env=False,
    )
    writer_pid = None
    try:
        h.until(lambda: (h.root / "child-ready").exists())
        assert int((h.root / "child-ready").read_text()) == child.pid
        start = h.start_run(instance)
        run = start["run_id"]
        h.until(lambda: (project / "writer.pid").exists())
        writer_pid = int((project / "writer.pid").read_text())
        assert writer_pid != child.pid and child.poll() is None
        before_kill = h.read(instance, run)
        assert before_kill["inflight_invocations"] and not before_kill["quiescent"]
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=3) == -signal.SIGKILL
        h.http.close()
        h.start()
        unavailable = h.http.get(
            f"/api/v1/router/instances/{instance}/runs/{run}?home=default"
        )
        assert (
            unavailable.status_code == 503
            and unavailable.json()["code"] == "storage_unavailable"
        )
        old_dir = h.config.state_dir
        old_state = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in old_dir.iterdir()
            if p.is_file()
        }
        assert any(n.endswith("-wal") for n in old_state)
        h.service.stop()
        h.http.close()
        h.config = dataclasses.replace(h.config, state_dir=h.root / "fresh-os-state")
        h.start()
        assert {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in old_dir.iterdir()
            if p.is_file()
        } == old_state
        retained = h.read(instance, run)
        recovery = h.send(
            h.request("recover_run", instance, run, version=retained["state_version"])
        )
        assert (
            recovery["status"] == "refused"
            and recovery["code"] == "stale_state_version"
        )
        unknown = h.read(instance, run)
        assert (
            unknown["status"] == "paused"
            and not unknown["quiescent"]
            and not unknown["inflight_invocations"]
        )
        assert unknown["invocations"][0]["writer_state"] in {"unknown", "may_live"}
        # Release only this owned writer and prove its exact PID absent before
        # making the operator's supported stopped reconciliation assertion.
        (project / "release").write_text("owned command may exit")

        def writer_absent():
            try:
                os.kill(writer_pid, 0)
            except ProcessLookupError:
                return True
            return False

        h.until(writer_absent, timeout=5)
        assert writer_absent()
        state_before_apply = h.read(instance, run)
        files = {p.name: p.read_text() for p in workflow.iterdir() if p.is_file()}
        files["review.py"] += "\n# Explicit recoverable draft edit.\n"
        draft = h.post("drafts", {"home": "default", "files": h.entries(files)})[
            "draft_id"
        ]
        h.service.router_workers.observation_timeout = 2
        apply = h.http.post(
            f"/api/v1/router/drafts/{draft}/apply",
            json={
                "home": "default",
                "instance_id": instance,
                "run_id": run,
                "base_state_version": state_before_apply["state_version"],
                "request_id": "a" * 32,
            },
        )
        marker = h.home / "os-client/publications" / instance / "in-progress.json"
        after_apply = h.read(instance, run)
        assert (
            apply.status_code == 409
            and apply.json()["code"] == "publication_writer_unresolved"
        )
        assert (
            marker.exists()
            and not after_apply["inflight_invocations"]
            and not after_apply["quiescent"]
        )
        invocation = after_apply["invocations"][0]["id"]
        reconcile_request = h.request(
            "reconcile_invocation",
            instance,
            run,
            plane="main",
            version=after_apply["state_version"],
            payload={
                "invocation_id": invocation,
                "reason": "exact owned writer PID verified absent",
                "stopped": True,
            },
        )
        assert writer_absent()
        reconcile = h.http.post(
            "/api/v1/router/requests",
            json={"home": "default", "request": reconcile_request},
        )
        assert reconcile.status_code in (200, 202), reconcile.text
        reconciled = h.until(
            lambda: (value if value and value["status"] != "queued" else None)
            if (
                value := h.observe(reconcile_request["request_id"]).json()[
                    "core_receipt"
                ]
            )
            else None
        )
        assert reconciled["status"] == "applied"
        stopped = h.read(instance, run)
        assert stopped["quiescent"] and stopped["count"] == 1
        assert stopped["invocations"][0]["writer_state"] == "stopped"
        fact = next(
            row for row in stopped["writer_facts"] if row["invocation_id"] == invocation
        )
        assert fact["request_id"] == reconciled["request_id"]
        repaired = h.http.post(
            "/api/v1/router/repair-publication",
            json={"home": "default", "instance_id": instance},
        )
        assert repaired.status_code == 200, repaired.text
        record = repaired.json()["data"]
        assert record["status"] == "paused_pending"
        final = h.read(instance, run)
        assert (
            final["status"] == "paused" and final["quiescent"] and final["count"] == 1
        )
        assert final["pending_revision"]["candidate_id"] == record["candidate_id"]
        assert final["run_gate"] == stopped["run_gate"]
        assert final["run_gate"]["reason"] == "writer_unknown"
        assert not any(
            row["operation"] in {"accept_revision", "resume_run", "resume_plane"}
            for row in final["controls"]
        )
        assert not (project / "checked.txt").exists() and not marker.exists()
        completed = marker.with_name("completed.json")
        assert json.loads(completed.read_text()) == record
        (h.root / "apply-reconcile-repair-proof.json").write_text(
            json.dumps(
                {
                    "old_os_state_preserved_sha256": old_state,
                    "unknown": unknown,
                    "apply_error": apply.json(),
                    "after_apply": after_apply,
                    "reconciliation_receipt": reconciled,
                    "writer_fact": fact,
                    "repaired_record": record,
                    "final": final,
                },
                indent=2,
            )
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        (project / "release").write_text("owned final cleanup")
        if writer_pid is not None:
            h.until(writer_absent, timeout=5)
        h.stop()


def test_default_apply_202_repeat_observes_one_call_and_durable_paused_pending(
    router_harness, workflow_files, monkeypatch
):
    import threading

    import millrace_os.workflow_drafts as writes

    h = router_harness
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    assert h.service.router_workers.observation_timeout == 0.15
    files = expanded(workflow_files(pause=True))
    identity = draft(h, files)
    body = {
        "home": "default",
        "instance_id": instance,
        "run_id": run,
        "base_state_version": before["state_version"],
        "request_id": uuid4().hex,
    }
    entered, release = threading.Event(), threading.Event()
    observed_writes = []
    original = writes._write

    def slow_owned_publication(path, raw):
        if path.is_relative_to(workflow):
            observed_writes.append(path.relative_to(workflow).as_posix())
            if len(observed_writes) == 1:
                entered.set()
                assert release.wait(5)
        return original(path, raw)

    monkeypatch.setattr(writes, "_write", slow_owned_publication)
    endpoint = f"/api/v1/router/drafts/{identity}/apply"
    try:
        first = h.http.post(endpoint, json=body)
        assert entered.wait(2)
        assert first.status_code == 202, first.text
        initial = first.json()["data"]["observation"]
        assert initial["request_id"] == body["request_id"]
        assert initial["phase"] == "call_active" and initial["core_receipt"] is None
        second = h.http.post(endpoint, json=body)
        assert second.status_code == 202, second.text
        repeat = second.json()["data"]["observation"]
        assert repeat["request_id"] == initial["request_id"]
        assert repeat["request_digest"] == initial["request_digest"]
        assert repeat["phase"] == "call_active" and repeat["core_receipt"] is None
        assert len(observed_writes) == 1
        conflict = h.http.post(
            endpoint,
            json={**body, "base_state_version": body["base_state_version"] + 1},
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["code"] == "request_id_reused"
        )
        marker = h.home / "os-client/publications" / instance / "in-progress.json"
        active_record = json.loads(marker.read_text())
        assert active_record["request_id"] == body["request_id"]
        assert active_record["pause_request"]["request_id"] != body["request_id"]
    finally:
        release.set()

    def completed():
        response = h.http.post(endpoint, json=body)
        assert response.status_code in (200, 202), response.text
        return response.json()["data"] if response.status_code == 200 else None

    record = h.until(completed)
    assert record["status"] == "paused_pending"
    assert sorted(observed_writes) == sorted(files)
    pending = h.read(instance, run)
    assert pending["status"] == "paused" and pending["quiescent"]
    assert pending["count"] == 1 and not pending["inflight_invocations"]
    assert pending["pending_revision"]["candidate_id"] == record["candidate_id"]
    assert pending["run_gate"]["reason"] == "operator_pause"
    assert not (project / "archive.txt").exists()
    assert not any(
        row["operation"] in {"accept_revision", "resume_run", "resume_plane"}
        for row in pending["controls"]
    )
    for operation in ["pause_run", "submit_revision"]:
        rows = [row for row in pending["controls"] if row["operation"] == operation]
        assert len(rows) == 1 and rows[0]["status"] == "applied"
    assert h.http.post(endpoint, json=body).json()["data"] == record
    assert sorted(observed_writes) == sorted(files)
    completed_path = marker.with_name("completed.json")
    durable = completed_path.read_bytes()
    assert json.loads(durable) == record and not marker.exists()
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    state_dir = h.config.state_dir
    h.stop()
    h.start()
    assert h.config.state_dir == state_dir and h.read(instance, run) == pending
    assert completed_path.read_bytes() == durable
    repair = h.http.post(
        "/api/v1/router/repair-publication",
        json={"home": "default", "instance_id": instance},
    )
    assert (
        repair.status_code == 404 and repair.json()["code"] == "publication_not_found"
    )
    assert h.read(instance, run) == pending
    (h.root / "default-apply-202-proof.json").write_text(
        json.dumps(
            {
                "body": body,
                "first202": first.json(),
                "repeat202": second.json(),
                "active_record": active_record,
                "completed_record": record,
                "pending_after_restart": pending,
                "repair_not_needed": repair.json(),
            },
            indent=2,
        )
    )
