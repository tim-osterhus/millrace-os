"""Plane controls, recurring handoff and restart truth through public OS HTTP."""

from __future__ import annotations

import copy
import json
from pathlib import Path


def _node(script, outcome, action):
    return {
        "invoke": {
            "binding": "python",
            "timeout_seconds": 10,
            "config": {"script": script},
        },
        "result": "json-v1",
        "outcomes": [outcome],
        "on": {outcome: action},
        "on_error": {"pause": {"reason": "command_error", "resume": "step"}},
        "on_unknown": {"pause": {"reason": "unknown_result", "resume": "step"}},
    }


def _two_planes(h, name="planes"):
    workflow, project = h.flow(name)
    graph = json.loads((workflow / "workflow.json").read_bytes())
    nodes = {
        "step": _node("step.py", "READY", {"goto": "after"}),
        "after": _node("after.py", "READY", {"goto": "finish"}),
        "finish": _node("finish.py", "ACCEPT", {"finish": "succeeded"}),
    }
    for key, node in nodes.items():
        node["on_error"]["pause"]["resume"] = key
        node["on_unknown"]["pause"]["resume"] = key
    graph["limits"]["max_parallel_invocations"] = 2
    graph["planes"] = {
        name: {"workspace": name, "start": "step", "nodes": copy.deepcopy(nodes)}
        for name in ("alpha", "beta")
    }
    graph["planes"]["alpha"]["nodes"]["step"]["invoke"]["timeout_seconds"] = 9
    graph["control_files"] = ["step.py", "after.py", "finish.py"]
    for stage, value in [("step", 40), ("after", 2)]:
        (workflow / f"{stage}.py").write_text(
            "import json,os,time\nfrom pathlib import Path\n"
            + f"Path('{stage}-started').write_text(str(os.getpid()))\n"
            + "end=time.monotonic()+8\n"
            + f"while not Path('{stage}-release').exists() and time.monotonic()<end:\n"
            + " time.sleep(.01)\n"
            + f"Path('{stage}-output').write_text('{value}')\n"
            + "print(json.dumps({'outcome':'READY'}))\n"
        )
    (workflow / "finish.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "value=int(Path('step-output').read_text())+int(Path('after-output').read_text())\n"
        "assert value==42\n"
        "Path('checked.txt').write_text(Path.cwd().name+':'+str(value)+'\\n')\n"
        "print(json.dumps({'outcome':'ACCEPT','refs':['checked.txt']}))\n"
    )
    (workflow / "workflow.json").write_text(json.dumps(graph))
    for plane in graph["planes"]:
        (project / plane).mkdir()
    return workflow, project


def _release(project):
    for plane in ("alpha", "beta"):
        for stage in ("step", "after"):
            (project / plane / f"{stage}-release").write_text("owned command release")


def _joined(h):
    h.until(lambda: all(row["joined"] for row in h.service.router_workers.snapshot()))


def _stop_sets(receipt, planes):
    assert receipt["target_planes"] == sorted(planes)
    assert receipt["active_invocations"] == sorted(set(receipt["active_invocations"]))
    assert all(isinstance(value, str) for value in receipt["active_invocations"])
    assert all(isinstance(value, str) for value in receipt["target_planes"])


def test_targeted_planes_two_instances_run_gate_restart_and_authority(
    router_harness, capsys
):
    from millrace_plus.router import control

    from millrace_os.cli import main

    h = router_harness
    workflow, project = _two_planes(h)
    other_workflow, other_project = h.flow("independent")
    instance = h.create(workflow, project)
    other = h.create(other_workflow, other_project)
    other_run = h.start_run(other)["run_id"]
    run = h.start_run(instance)["run_id"]
    try:
        h.until(
            lambda: all(
                (project / p / "step-started").exists() for p in ("alpha", "beta")
            )
        )
        live = h.read(instance, run)
        assert {i["plane_id"] for i in live["invocations"]} == {"alpha", "beta"}
        assert all(i["node"] == "step" for i in live["invocations"])
        pause_request = h.request("pause_plane", instance, run, plane="alpha")
        pause = h.send(pause_request)
        assert pause["status"] == "applied" and pause["plane_id"] == "alpha"
        _stop_sets(pause, ["alpha"])
        assert set(pause["active_invocations"]) == {
            i["id"] for i in live["invocations"] if i["plane_id"] == "alpha"
        }
        (project / "beta/step-release").write_text("beta advances independently")
        h.until(lambda: (project / "beta/after-started").exists())
        progressing = h.read(instance, run)
        assert progressing["planes"]["alpha"]["status"] == "paused"
        assert any(
            i["plane_id"] == "beta" and i["node"] == "after"
            for i in progressing["invocations"]
        )
        assert not (project / "alpha/after-started").exists()
        sibling = h.settled(other, other_run, "succeeded")
        assert (other_project / "checked.txt").read_text() == "checked 42\n"
        run_pause = h.send(h.request("pause_run", instance, run))
        assert run_pause["status"] == "applied" and run_pause["plane_id"] is None
        _stop_sets(run_pause, ["alpha", "beta"])
        assert set(run_pause["active_invocations"]) == {
            i["id"]
            for i in progressing["invocations"]
            if i["status"] in {"reserved", "active"}
        }
        assert len(run_pause["active_invocations"]) == 2
        (project / "alpha/step-release").write_text("drain alpha under both pauses")
        (project / "beta/after-release").write_text("drain beta result")
        paused = h.settled(instance, run, "paused")
        _joined(h)
        paused = h.read(instance, run)
        assert paused["run_gate"]["reason"] == "operator_pause"
        assert not paused["inflight_invocations"]
        assert all(i["writer_state"] == "stopped" for i in paused["invocations"])
        # A receipt's immutable historical ID strings are distinct from the
        # current writer observation/version; no historical inspect argument.
        assert all(isinstance(i, str) for i in run_pause["active_invocations"])
        assert h.read(instance, run) == control.inspect_run(
            instance, run, state_home=h.home
        )
        validation = h.post(
            "validate",
            {
                "home": "default",
                "instance_id": instance,
                "run_id": run,
                "draft_id": None,
            },
        )
        assert (
            validation["source_provider"] == "core"
            and validation["authority"] == "core-state-home"
        )
        data = validation["data"]
        assert (
            data["validation_context"]["run_state_version"] == paused["state_version"]
        )
        assert set(data["resolved_nodes"]) == {"alpha", "beta"}
        assert data == control.validate_revision(
            instance, run_id=run, state_home=h.home
        )
        assert (
            data["resolved_nodes"]["alpha"]["step"]["common"]["effective"][
                "timeout_seconds"
            ]
            == 9
        )
        assert (
            data["resolved_nodes"]["beta"]["step"]["common"]["effective"][
                "timeout_seconds"
            ]
            == 10
        )
        for plane in ("alpha", "beta"):
            assert "step" in data["resolved_nodes"][plane]
        args = [
            "--home",
            "default",
            "--instance",
            instance,
            "--run",
            run,
            "--port",
            str(h.config.port),
            "--config-dir",
            str(h.config.config_dir),
            "--state-dir",
            str(h.config.state_dir),
        ]
        assert main(["router", "validate", *args]) == 0
        assert json.loads(capsys.readouterr().out)["data"] == data
        h.stop()
        h.start()
        assert h.read(instance, run) == paused
        assert h.read(other, other_run) == sibling
        h.send(h.request("resume_run", instance, run, version=paused["state_version"]))
        h.until(
            lambda: (
                v
                if (v := h.read(instance, run))["planes"]["beta"]["status"]
                == "succeeded"
                else None
            )
        )
        _joined(h)
        beta_done = h.read(instance, run)
        assert beta_done["run_gate"] is None
        assert beta_done["planes"]["alpha"]["status"] == "paused"
        assert not (project / "alpha/after-started").exists()
        h.send(
            h.request(
                "resume_plane",
                instance,
                run,
                plane="alpha",
                version=beta_done["state_version"],
            )
        )
        h.until(lambda: (project / "alpha/after-started").exists())
        (project / "alpha/after-release").write_text("explicit alpha continuation")
        final = h.settled(instance, run, "succeeded")
        _joined(h)
        assert final["count"] == 6
        for plane in ("alpha", "beta"):
            assert (project / plane / "checked.txt").read_text() == plane + ":42\n"
        assert h.send(pause_request) == pause
        assert h.read(other, other_run) == sibling
        (h.root / "plane-controls-proof.json").write_text(
            json.dumps(
                {
                    "target_receipt": pause,
                    "run_receipt": run_pause,
                    "paused_current_observation": paused,
                    "current_observation_time": validation["observed_at"],
                    "validation": data,
                    "final": final,
                    "independent_instance": sibling,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    finally:
        _release(project)


def test_recurring_seam_selection_and_consumer_progress_survive_restart(router_harness):
    h = router_harness
    workflow, project = h.flow("seams")
    graph = json.loads((workflow / "workflow.json").read_bytes())
    emit = _node("emit.py", "READY", {"goto": "step"})
    emit["outcomes"] = ["READY", "END"]
    emit["on"]["END"] = {"finish": "succeeded"}
    consume = _node(
        "consume.py", "HOLD", {"pause": {"reason": "review", "resume": "step"}}
    )
    consume["outcomes"] = ["HOLD", "MORE", "DONE"]
    consume["on"].update(MORE={"goto": "step"}, DONE={"finish": "succeeded"})
    graph["planes"] = {
        "alpha": {"workspace": "alpha", "start": "step", "nodes": {"step": emit}},
        "beta": {"workspace": "beta", "start": "step", "nodes": {"step": consume}},
    }
    graph["limits"]["max_parallel_invocations"] = 2
    graph["control_files"] = ["emit.py", "consume.py"]
    graph["seams"] = {
        "delivery": {
            "producer": {"plane": "alpha", "node": "step", "outcome": "READY"},
            "consumer": {"plane": "beta", "node": "step"},
        }
    }
    (workflow / "emit.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "p=Path('counter');n=int(p.read_text())+1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "Path('value'+str(n)+'.txt').write_text(str(21*n))\n"
        "print(json.dumps({'outcome':'READY' if n<=2 else 'END',"
        "'refs':['value'+str(n)+'.txt']}))\n"
    )
    (workflow / "consume.py").write_text(
        "import json,os\nfrom pathlib import Path\n"
        "item=json.loads(Path(os.environ['MILLRACE_SEAM_INPUT']).read_text())\n"
        "n=item['sequence'];assert item['consumer_plane_id']=='beta'\n"
        "source=Path(item['producer_workspace'])/('value'+str(n)+'.txt')\n"
        "value=int(source.read_text());assert value==21*n\n"
        "p=Path('seen');first=not p.exists();p.open('a').write(str(n)+'\\n')\n"
        "Path('checked'+str(n)+'.txt').write_text(str(value*2))\n"
        "outcome='HOLD' if first else 'MORE' if n==1 else 'DONE'\n"
        "print(json.dumps({'outcome':outcome}))\n"
    )
    for plane in ("alpha", "beta"):
        (project / plane).mkdir()
    (workflow / "workflow.json").write_text(json.dumps(graph))
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    before = h.until(
        lambda: (
            v
            if (v := h.read(instance, run))["quiescent"]
            and v["planes"]["alpha"]["status"] == "succeeded"
            and v["planes"]["beta"]["status"] == "paused"
            else None
        )
    )
    _joined(h)
    before = h.read(instance, run)
    assert before["planes"]["beta"]["selected"] == {"delivery": 1}
    assert before["planes"]["beta"]["progress"] == {}
    assert [o["sequence"] for o in before["seam_occurrences"]] == [1, 2]
    assert before["seam_acknowledgements"] == []
    h.stop()
    h.start()
    assert h.read(instance, run) == before
    h.send(
        h.request(
            "resume_plane", instance, run, plane="beta", version=before["state_version"]
        )
    )
    final = h.settled(instance, run, "succeeded")
    _joined(h)
    assert final["count"] == 6
    assert final["planes"]["beta"]["progress"] == {"delivery": 2}
    assert final["planes"]["beta"]["selected"] == {}
    assert final["seam_occurrences"] == before["seam_occurrences"]
    assert len(final["seam_acknowledgements"]) == 2
    consumers = [i for i in final["invocations"] if i["plane_id"] == "beta"]
    assert (
        consumers[0]["launch"]["seam_input"]["content"]
        == consumers[1]["launch"]["seam_input"]["content"]
    )
    assert (
        consumers[0]["launch"]["seam_input"]["path"]
        != consumers[1]["launch"]["seam_input"]["path"]
    )
    assert (project / "beta/seen").read_text().splitlines() == ["1", "1", "2"]
    assert (project / "beta/checked1.txt").read_text() == "42"
    assert (project / "beta/checked2.txt").read_text() == "84"
    (h.root / "seam-restart-proof.json").write_text(
        json.dumps({"before": before, "final": final}, sort_keys=True, indent=2) + "\n"
    )


def test_two_plane_unknown_writers_survive_explicit_os_recovery(router_harness):
    import os
    import signal
    import subprocess
    import sys
    from dataclasses import replace

    import httpx

    from millrace_os.credentials import CredentialStore

    h = router_harness
    workflow, project = _two_planes(h, "uncertain")
    sibling_flow, sibling_project = h.flow("surviving-instance")
    instance = h.create(workflow, project)
    sibling = h.create(sibling_flow, sibling_project)
    sibling_run = h.start_run(sibling)["run_id"]
    sibling_before = h.settled(sibling, sibling_run, "succeeded")
    _joined(h)
    h.stop()
    launch = """
import os,sys,time
from pathlib import Path
from millrace_os.config import ApplicationConfig
from millrace_os.service import Service
root,port=sys.argv[1:];root=Path(root)
service=Service(ApplicationConfig(config_dir=root/'config',state_dir=root/'os-state',credential_path=root/'config/credentials.json',port=int(port),router_state_homes={'default':root/'core'}))
service.start()
(root/'plane-child-ready').write_text(str(os.getpid()))
while True: time.sleep(.05)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", launch, str(h.root), str(h.config.port)],
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
        },
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
    pids = {}
    try:
        h.until(lambda: (h.root / "plane-child-ready").exists())
        assert int((h.root / "plane-child-ready").read_text()) == child.pid
        run = h.start_run(instance)["run_id"]
        h.until(
            lambda: all(
                (project / p / "step-started").exists() for p in ("alpha", "beta")
            )
        )
        pids = {
            p: int((project / p / "step-started").read_text())
            for p in ("alpha", "beta")
        }

        def started():
            value = h.read(instance, run)
            return (
                value
                if {i["plane_id"]: i["pid"] for i in value["invocations"]} == pids
                else None
            )

        before = h.until(started)
        assert {i["plane_id"]: i["pid"] for i in before["invocations"]} == pids
        assert before["count"] == 2 and not before["quiescent"]
        assert child.poll() is None and child.pid not in pids.values()
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
        old_state = {
            p.name: p.read_bytes() for p in h.config.state_dir.iterdir() if p.is_file()
        }
        assert any(name.endswith("-wal") for name in old_state)
        h.service.stop()
        h.http.close()
        h.config = replace(h.config, state_dir=h.root / "fresh-plane-os-state")
        h.start()
        assert {
            p.name: p.read_bytes()
            for p in (h.root / "os-state").iterdir()
            if p.is_file()
        } == old_state
        assert h.service.router_workers.snapshot() == []
        assert h.read(instance, run) == before
        assert h.read(sibling, sibling_run) == sibling_before
        recovered = h.send(
            h.request("recover_run", instance, run, version=before["state_version"])
        )
        assert (
            recovered["status"] == "refused"
            and recovered["code"] == "stale_state_version"
        )
        _joined(h)
        unknown = h.read(instance, run)
        assert (
            unknown["status"] == "paused"
            and unknown["pause_reason"] == "writer_unknown"
        )
        assert not unknown["quiescent"] and unknown["count"] == 2
        assert {
            i["plane_id"]
            for i in unknown["invocations"]
            if i["writer_state"] != "stopped"
        } == {"alpha", "beta"}
        assert unknown["startup_deadline"] == before["startup_deadline"]
        assert unknown["cap"] == before["cap"]
        unsafe = h.send(
            h.request("resume_run", instance, run, version=unknown["state_version"])
        )
        assert unsafe["status"] == "refused" and unsafe["code"] == "writer_unknown"
        _joined(h)
        assert not any(
            (project / p / "after-started").exists() for p in ("alpha", "beta")
        )
        _release(project)

        def exited():
            for pid in pids.values():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                return False
            return True

        h.until(exited, timeout=5)
        reconciliations = []
        for plane in ("alpha", "beta"):
            current = h.read(instance, run)
            invocation = next(
                i for i in current["invocations"] if i["plane_id"] == plane
            )
            receipt = h.send(
                h.request(
                    "reconcile_invocation",
                    instance,
                    run,
                    plane=plane,
                    version=current["state_version"],
                    payload={
                        "invocation_id": invocation["id"],
                        "reason": "verified exact owned command PID exited",
                        "stopped": True,
                    },
                )
            )
            assert receipt["status"] == "applied" and receipt["plane_id"] == plane
            reconciliations.append(receipt)
            _joined(h)
        cancelled = h.send(h.request("cancel_run", instance, run))
        assert cancelled["status"] == "applied"
        _stop_sets(cancelled, ["alpha", "beta"])
        final = h.settled(instance, run, "cancelled")
        _joined(h)
        assert final["count"] == 2
        assert all(i["writer_state"] == "stopped" for i in final["invocations"])
        assert h.read(sibling, sibling_run) == sibling_before
        (h.root / "two-plane-uncertainty-proof.json").write_text(
            json.dumps(
                {
                    "owned_os_pid": child.pid,
                    "owned_command_pids": pids,
                    "before": before,
                    "unknown": unknown,
                    "unsafe_resume": unsafe,
                    "reconciliations": reconciliations,
                    "cancel_receipt": cancelled,
                    "final": final,
                    "independent_instance": sibling_before,
                    "recovery_scope": (
                        "Old OS WAL503 retained; explicit fresh OS state "
                        "same Core home, no automatic draft/session recovery."
                    ),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        _release(project)
        h.until(lambda: all(_pid_exited(pid) for pid in pids.values()), timeout=5)


def _pid_exited(pid):
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_graceful_sigterm_drains_two_planes_and_same_state_resumes_exactly_once(
    router_harness,
):
    import hashlib
    import os
    import signal
    import subprocess
    import sys

    import httpx

    from millrace_os.credentials import CredentialStore

    h = router_harness
    workflow, project = _two_planes(h, "sigterm-planes")
    instance = h.create(workflow, project)
    _joined(h)
    original_state = h.config.state_dir
    h.stop()
    log_path = h.root / "graceful-sigterm-service.log"
    cli_args = [
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(h.config.port),
        "--config-dir",
        str(h.config.config_dir),
        "--state-dir",
        str(original_state),
        "--router-state-home",
        f"default={h.home}",
        "--no-pid-file",
    ]
    module_path = Path(__file__).parents[2] / "src/millrace_os/router_workers.py"
    expected_hash = hashlib.sha256(module_path.read_bytes()).hexdigest()
    attestation = h.root / "sigterm-import-attestation.json"
    launch = """
import hashlib,json,os,sys
from pathlib import Path
import millrace_os.router_workers as workers
from millrace_os.cli import main
out,expected,path=sys.argv[1:4]
actual=Path(workers.__file__).resolve()
assert actual==Path(path).resolve()
assert hashlib.sha256(actual.read_bytes()).hexdigest()==expected
Path(out).write_text(json.dumps({'pid':os.getpid(),'module_path':str(actual),'sha256':expected,'cli_args':sys.argv[4:]}))
sys.argv=['millrace-os',*sys.argv[4:]]
raise SystemExit(main())
"""
    argv = [
        sys.executable,
        "-c",
        launch,
        str(attestation),
        expected_hash,
        str(module_path),
        *cli_args,
    ]
    with log_path.open("wb") as log:
        child = subprocess.Popen(
            argv,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
            },
            stdout=log,
            stderr=subprocess.STDOUT,
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
    try:
        h.until(lambda: "READY " in log_path.read_text())
        assert child.poll() is None
        imported = json.loads(attestation.read_text())
        assert imported["pid"] == child.pid
        assert imported["module_path"] == str(module_path.resolve())
        assert imported["sha256"] == expected_hash
        run = h.start_run(instance)["run_id"]
        h.until(
            lambda: all(
                (project / plane / "step-started").exists()
                for plane in ("alpha", "beta")
            )
        )

        def started():
            value = h.read(instance, run)
            invocations = value["invocations"]
            return (
                value
                if len(invocations) == 2
                and all(
                    type(row["pid"]) is int and row["pid"] > 0 for row in invocations
                )
                else None
            )

        before = h.until(started)
        assert before["count"] == 2 and not before["quiescent"]
        identities = {row["plane_id"]: row["id"] for row in before["invocations"]}
        pids = {row["plane_id"]: row["pid"] for row in before["invocations"]}
        assert set(identities) == {"alpha", "beta"}
        assert all(row["writer_state"] == "running" for row in before["invocations"])
        owner_path = project / ".millrace-router-owner/owner.json"
        retained_hold = owner_path.read_bytes()
        assert json.loads(retained_hold)["run_id"] == run
        os.kill(child.pid, signal.SIGTERM)

        def stopped_at_gate():
            value = h.read(instance, run)
            return value if value["run_gate"] is not None else None

        held = h.until(stopped_at_gate)
        assert child.poll() is None, "SIGTERM must wait for both owned writers"
        assert held["count"] == 2 and not held["quiescent"]
        assert not held["cancel_requested"]
        stops = [row for row in held["controls"] if row["operation"] == "pause_run"]
        assert len(stops) == 1
        _stop_sets(stops[0], ["alpha", "beta"])
        assert set(stops[0]["active_invocations"]) == set(identities.values())
        for plane in ("alpha", "beta"):
            assert not (project / plane / "step-output").exists()
            (project / plane / "step-release").write_text("complete graceful drain")
        assert child.wait(timeout=8) == 0
        assert all(_pid_exited(pid) for pid in pids.values())
        h.http.close()
        h.start()
        assert h.config.state_dir == original_state and h.service.state is not None
        paused = h.read(instance, run)
        assert paused["status"] == "paused" and paused["quiescent"]
        assert paused["count"] == 2 and not paused["inflight_invocations"]
        assert {
            row["plane_id"]: row["id"] for row in paused["invocations"]
        } == identities
        assert all(row["writer_state"] == "stopped" for row in paused["invocations"])
        assert all(
            row["effective_writer"] == "stopped" for row in paused["completions"]
        )
        assert owner_path.read_bytes() == retained_hold
        assert all(not (project / p / "after-started").exists() for p in identities)
        for plane in identities:
            (project / plane / "after-release").write_text("explicit continuation")
        resume = h.request("resume_run", instance, run, version=paused["state_version"])
        receipt = h.send(resume)
        assert receipt["status"] == "applied"
        final = h.settled(instance, run, "succeeded")
        assert final["count"] == 6
        assert not owner_path.exists()
        assert len({row["id"] for row in final["invocations"]}) == 6
        for plane in identities:
            rows = [row for row in final["invocations"] if row["plane_id"] == plane]
            assert [row["node"] for row in rows] == ["step", "after", "finish"]
            assert rows[0]["id"] == identities[plane]
            assert (project / plane / "checked.txt").read_text() == plane + ":42\n"
        assert h.send(resume) == receipt
        assert h.read(instance, run)["invocations"] == final["invocations"]
        (h.root / "graceful-sigterm-proof.json").write_text(
            json.dumps(
                {
                    "owned_pid": child.pid,
                    "source_import": imported,
                    "retained_paused_hold": json.loads(retained_hold),
                    "argv": argv,
                    "same_os_state": str(original_state),
                    "before": before,
                    "held": held,
                    "paused_after_restart": paused,
                    "explicit_resume_receipt": receipt,
                    "final": final,
                },
                indent=2,
            )
        )
    finally:
        _release(project)
        if child.poll() is None:
            os.kill(child.pid, signal.SIGTERM)
            child.wait(timeout=10)
        if h.service.lifecycle == "stopped":
            h.http.close()
            h.start()
