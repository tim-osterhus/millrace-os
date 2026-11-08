"""Headless parity reads over a real Apply: the 202, its local result and exact
publication facts, explicit continuation, and unknown after retirement/restart."""

import threading
from uuid import uuid4

from test_workflow_drafts import draft, expanded


def test_apply_local_result_publication_status_then_explicit_continuation(
    router_harness, workflow_files, monkeypatch
):
    import millrace_os.workflow_drafts as writes

    h = router_harness

    def joined():
        return all(row["joined"] for row in h.service.router_workers.snapshot())

    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    before = h.settled(instance, run, "paused")
    h.until(joined)
    identity = draft(h, expanded(workflow_files(pause=True)))
    request_id = uuid4().hex
    local = f"/api/v1/router/local-calls/{request_id}"
    status = f"/api/v1/router/publications/{instance}"
    exact = {"home": "default", "request_id": request_id}
    entered, release = threading.Event(), threading.Event()
    original = writes._write

    def held_publication(path, raw):
        if path.is_relative_to(workflow) and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(path, raw)

    monkeypatch.setattr(writes, "_write", held_publication)
    try:
        first = h.http.post(
            f"/api/v1/router/drafts/{identity}/apply",
            json={
                "home": "default",
                "instance_id": instance,
                "run_id": run,
                "base_state_version": before["state_version"],
                "request_id": request_id,
            },
        )
        assert entered.wait(5)
        assert first.status_code == 202, first.text
        active = h.http.get(local, params={"home": "default"})
        assert active.status_code == 202, active.text
        observed = active.json()
        assert (observed["phase"], observed["result"]) == ("call_active", None)
        assert observed["operation"] == "draft_apply"
        assert observed["instance_id"] == instance and observed["run_id"] == run
        progress = h.http.get(status, params=exact)
        assert progress.status_code == 200, progress.text
        facts = progress.json()
        assert facts["completed"] is None
        assert facts["in_progress"]["request_id"] == request_id
        assert "frozen_token" not in facts["in_progress"]
    finally:
        release.set()

    def returned():
        response = h.http.get(local, params={"home": "default"})
        return response if response.status_code == 200 else None

    done = h.until(returned).json()
    record = done["result"]
    assert done["phase"] == "call_returned" and record["status"] == "paused_pending"
    facts = h.http.get(status, params=exact).json()
    assert facts["in_progress"] is None
    assert facts["completed"] == {
        k: v for k, v in record.items() if k != "frozen_token"
    }
    # The reads accepted, resumed and repaired nothing.
    pending = h.read(instance, run)
    assert pending["pending_revision"]["candidate_id"] == record["candidate_id"]
    assert not any(
        row["operation"] in {"accept_revision", "resume_run", "resume_plane"}
        for row in pending["controls"]
    )
    # Continuation stays explicit: accept, then resume the paused plane.
    accepted = h.send(
        h.request(
            "accept_revision",
            instance,
            run,
            version=pending["state_version"],
            payload={"candidate_id": record["candidate_id"]},
        )
    )
    assert accepted["status"] == "applied"
    h.until(joined)
    held = h.read(instance, run)
    assert held["planes"]["main"]["status"] == "paused"
    h.send(
        h.request(
            "resume_plane", instance, run, plane="main", version=held["state_version"]
        )
    )
    h.settled(instance, run, "succeeded")
    assert (project / "archive.txt").read_text() == "archived 42\n"
    # After retirement the local result is unknown; publication facts remain.
    h.until(joined)
    h.service.router_workers.observation_ttl = 0
    gone = h.http.get(local, params={"home": "default"})
    assert gone.status_code == 404 and gone.json()["code"] == "local_call_unavailable"
    h.stop()
    h.start()
    gone = h.http.get(local, params={"home": "default"})
    assert gone.status_code == 404 and gone.json()["code"] == "local_call_unavailable"
    after = h.http.get(status, params=exact)
    assert after.status_code == 200 and after.json()["completed"] == facts["completed"]
