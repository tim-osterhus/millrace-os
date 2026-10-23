"""Two real SDK client processes share existing OS/Core request authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn

from millrace_os.mcp_auth import HTTPGuard, ResourceVerifier
from millrace_os.mcp_bridge import Bridge
from millrace_os.mcp_client import BackendClient
from millrace_os.mcp_contracts import PROTOCOL, MCPConfig, schema_bundle


def configuration(h):
    return MCPConfig.model_validate(
        {
            "format": "millrace-os-mcp-config/v1",
            "transport": "stdio",
            "os_origin": h.service.browser_endpoint.rstrip("/"),
            "os_credential_file": str(h.config.credential_path),
            "homes": ["default"],
        }
    )


def direct(config, name, arguments):
    async def call():
        backend = BackendClient(config)
        try:
            await backend.startup()
            bridge = Bridge(config, backend)
            bridge._admitted = lambda: True
            return await bridge.call_tool(name, arguments)
        finally:
            await backend.close()

    return asyncio.run(call())


@contextmanager
def http_bridge(config, signing, port):
    _, jwk, token = signing
    values = config.model_dump(exclude_unset=True) | {
        "transport": "streamable-http",
        "resource_url": "https://mcp.example/mcp",
        "issuer_url": "https://issuer.example",
        "jwks_url": "https://issuer.example/jwks",
        "trusted_subjects": ["operator"],
        "allowed_hosts": ["mcp.example"],
        "allowed_origins": ["https://client.example"],
        "listen_port": port,
    }
    http_config = MCPConfig.model_validate(values)
    server = None
    failures = []
    ready = threading.Event()

    async def run():
        nonlocal server
        backend = BackendClient(config)
        verifier = ResourceVerifier(
            http_config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": [jwk]})
            ),
        )
        try:
            await backend.startup()
            bridge = Bridge(http_config, backend, verifier)
            server = uvicorn.Server(
                uvicorn.Config(
                    HTTPGuard(bridge.streamable_http_app(), http_config),
                    host="127.0.0.1",
                    port=port,
                    access_log=False,
                    log_level="critical",
                    proxy_headers=False,
                )
            )
            ready.set()
            await server.serve()
        finally:
            await backend.close()
            await verifier.close()

    def own():
        try:
            asyncio.run(run())
        except BaseException as error:
            failures.append(error)
            ready.set()

    thread = threading.Thread(target=own, name="mcp-fixture-http")
    thread.start()
    try:
        assert ready.wait(5)
        deadline = time.monotonic() + 5
        while server and not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server and server.started and not failures
        yield {
            "transport": "streamable-http",
            "url": f"http://127.0.0.1:{port}/mcp",
            "token": token(),
        }
    finally:
        if server:
            server.should_exit = True
        thread.join(8)
        assert not thread.is_alive() and not failures


def clients(jobs):
    peer = Path(__file__).with_name("sdk_client_peer.py")
    processes = [
        subprocess.Popen(
            [sys.executable, str(peer)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        for _ in jobs
    ]
    results = []
    try:
        for process, job in zip(processes, jobs, strict=True):
            process.stdin.write(json.dumps(job))
            process.stdin.close()
            process.stdin = None
        for process in processes:
            out, err = process.communicate(timeout=20)
            assert process.returncode == 0, err
            results.append(json.loads(out))
        assert len({r["pid"] for r in results}) == len(jobs)
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def create_request(h):
    workflow, project = h.flow(wait=True)
    return h.request(
        "create_instance",
        version=0,
        payload={
            "workflow_file": str(workflow / "workflow.json"),
            "project_root": str(project),
            "path": [str(Path(sys.executable).parent), "/usr/bin", "/bin"],
            "env_allowlist": [],
            "host_paths": {},
        },
    ), project


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_two_independent_sdk_clients_over_each_transport(
    mcp_harness, signing, tmp_path, transport, record_property, mcp_port
):
    h = mcp_harness
    config = configuration(h)
    request, project = create_request(h)
    path = tmp_path / "bridge.json"
    path.write_text(config.model_dump_json(exclude_unset=True))
    calls = [
        {"name": "millrace_v1_discover", "arguments": {}},
        {
            "name": "millrace_v1_request",
            "arguments": {"home": "default", "request": request},
        },
        {
            "name": "millrace_v1_instances",
            "arguments": {"home": "default", "extra": "refuse"},
        },
    ]

    def exercise(base):
        job = base | {"calls": calls}
        results = clients([job, job])
        receipt = h.send(request)
        instance = receipt["instance_id"]
        start = h.request(
            "start_run", instance, version=1, payload={"edit_mode": "locked"}
        )
        started = clients(
            [
                base
                | {
                    "calls": [
                        {
                            "name": "millrace_v1_request",
                            "arguments": {"home": "default", "request": start},
                        }
                    ]
                }
            ]
        )[0]
        record_property(
            "sdk_client_pids",
            json.dumps([r["pid"] for r in results] + [started["pid"]]),
        )
        return results, receipt, start, h.send(start)["run_id"]

    if transport == "stdio":
        results, receipt, start, run = exercise(
            {"transport": "stdio", "config": str(path)}
        )
    else:
        with http_bridge(config, signing, mcp_port) as base:
            results, receipt, start, run = exercise(base)
    assert all(r["protocol"] == PROTOCOL for r in results)
    assert all(
        [{"name": t["name"], "inputSchema": t["inputSchema"]} for t in r["tools"]]
        == schema_bundle()["tools"]
        for r in results
    )
    for result in results:
        observed = result["results"][1]["structuredContent"]["os"]
        assert observed["request_id"] == request["request_id"]
        assert observed["request_digest"] == receipt["request_digest"]
        assert json.loads(result["results"][1]["content"][0]["text"])["os"] == observed
        assert (
            result["results"][2]["structuredContent"]["access_error"]["code"]
            == "invalid_arguments"
        )
    assert (
        receipt["request_digest"]
        == hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    instance = receipt["instance_id"]
    # Both SDK clients and their transports are already closed; OS work persists.
    h.until(lambda: (project / "started").exists())
    (project / "release").write_text("complete after transport closure")
    final = h.settled(instance, run, "succeeded")
    later = direct(
        config,
        "millrace_v1_run",
        {"home": "default", "instance_id": instance, "run_id": run},
    )
    assert later["os"]["data"] == final
    assert direct(config, "millrace_v1_request", {"home": "default", "request": start})[
        "os"
    ]["core_receipt"] == h.send(start)


def test_shared_clients_replay_and_conflict(mcp_harness):
    h = mcp_harness
    config = configuration(h)
    request, _ = create_request(h)
    first = direct(
        config, "millrace_v1_request", {"home": "default", "request": request}
    )
    receipt = h.send(request)
    assert first["os"]["request_digest"] == receipt["request_digest"]
    repeat = direct(
        config, "millrace_v1_request", {"home": "default", "request": request}
    )
    assert repeat["os"]["core_receipt"] == receipt
    changed = request | {"payload": request["payload"] | {"env_allowlist": ["CHANGED"]}}
    conflict = direct(
        config, "millrace_v1_request", {"home": "default", "request": changed}
    )
    assert conflict["access_error"]["status"] == 409
    assert len(h.http.get("/api/v1/router/instances?home=default").json()["data"]) == 1


def test_stale_versions_and_wrong_targets(mcp_harness):
    h = mcp_harness
    config = configuration(h)
    request, _ = create_request(h)
    receipt = h.send(request)
    started = h.start_run(receipt["instance_id"])
    (h.projects[0] / "release").write_text("finish before stale replay")
    h.settled(receipt["instance_id"], started["run_id"], "succeeded")
    stale = h.request(
        "start_run", receipt["instance_id"], version=1, payload={"edit_mode": "locked"}
    )
    result = direct(
        config, "millrace_v1_request", {"home": "default", "request": stale}
    )
    assert "os" in result, result
    assert result["os"]["core_receipt"]["code"] == "stale_state_version"
    wrong = direct(
        config,
        "millrace_v1_run",
        {
            "home": "default",
            "instance_id": receipt["instance_id"],
            "run_id": uuid4().hex,
        },
    )
    assert "access_error" in wrong
    wrong_home = direct(config, "millrace_v1_instances", {"home": "not-configured"})
    assert wrong_home["access_error"]["code"] == "invalid_arguments"


def test_create_restart_requires_exact_post_replay(mcp_harness):
    h = mcp_harness
    config = configuration(h)
    request, _ = create_request(h)
    saved_request = json.loads(json.dumps(request))
    first = direct(
        config, "millrace_v1_request", {"home": "default", "request": request}
    )
    receipt = h.send(request)
    h.stop()
    h.start()

    def replay_observation():
        after = direct(
            configuration(h),
            "millrace_v1_request",
            {"home": "default", "request": request},
        )
        assert request == saved_request
        assert after["os"]["request_id"] == request["request_id"]
        assert after["os"]["request_digest"] == receipt["request_digest"]
        assert after["os"]["instance_id"] in {None, receipt["instance_id"]}
        assert after["os"]["run_id"] is None
        return after if after["os"]["core_receipt"] is not None else None

    after = h.until(replay_observation)
    assert after["os"]["core_receipt"] == receipt
    assert first["os"]["request_digest"] == after["os"]["request_digest"]
    assert len(h.http.get("/api/v1/router/instances?home=default").json()["data"]) == 1
    missing = direct(
        configuration(h),
        "millrace_v1_receipt",
        {"home": "default", "request_id": request["request_id"]},
    )
    assert missing["access_error"]["code"] == "invalid_arguments"


def test_restart_disconnect_keeps_exact_saved_identity(mcp_harness):
    h = mcp_harness
    config = configuration(h)
    request, _ = create_request(h)
    receipt = h.send(request)
    instance = receipt["instance_id"]
    start = h.request("start_run", instance, version=1, payload={"edit_mode": "locked"})
    direct(config, "millrace_v1_request", {"home": "default", "request": start})
    started = h.send(start)
    # Close the observer's client; Core/OS continue to own this exact run.
    project = h.projects[0]
    (project / "release").write_text("finish")
    h.settled(instance, started["run_id"], "succeeded")
    h.stop()
    h.start()
    replay = direct(
        configuration(h), "millrace_v1_request", {"home": "default", "request": start}
    )
    assert replay["os"]["core_receipt"] == started
    read = direct(
        configuration(h),
        "millrace_v1_receipt",
        {
            "home": "default",
            "request_id": start["request_id"],
            "instance_id": instance,
            "run_id": started["run_id"],
        },
    )
    assert read["os"]["core_receipt"] == started


def test_simultaneous_targeted_controls_keep_one_saved_identity(
    mcp_harness, signing, record_property, mcp_port
):
    h = mcp_harness
    config = configuration(h)
    request, project = create_request(h)
    instance = h.send(request)["instance_id"]
    started = h.start_run(instance)
    run = started["run_id"]
    h.until(lambda: (project / "started").exists())
    pause = h.request("pause_run", instance, run)
    observed_receipts = []
    with http_bridge(config, signing, mcp_port) as base:
        job = base | {
            "calls": [
                {
                    "name": "millrace_v1_request",
                    "arguments": {"home": "default", "request": pause},
                }
            ]
        }
        results = clients([job, job])
        record_property(
            "sdk_targeted_client_pids", json.dumps([r["pid"] for r in results])
        )
        for result in results:
            observation = result["results"][0]["structuredContent"]["os"]
            assert observation["request_id"] == pause["request_id"]
            assert (
                observation["instance_id"] == instance and observation["run_id"] == run
            )
            receipt = observation["core_receipt"]
            assert receipt["status"] in {"queued", "applied"}
            assert receipt["request_id"] == pause["request_id"]
            assert receipt["instance_id"] == instance and receipt["run_id"] == run
            assert receipt["request_digest"] == observation["request_digest"]
            observed_receipts.append(receipt)
    # Closing the bridge and clients preserves the saved control identity.
    (project / "release").write_text("finish active invocation")
    applied = h.send(pause)
    assert applied["status"] == "applied"
    for receipt in observed_receipts:
        assert receipt["request_digest"] == applied["request_digest"]
        if receipt["status"] == "applied":
            assert receipt == applied
    paused = h.settled(instance, run, "paused")
    replay = direct(
        config, "millrace_v1_request", {"home": "default", "request": pause}
    )
    assert replay["os"]["core_receipt"] == applied
    resume = h.request("resume_run", instance, run, version=paused["state_version"])
    direct(config, "millrace_v1_request", {"home": "default", "request": resume})
    h.send(resume)
    h.settled(instance, run, "succeeded")
    assert (project / "checked.txt").read_text() == "checked 42\n"
