"""Public HTTP command journeys, receipt truth and owned caller lifecycles."""

import hashlib
import json
from uuid import uuid4

import pytest


def test_inventory_receipt_and_replay_truth(router_harness):
    h = router_harness
    workflow, project = h.flow()
    instance = h.create(workflow, project)
    inventory = h.http.get("/api/v1/router/instances?home=default")
    assert inventory.status_code == 200, inventory.text
    assert [item["instance_id"] for item in inventory.json()["data"]] == [instance]
    request = h.request(
        "start_run", instance, version=1, payload={"edit_mode": "locked"}
    )
    receipt = h.send(request)
    final = h.settled(instance, receipt["run_id"], "succeeded")
    assert (project / "checked.txt").read_text() == "checked 42\n"
    assert final["count"] == 2
    expected_digest = hashlib.sha256(
        json.dumps(
            request, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    assert receipt["request_digest"] == expected_digest
    assert h.send(request) == receipt
    assert h.read(instance, receipt["run_id"]) == final
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    stale = h.send(
        h.request("start_run", instance, version=1, payload={"edit_mode": "locked"})
    )
    assert stale["status"] == "refused" and stale["code"] == "stale_state_version"
    assert len(h.read(instance)["runs"]) == 1
    changed = {**request, "payload": {"edit_mode": "live"}}
    response = h.http.post(
        "/api/v1/router/requests", json={"home": "default", "request": changed}
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "request_id_reused"
    assert h.read(instance, receipt["run_id"]) == final
    missing = h.observe(uuid4().hex)
    assert (
        missing.status_code == 404
        and missing.json()["code"] == "observation_unavailable"
    )


def test_request_observation_never_fabricates_receipt_or_loses_owner(
    router_factory, tmp_path
):
    import threading

    from millrace_os.router_client import RouterClient

    class DelayedClient(RouterClient):
        entered = threading.Event()
        admit = threading.Event()
        returned_receipts = {}

        def execute_request(self, home, request, on_started=None):
            if request["operation"] == "create_instance":
                self.entered.set()
                assert self.admit.wait(5)
            result = super().execute_request(home, request, on_started)
            self.returned_receipts[request["request_id"]] = result
            return result

    client = DelayedClient({"default": tmp_path / "delayed/core"})
    h = router_factory("delayed", router_client=client)
    h.service.router_workers.observation_timeout = 0.01
    workflow, project = h.flow(wait=True)
    import sys
    from pathlib import Path

    request = h.request(
        "create_instance",
        version=0,
        payload={
            "workflow_file": str(workflow / "workflow.json"),
            "project_root": str(project),
            "path": [str(Path(sys.executable).parent), "/usr/bin", "/bin"],
            "env_allowlist": [],
            "host_paths": {},
        },
    )
    try:
        first = h.send(request, wait=False)
        assert client.entered.wait(2)
        assert first["core_receipt"] is None and first["instance_id"] is None
        assert first["phase"] == "call_active"
        h.service.router_workers.observation_ttl = 0
        again = h.observe(request["request_id"]).json()
        assert again["core_receipt"] is None
        assert not h.service.router_workers.snapshot()[0]["joined"]
        h.service.router_workers.capacity = 1
        refused = h.http.post(
            "/api/v1/router/requests",
            json={"home": "default", "request": {**request, "request_id": uuid4().hex}},
        )
        assert refused.status_code == 429 and refused.json()["code"] == "router_busy"
        assert len(h.service.router_workers.snapshot()) == 1
    finally:
        h.service.router_workers.observation_ttl = 300
        client.admit.set()
    # The same saved body observes the original call; it cannot create twice.
    created = h.send(request)
    instance = created["instance_id"]
    h.service.router_workers.observation_ttl = 300
    start = h.request("start_run", instance, version=1, payload={"edit_mode": "locked"})
    h.send(start, wait=False)
    h.until(lambda: (project / "started").exists())
    observation = h.until(
        lambda: (
            v if (v := h.observe(start["request_id"]).json())["core_receipt"] else None
        )
    )
    assert observation["phase"] == "call_active"
    assert observation["core_receipt"]["status"] == "applied"
    run = observation["run_id"]
    stop_request = h.request("pause_run", instance, run)
    h.send(stop_request, wait=False)
    queued = h.until(lambda: client.returned_receipts.get(stop_request["request_id"]))
    assert queued["status"] == "queued"
    (project / "release").write_text("finish current invocation")
    stopped = h.send(stop_request)
    assert stopped["status"] == "applied"
    assert stopped["active_invocations"] == sorted(set(stopped["active_invocations"]))
    assert all(isinstance(value, str) for value in stopped["active_invocations"])
    assert stopped["target_planes"] == ["main"]
    (project / "release").write_text("finish current invocation")
    paused = h.settled(instance, run, "paused")
    assert paused["count"] == 1
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))
    h.stop()
    h.start()
    unknown = h.observe(start["request_id"])
    assert (
        unknown.status_code == 404
        and unknown.json()["code"] == "observation_unavailable"
    )
    lookup = h.observe(start["request_id"], instance_id=instance, run_id=run)
    assert lookup.status_code == 200
    assert lookup.json()["source"] == "core_receipt"
    assert lookup.json()["phase"] == "receipt_observed"
    assert lookup.json()["core_receipt"] == observation["core_receipt"]
    assert h.read(instance, run) == paused


def test_closing_ingress_promotion_remains_owned_until_final_drain(
    router_factory, tmp_path
):
    """A control that becomes the holder remains owned after its receipt exists."""
    import threading

    from millrace_os.router_client import RouterClient

    class ClosingClient(RouterClient):
        entered = threading.Event()
        holder_returned = threading.Event()
        returned = threading.Event()
        release = threading.Event()

        def execute_request(self, home, request, on_started=None):
            if request["operation"] == "pause_run":
                self.entered.set()
                assert self.holder_returned.wait(5)
            result = super().execute_request(home, request, on_started)
            if request["operation"] == "start_run":
                self.holder_returned.set()
            if request["operation"] == "pause_run":
                self.returned.set()
                assert self.release.wait(5)
            return result

    client = ClosingClient({"default": tmp_path / "closing/core"})
    h = router_factory("closing", router_client=client)
    workflow, project = h.flow(pause=True, wait=True)
    instance = h.create(workflow, project)
    receipt = h.start_run(instance)
    run = receipt["run_id"]
    h.until(lambda: (project / "started").exists())
    request = h.request("pause_run", instance, run)
    h.service.router_workers.observation_timeout = 0.01
    try:
        h.send(request, wait=False)
        assert client.entered.wait(2)
        # The ingress call was admitted while the first public holder was live.
        # It crosses the public facade only after that holder's final return.
        (project / "release").write_text("close first holder")
        assert client.returned.wait(3)
        state = h.read(instance, run)
        obs = h.until(
            lambda: (
                v
                if (v := h.observe(request["request_id"]).json())["core_receipt"]
                else None
            )
        )
        assert (
            obs["phase"] == "call_active" and obs["core_receipt"]["status"] == "applied"
        )
        h.service.request_shutdown()
        assert h.service.lifecycle == "closing"
        assert h.http.get("/api/v1/router/instances?home=default").status_code == 200
        refused = h.http.post(
            "/api/v1/router/requests",
            json={
                "home": "default",
                "request": h.request(
                    "submit_revision", instance, run, version=state["state_version"]
                ),
            },
        )
        assert refused.status_code == 503
        assert any(
            not row["joined"] and row["slot"] == "stop"
            for row in h.service.router_workers.snapshot()
        )
        assert h.service._state is not None
    finally:
        client.release.set()
        h.service.stop()
    assert all(row["joined"] for row in h.service.router_workers.snapshot())
    h.http.close()
    h.start()
    assert h.read(instance, run)["count"] == 1
    assert h.read(instance, run)["status"] == "paused"


def test_first_stop_drains_second_cancel_and_restart_resumes_once(router_harness):
    h = router_harness
    workflow, project = h.flow(wait=True)
    instance = h.create(workflow, project)
    start = h.start_run(instance)
    run = start["run_id"]
    h.until(lambda: (project / "started").exists())
    h.service.request_shutdown()
    # Reading and an explicit second cancellation remain legal during the drain.
    assert (
        h.http.get(
            f"/api/v1/router/instances/{instance}/runs/{run}?home=default"
        ).status_code
        == 200
    )
    h.service.request_shutdown(cancel=True)
    h.service.stop()
    assert all(row["joined"] for row in h.service.router_workers.snapshot())
    h.http.close()
    h.start()
    cancelled = h.settled(instance, run, "cancelled")
    assert cancelled["count"] == 1
    assert all(item["writer_state"] == "stopped" for item in cancelled["invocations"])
    assert not (project / "checked.txt").exists()
    # Restart never creates replacement work; a fresh run is an explicit intent.
    assert len(h.read(instance)["runs"]) == 1
    (project / "release").write_text("fresh explicit run")
    fresh = h.start_run(instance)
    assert fresh["run_id"] != run
    assert h.settled(instance, fresh["run_id"], "succeeded")["count"] == 2
    assert h.read(instance, run) == cancelled


def test_headless_two_instances_author_edit_expand_and_recover(
    router_harness, workflow_files, capsys
):
    from millrace_os.cli import main

    h = router_harness
    args = [
        "--home",
        "default",
        "--port",
        str(h.config.port),
        "--config-dir",
        str(h.config.config_dir),
        "--state-dir",
        str(h.config.state_dir),
    ]
    assert main(["router", "draft", "create", *args]) == 0
    identity = json.loads(capsys.readouterr().out)["draft_id"]
    for name, content in workflow_files(pause=True).items():
        source = h.root / name
        source.write_text(content)
        assert (
            main(
                [
                    "router",
                    "draft",
                    "put",
                    *args,
                    "--draft",
                    identity,
                    "--path",
                    name,
                    "--file",
                    str(source),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["draft_id"] == identity
    destination = h.root / "authored"
    assert (
        main(
            [
                "router",
                "catalog",
                "copy",
                *args,
                "--draft",
                identity,
                "--destination",
                str(destination),
            ]
        )
        == 0
    )
    copied = json.loads(capsys.readouterr().out)
    assert copied["authority"] == "os-client"
    project = h.root / "author-project"
    project.mkdir()
    h.projects.append(project)
    authored = h.create(destination, project)
    other_flow, other_project = h.flow("sibling")
    sibling = h.create(other_flow, other_project)
    original = h.start_run(authored)["run_id"]
    other = h.start_run(sibling, mode="live")["run_id"]
    paused = h.settled(authored, original, "paused")
    completed = h.settled(sibling, other, "succeeded")
    assert (
        completed["count"] == 2
        and (other_project / "checked.txt").read_text() == "checked 42\n"
    )
    assert not (project / "checked.txt").exists()
    assert (
        main(["router", "validate", *args, "--instance", authored, "--run", original])
        == 0
    )
    validation = json.loads(capsys.readouterr().out)
    assert validation.get("source_provider") == "core", validation
    assert (
        validation["data"]["validation_context"]["run_state_version"]
        == paused["state_version"]
    )
    h.stop()
    h.start()
    assert h.read(authored, original) == paused
    assert h.read(sibling, other) == completed
    request = h.request(
        "resume_plane",
        authored,
        original,
        plane="main",
        version=paused["state_version"],
    )
    request_file = h.root / "resume.json"
    request_file.write_text(json.dumps(request))
    assert main(["router", "request", *args, "--file", str(request_file)]) == 0
    observation = json.loads(capsys.readouterr().out)
    assert observation["request_id"] == request["request_id"]
    final = h.settled(authored, original, "succeeded")
    assert (
        final["count"] == 2 and (project / "checked.txt").read_text() == "checked 42\n"
    )
    assert h.read(sibling, other) == completed
    assert final["invocations"][0] == paused["invocations"][0]


def test_killed_owned_service_restart_preserves_unknown_writer(router_harness):
    """Kill only the exact Popen-owned OS; Core uncertainty stays visible."""
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx

    from millrace_os.credentials import CredentialStore

    h = router_harness
    workflow, project = h.flow(wait=True)
    script = workflow / "work.py"
    script.write_text(
        "import os\nfrom pathlib import Path\n"
        "Path('writer.pid').write_text(str(os.getpid()))\n" + script.read_text()
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
service=Service(ApplicationConfig(config_dir=root/'config',state_dir=root/'os-state',credential_path=root/'config/credentials.json',port=int(port),router_state_homes={'default':root/'core'}))
service.start()
(root/'child-ready').write_text(str(__import__('os').getpid()))
while True: time.sleep(.05)
"""
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }
    child = subprocess.Popen(
        [sys.executable, "-c", launch, str(h.root), str(h.config.port)],
        env=environment,
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
        before = h.read(instance, run)
        assert before["count"] == 1 and not before["quiescent"]
        assert child.poll() is None and writer_pid != child.pid
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=3) == -signal.SIGKILL
        h.http.close()
        h.start()
        # Foundation deliberately refuses leftover SQLite sidecars after kill;
        # expose recovery honestly and preserve all old OS state evidence.
        unavailable = h.http.get(
            f"/api/v1/router/instances/{instance}/runs/{run}?home=default"
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "storage_unavailable"
        old_state = {
            p.name: p.read_bytes() for p in h.config.state_dir.iterdir() if p.is_file()
        }
        assert any(name.endswith("-wal") for name in old_state)
        h.service.stop()
        h.http.close()
        # An explicit operator chooses fresh OS-local state, keeping exactly the
        # same durable Core home; no SQLite/WAL repair or runtime state surgery.
        from dataclasses import replace

        h.config = replace(h.config, state_dir=h.root / "fresh-os-state")
        h.start()
        assert {
            p.name: p.read_bytes()
            for p in (h.root / "os-state").iterdir()
            if p.is_file()
        } == old_state
        # Restart and reads neither invent completion nor relaunch any work.
        retained = h.read(instance, run)
        assert retained == before
        assert h.service.router_workers.snapshot() == []
        recovered = h.send(
            h.request("recover_run", instance, run, version=retained["state_version"])
        )
        # Acquiring the new holder first records abandoned work, advancing the
        # version. The original explicit version truthfully becomes stale.
        assert recovered["status"] == "refused"
        assert recovered["code"] == "stale_state_version"
        # A final receipt can precede return/join of its foreground call.
        h.until(
            lambda: all(row["joined"] for row in h.service.router_workers.snapshot())
        )
        unknown = h.read(instance, run)
        assert unknown["status"] == "paused" and not unknown["quiescent"]
        assert unknown["pause_reason"] == "writer_unknown"
        assert unknown["count"] == 1 and len(unknown["invocations"]) == 1
        assert unknown["invocations"][0]["writer_state"] != "stopped"
        unsafe = h.send(
            h.request("resume_run", instance, run, version=unknown["state_version"])
        )
        assert unsafe["status"] == "refused" and unsafe["code"] == "writer_unknown"
        assert not (project / "checked.txt").exists()
        # Release only this test's command and verify its exact recorded PID has
        # exited before the operator's supported reconciliation assertion.
        (project / "release").write_text("owned writer cleanup")

        def exited():
            try:
                os.kill(writer_pid, 0)
            except ProcessLookupError:
                return True
            return False

        h.until(exited, timeout=5)
        current = h.read(instance, run)
        reconciled = h.send(
            h.request(
                "reconcile_invocation",
                instance,
                run,
                plane="main",
                version=current["state_version"],
                payload={
                    "invocation_id": current["invocations"][0]["id"],
                    "reason": "verified owned command exited",
                    "stopped": True,
                },
            )
        )
        assert reconciled["status"] == "applied"
        h.send(h.request("cancel_run", instance, run))
        final = h.settled(instance, run, "cancelled")
        assert final["count"] == 1
        (h.root / "kill-restart-proof.json").write_text(
            json.dumps(
                {
                    "owned_os_pid": child.pid,
                    "owned_writer_pid": writer_pid,
                    "old_os_state_files": sorted(old_state),
                    "before_kill": before,
                    "after_explicit_fresh_os_state": retained,
                    "recovery_receipt": recovered,
                    "unknown_writer": unknown,
                    "unsafe_resume_receipt": unsafe,
                    "reconciliation_receipt": reconciled,
                    "final": final,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    finally:
        if child.poll() is None:
            # This handle is the only kill target, never a discovered user PID.
            child.kill()
            child.wait(timeout=3)
        (project / "release").write_text("owned fixture cleanup")
        if writer_pid is not None:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(writer_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)


@pytest.mark.parametrize(
    "resolution",
    ["refused", "applied"],
    ids=["queued-start-refused", "queued-start-later-run"],
)
def test_shutdown_tracks_physically_returned_queued_start_until_resolved(
    router_harness, resolution
):
    import threading

    from millrace_plus.router import control

    h = router_harness
    workflow, project = h.flow(wait=True)
    instance = h.create(workflow, project)
    entered, release = threading.Event(), threading.Event()
    external_ids, errors = {}, []
    external = h.request(
        "start_run", instance, version=1, payload={"edit_mode": "locked"}
    )

    def started(ids):
        if not external_ids:
            external_ids.update(ids)
        entered.set()
        assert release.wait(10)

    def hold():
        try:
            control.execute_request(external, state_home=h.home, on_started=started)
        except BaseException as exc:
            errors.append({"code": getattr(exc, "code", None), "error": repr(exc)})

    holder = threading.Thread(target=hold, daemon=False)
    holder.start()
    try:
        assert entered.wait(3)
        if resolution == "applied":
            # FIFO cancellation makes the external run terminal before the next
            # start is considered. Both submissions use only public Core calls.
            cancel = control.execute_request(
                h.request(
                    "cancel_plane", instance, external_ids["run_id"], plane="main"
                ),
                state_home=h.home,
            )
            assert cancel["status"] == "queued"
        request = h.request(
            "start_run",
            instance,
            version=1 if resolution == "refused" else 2,
            payload={"edit_mode": "locked"},
        )
        h.send(request, wait=False)
        queued = h.until(
            lambda: (
                v
                if (v := h.observe(request["request_id"]).json())["core_receipt"]
                else None
            )
        )
        assert queued["core_receipt"]["status"] == "queued"
        assert queued["phase"] == "call_returned"
        # Physical return is not logical resolution, even under TTL pressure.
        h.service.router_workers.observation_ttl = 0
        h.service.request_shutdown()
        assert not h.service.wait_for_stop(0.15)
        assert h.service.lifecycle == "closing" and holder.is_alive()
        retained = h.service.router_workers.snapshot()
        assert any(
            row["observation"]["request_id"] == request["request_id"]
            for row in retained
        )
        release.set()

        def resolved():
            state = control.inspect_instance(instance, state_home=h.home)
            return next(
                (
                    c
                    for c in state["controls"]
                    if c["request_id"] == request["request_id"]
                    and c["status"] != "queued"
                ),
                None,
            )

        final_receipt = h.until(resolved)
        assert final_receipt["status"] == resolution
        assert (
            final_receipt["request_digest"] == queued["core_receipt"]["request_digest"]
        )
        if resolution == "refused":
            assert final_receipt["code"] == "stale_state_version"
        if resolution == "applied":
            run_id = final_receipt["run_id"]
            assert run_id != external_ids["run_id"]
            paused = h.until(
                lambda: (
                    value
                    if (
                        value := control.inspect_run(
                            instance, run_id, state_home=h.home
                        )
                    )["run_gate"]
                    is not None
                    else None
                )
            )
            assert paused["run_gate"]["reason"] == "operator_pause"
            (project / "release").write_text("finish only admitted work")
        assert h.service.wait_for_stop(5)
        (project / "release").write_text("complete external holder cleanup")
        holder.join(5)
        assert not holder.is_alive()
        if resolution == "refused":
            assert errors == []
        else:
            # Scoped cancellation claims the first ready run's project hold;
            # this foreign holder promotes the next start before releasing it.
            # Preserve that precise Core diagnostic, not a healthy continuation
            # claim. OS must still discover and safely pause the admitted run.
            assert errors == [
                {
                    "code": "project_owned",
                    "error": (
                        "RouterError('An unresolved project hold blocks this writer')"
                    ),
                }
            ]
        assert all(row["joined"] for row in h.service.router_workers.snapshot())
        if resolution == "applied":
            final = control.inspect_run(
                instance, final_receipt["run_id"], state_home=h.home
            )
            assert final["status"] == "paused" and final["quiescent"]
            assert final["count"] == 0
            assert (
                control.inspect_run(
                    instance, external_ids["run_id"], state_home=h.home
                )["status"]
                == "cancelled"
            )
            assert all(i["writer_state"] == "stopped" for i in final["invocations"])
            assert not (project / "checked.txt").exists()
            old_run = control.inspect_run(
                instance, external_ids["run_id"], state_home=h.home
            )
            assert old_run["count"] == 0 and old_run["quiescent"]
            assert old_run["invocations"] == final["invocations"] == []
            hold = json.loads(
                (project / ".millrace-router-owner/owner.json").read_bytes()
            )
            assert hold == {
                "hold_id": final["hold_id"],
                "instance_id": instance,
                "run_id": final["id"],
                "store_path": str(h.home / "instances" / instance / "state.sqlite3"),
            }
            (h.root / "queued-start-proof.json").write_text(
                json.dumps(
                    {
                        "external_error": errors,
                        "request_receipt": final_receipt,
                        "old_run": old_run,
                        "generated_run": final,
                        "current_hold": hold,
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
    finally:
        h.service.router_workers.observation_ttl = 300
        release.set()
        (project / "release").write_text("owned queued-start cleanup")
        holder.join(10)
        assert not holder.is_alive()


def test_returned_queued_pause_frees_reserved_ingress_for_later_cancel(router_harness):
    import threading

    from millrace_plus.router import control

    h = router_harness
    workflow, _ = h.flow()
    instance = h.create(workflow, h.projects[-1])
    entered, release = threading.Event(), threading.Event()
    ids, external_errors = {}, []

    def started(value):
        ids.update(value)
        entered.set()
        assert release.wait(5)
        raise RuntimeError("owned holder exits before queue drain")

    def external_holder():
        try:
            control.execute_request(
                h.request(
                    "start_run", instance, version=1, payload={"edit_mode": "locked"}
                ),
                state_home=h.home,
                on_started=started,
            )
        except RuntimeError as exc:
            external_errors.append(str(exc))

    holder = threading.Thread(target=external_holder, daemon=False)
    holder.start()
    try:
        assert entered.wait(2)
        pause = h.request("pause_run", instance, ids["run_id"])
        h.send(pause, wait=False)

        def physically_joined():
            return next(
                (
                    row
                    for row in h.service.router_workers.snapshot()
                    if row["observation"]["request_id"] == pause["request_id"]
                    and row["joined"]
                ),
                None,
            )

        joined = h.until(physically_joined)
        assert joined["observation"]["phase"] == "call_returned"
        assert joined["observation"]["core_receipt"]["status"] == "queued"
        assert holder.is_alive()
        # The foreign caller leaves without polling the queued pause. Its exact
        # callback failure is fixture evidence, not a forged Core completion.
        release.set()
        holder.join(3)
        assert not holder.is_alive()
        assert external_errors == ["owned holder exits before queue drain"]
        before = control.inspect_run(instance, ids["run_id"], state_home=h.home)
        assert (
            next(
                r for r in before["controls"] if r["request_id"] == pause["request_id"]
            )["status"]
            == "queued"
        )
        cancel = h.request("cancel_run", instance, ids["run_id"])
        receipt = h.send(cancel)
        assert receipt["status"] in {"applied", "refused"}
        # This must be an actual durable Core receipt, never OS router_busy or
        # a healthy-cancellation claim substituted for Core guard/writer facts.
        after = control.inspect_run(instance, ids["run_id"], state_home=h.home)
        assert (
            next(
                r for r in after["controls"] if r["request_id"] == cancel["request_id"]
            )
            == receipt
        )
        assert after["count"] == 0 and after["invocations"] == []
    finally:
        release.set()
        holder.join(5)
        assert not holder.is_alive()


def test_generated_run_learned_between_scan_and_final_join_is_paused(router_harness):
    import threading

    from millrace_plus.router import control

    h = router_harness
    workflow, project = h.flow(wait=True)
    instance = h.create(workflow, project)
    entered, release = threading.Event(), threading.Event()
    ids, errors = {}, []

    def started(value):
        if not ids:
            ids.update(value)
        entered.set()
        assert release.wait(10)

    def external_holder():
        try:
            control.execute_request(
                h.request(
                    "start_run", instance, version=1, payload={"edit_mode": "locked"}
                ),
                state_home=h.home,
                on_started=started,
            )
        except BaseException as exc:
            errors.append({"code": getattr(exc, "code", None), "error": repr(exc)})

    holder = threading.Thread(target=external_holder, daemon=False)
    holder.start()
    try:
        assert entered.wait(3)
        cancelled = control.execute_request(
            h.request("cancel_plane", instance, ids["run_id"], plane="main"),
            state_home=h.home,
        )
        assert cancelled["status"] == "queued"
        request = h.request(
            "start_run", instance, version=2, payload={"edit_mode": "locked"}
        )
        h.send(request, wait=False)
        workers = h.service.router_workers
        call = workers._calls[workers._key("default", request["request_id"])]
        assert call.done.wait(3) and call.run_id is None
        underlying = workers._lock
        scans = []

        class ResolveBetweenScanAndJoin:
            def __enter__(self):
                if threading.current_thread().name == "millrace-os-drain":
                    scans.append(True)
                    if len(scans) == 2:
                        # First pass saw no runID. Resolve the real public start
                        # before final retirement, while its watcher can still
                        # acquire the original OS lock and publish that target.
                        release.set()
                        call.watcher.join(5)
                        assert not call.watcher.is_alive() and call.run_id is not None
                return underlying.__enter__()

            def __exit__(self, *args):
                return underlying.__exit__(*args)

        workers._lock = ResolveBetweenScanAndJoin()
        h.service.request_shutdown()
        assert h.service.wait_for_stop(5)
        holder.join(3)
        assert not holder.is_alive()
        assert errors == [
            {
                "code": "project_owned",
                "error": "RouterError('An unresolved project hold blocks this writer')",
            }
        ]
        instance_state = control.inspect_instance(instance, state_home=h.home)
        applied = next(
            r
            for r in instance_state["controls"]
            if r["request_id"] == request["request_id"]
        )
        assert applied["status"] == "applied" and applied["run_id"] != ids["run_id"]
        final = control.inspect_run(instance, applied["run_id"], state_home=h.home)
        (h.root / "join-handoff-proof.json").write_text(
            json.dumps(
                {
                    "resolved_receipt": applied,
                    "generated_run": final,
                    "foreign_errors": errors,
                    "shutdown_finished": h.service.lifecycle == "stopped",
                    "coordinator_lock_entries": len(scans),
                    "calls": workers.snapshot(),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        assert final["run_gate"] is not None
        assert final["run_gate"]["reason"] == "operator_pause"
        assert final["status"] == "paused" and final["quiescent"]
        assert final["count"] == 0 and final["invocations"] == []
        assert all(row["joined"] for row in workers.snapshot())
    finally:
        release.set()
        (project / "release").write_text("owned scan-handoff cleanup")
        holder.join(5)
        assert not holder.is_alive()


@pytest.mark.parametrize("read_case", ["unknown_run", "store_read_error"])
def test_shutdown_survives_public_read_refusal_and_keeps_truth_unsettled(
    router_factory, tmp_path, monkeypatch, read_case
):
    import threading

    from millrace_os.router_client import RouterClient, RouterUnavailable

    entered = threading.Event()
    release = threading.Event()
    armed = threading.Event()
    attempted = threading.Event()
    restore_truth = threading.Event()
    target = []
    failures = []

    class DelayedControl(RouterClient):
        def execute_request(self, home, request, on_started=None):
            if target and request["request_id"] == target[0]:
                entered.set()
                assert release.wait(5)
            return super().execute_request(home, request, on_started)

        def inspect_run(self, home, instance_id, run_id):
            if armed.is_set():
                attempted.set()
                if read_case == "store_read_error" and not restore_truth.is_set():
                    raise RouterUnavailable("core_store_corrupt", 409)
            return super().inspect_run(home, instance_id, run_id)

    homes = {"default": tmp_path / "shared-core"}
    client = DelayedControl(homes)
    h = router_factory("read-refusal", homes=homes, router_client=client)
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    if read_case == "unknown_run":
        run, version = "f" * 32, 1
    else:
        run = h.start_run(instance)["run_id"]
        version = h.settled(instance, run, "paused")["state_version"]
        h.until(
            lambda: all(row["joined"] for row in h.service.router_workers.snapshot())
        )
    request = h.request("submit_revision", instance, run, version=version)
    target.append(request["request_id"])
    original_hook = threading.excepthook

    def capture(args):
        if args.thread.name == "millrace-os-drain":
            failures.append(
                {
                    "type": args.exc_type.__name__,
                    "code": getattr(args.exc_value, "code", None),
                }
            )
        else:
            original_hook(args)

    monkeypatch.setattr(threading, "excepthook", capture)
    try:
        h.service.router_workers.observation_timeout = 0.01
        observation = h.send(request, wait=False)
        assert entered.wait(2)
        assert (
            observation["phase"] == "call_active"
            and observation["core_receipt"] is None
        )
        armed.set()
        h.service.request_shutdown()
        assert attempted.wait(2)
        assert not h.service.wait_for_stop(0.1)
        assert h.service._shutdown_thread.is_alive(), failures
        assert h.service.lifecycle == "closing" and h.service.state is not None
        assert h.http.get("/api/v1/router/instances?home=default").status_code == 200
        assert any(not row["joined"] for row in h.service.router_workers.snapshot())
        release.set()
        h.until(
            lambda: all(row["joined"] for row in h.service.router_workers.snapshot())
        )
        if read_case == "store_read_error":
            assert not h.service.wait_for_stop(0.1)
            assert h.service._shutdown_thread.is_alive() and h.service.state is not None
            response = h.http.get(
                f"/api/v1/router/instances/{instance}/runs/{run}?home=default"
            )
            assert (
                response.status_code == 409
                and response.json()["code"] == "core_store_corrupt"
            )
            # No refusal is interpreted as stopped, quiescent or nonexistent.
            assert (
                h.http.get("/api/v1/router/instances?home=default").status_code == 200
            )
            restore_truth.set()
        assert h.service.wait_for_stop(5)
        assert not failures and h.service.state is None
        rows = h.service.router_workers.snapshot()
        assert all(row["joined"] for row in rows)
        if read_case == "unknown_run":
            call = next(
                row
                for row in rows
                if row["observation"]["request_id"] == request["request_id"]
            )
            assert call["worker_error"] is None
            assert call["observation"]["core_receipt"]["status"] == "refused"
            assert call["observation"]["core_receipt"]["code"] == "unknown_run"
        (h.root / (read_case + "-shutdown-proof.json")).write_text(
            json.dumps(
                {
                    "request": request,
                    "initial_observation": observation,
                    "joined_calls": rows,
                    "drain_failures": failures,
                    "state_closed": h.service.state is None,
                },
                indent=2,
            )
        )
    finally:
        release.set()
        restore_truth.set()
        h.until(
            lambda: all(row["joined"] for row in h.service.router_workers.snapshot())
        )
        h.service.stop()
        if h.service.lifecycle != "stopped":
            # Dispose only this fixture's HTTP/SQLite resources after all public
            # calls joined; this is not Core state repair or stopped evidence.
            h.service._finish_stop()


def test_invalid_run_id_shutdown_waits_for_public_call_then_closes(
    router_factory, tmp_path
):
    import threading

    from millrace_os.router_client import RouterClient

    entered, release, inspected = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    target = []

    class DelayedInvalidTarget(RouterClient):
        def execute_request(self, home, request, on_started=None):
            if target and request["request_id"] == target[0]:
                entered.set()
                assert release.wait(5)
            return super().execute_request(home, request, on_started)

        def inspect_run(self, home, instance_id, run_id):
            if run_id == "invalid-run-id":
                inspected.set()
            return super().inspect_run(home, instance_id, run_id)

    homes = {"default": tmp_path / "shared-core"}
    client = DelayedInvalidTarget(homes)
    h = router_factory("invalid-run", homes=homes, router_client=client)
    workflow, project = h.flow()
    instance = h.create(workflow, project)
    request = h.request("submit_revision", instance, "invalid-run-id", version=1)
    target.append(request["request_id"])
    try:
        h.service.router_workers.observation_timeout = 0.01
        first = h.send(request, wait=False)
        assert entered.wait(2)
        assert first["phase"] == "call_active" and first["core_receipt"] is None
        h.service.request_shutdown()
        assert inspected.wait(2)
        assert not h.service.wait_for_stop(0.05)
        assert h.service._shutdown_thread.is_alive() and h.service.state is not None
        assert any(not row["joined"] for row in h.service.router_workers.snapshot())
        assert h.http.get("/api/v1/router/instances?home=default").status_code == 200
        release.set()
        h.until(
            lambda: all(row["joined"] for row in h.service.router_workers.snapshot())
        )
        assert h.service.wait_for_stop(0.5), (
            "Definitively invalid run ID must not retain an impossible target "
            "after physical join"
        )
        rows = h.service.router_workers.snapshot()
        call = next(
            row
            for row in rows
            if row["observation"]["request_id"] == request["request_id"]
        )
        assert call["joined"] and call["worker_error"]["code"] == "core_invalid_id"
        assert call["observation"]["core_receipt"] is None
        assert call["observation"]["phase"] == "call_failed"
        assert h.service.state is None and h.service.lifecycle == "stopped"
        (h.root / "invalid-id-shutdown-proof.json").write_text(
            json.dumps(
                {
                    "request": request,
                    "initial_observation": first,
                    "joined_calls": rows,
                    "state_closed": h.service.state is None,
                },
                indent=2,
            )
        )
    finally:
        release.set()
        h.until(
            lambda: all(row["joined"] for row in h.service.router_workers.snapshot())
        )
        # Disposal only after public calls joined. This does not establish a Core
        # stopped fact or make the preceding product assertions pass.
        if not h.service.wait_for_stop(0.01):
            # Failed old-source regression: dispose this fixture's impossible-ID
            # closing reference through the existing supervisor lifecycle API,
            # only after every owned call physically joined. No Core run exists,
            # no Core state/hold is edited, and no preceding assertion uses this.
            h.service.router_workers.reopen()
        h.service.stop()
