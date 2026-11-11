"""Portable, real owned-child lifecycle without any native provider."""

import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from millrace_os.config import ApplicationConfig, ConfigurationError
from millrace_os.demo_service import (
    DemoServiceError,
    _ChildOwner,
    start_demo_service,
)


def private_root(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path = path.resolve()
    info = path.stat()
    marker = path / "owner.json"
    marker.write_text(
        json.dumps(
            {
                "format": "millrace-demo-owner/v1",
                "demo_id": secrets.token_hex(16),
                "root": str(path),
                "root_device": info.st_dev,
                "root_inode": info.st_ino,
            }
        )
    )
    marker.chmod(0o600)
    return path


@pytest.fixture
def owned(tmp_path):
    handles = []

    def start(name="demo"):
        value = start_demo_service(
            private_root(tmp_path / name), owner_token=secrets.token_bytes(32)
        )
        handles.append(value)
        return value

    yield start
    for handle in handles:
        if not handle.shutdown_result["child_exited"]:
            handle.finish(cancel=True)
        assert handle.wait(), handle.shutdown_result
        handle.close()


def test_two_owned_services_keep_distinct_bound_ports_and_unrelated_socket(owned):
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen()
        address = unrelated.getsockname()
        first, second = owned("one"), owned("two")
        assert first.endpoint != second.endpoint
        with httpx.Client(trust_env=False) as client:
            for handle in (first, second):
                reply = client.get(
                    handle.endpoint + "/api/v1/identity", headers=handle.auth_headers
                )
                assert reply.status_code == 200
                assert reply.json()["data"]["authentication"] == "bearer"
            first.finish(cancel=False)
            first.finish(cancel=False)
            assert first.wait()
            assert client.get(second.endpoint + "/health").status_code == 200
        assert unrelated.getsockname() == address
        assert unrelated.fileno() >= 0


def test_bound_target_private_context_and_finish_intent(owned):
    handle = owned()
    with httpx.Client(
        base_url=handle.endpoint, headers=handle.auth_headers, trust_env=False
    ) as client:
        pending = client.get("/api/v1/demo/context")
        assert pending.status_code == 503
        assert pending.json()["code"] == "demo_context_pending"
        url = handle.bootstrap_url(instance_id="a" * 32, run_id="b" * 32)
        assert "/ui/stock/?home=demo&instance=" in url and "#" in url
        assert "Bearer" not in url
        with pytest.raises(DemoServiceError):
            handle.bootstrap_url(instance_id="c" * 32, run_id="b" * 32)
        context = client.get("/api/v1/demo/context").json()
        assert context["instance_id"] == "a" * 32
        assert context["accepted_output_refs"] == []
        for _ in range(2):
            assert client.post("/api/v1/demo/finish", json={}).json() == {
                "format": "millrace-os-demo-finish/v1",
                "requested": True,
            }
        # Finish acknowledges the parent's capture/export intent; it does not
        # remove the HTTP service before the coordinator can collect evidence.
        assert client.get("/health").status_code == 200
        deadline = time.monotonic() + 2
        while not handle.finish_requested and time.monotonic() < deadline:
            time.sleep(0.01)
        assert handle.finish_requested
    handle.finish(cancel=False)
    assert handle.wait()
    result = handle.shutdown_result
    assert result["child_exited"] and result["workers_drained"]
    assert result["service_stopped"] and result["returncode"] == 0
    assert result["graph_writers_stopped"] is None
    assert (handle.root / "os-state" / "demo-shutdown.json").is_file()
    assert not (handle.root / "os-state" / "millrace-os.pid").exists()


def test_parent_channel_eof_pauses_drains_and_joins_exact_child(owned):
    handle = owned()
    handle.close()
    assert handle.wait()
    record = json.loads((handle.root / "os-state" / "demo-shutdown.json").read_text())
    shutdown = handle.shutdown_result
    assert shutdown["child_exited"] and shutdown["returncode"] == 0
    assert shutdown["service_stopped"] and shutdown["workers_drained"]
    assert shutdown["cancel_requested"] is False
    assert shutdown["graph_writers_stopped"] is None
    assert all(shutdown[key] == value for key, value in record.items())
    assert record["cancel_requested"] is False
    assert record["graph_writers_stopped"] is None
    handle.close()


def test_wait_timeout_reports_incomplete_without_signaling(owned, monkeypatch):
    handle = owned()

    def timed_out(timeout):
        raise subprocess.TimeoutExpired("exact-child", timeout)

    original = handle._process.wait
    monkeypatch.setattr(handle._process, "wait", timed_out)
    assert handle.wait(timeout=0.001) is False
    assert handle.shutdown_result["child_exited"] is False
    monkeypatch.setattr(handle._process, "wait", original)


def test_repeated_private_finish_does_not_escalate_graceful_shutdown(tmp_path):
    calls = []

    class Service:
        def request_shutdown(self, *, cancel=False):
            calls.append(cancel)

    owner = _ChildOwner(tmp_path, -1)
    message = {"kind": "finish", "id": "one", "cancel": False}
    owner.command(message, Service())
    owner.command(message, Service())
    assert calls == [False]
    owner.command({**message, "cancel": True}, Service())
    owner.command({**message, "cancel": True}, Service())
    assert calls == [False, True]


def test_owned_config_only_enables_explicit_ephemeral_port(tmp_path):
    values = dict(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credential",
        port=0,
    )
    with pytest.raises(ConfigurationError):
        ApplicationConfig(**values)
    assert ApplicationConfig(**values, allow_ephemeral_port=True).port == 0
    with pytest.raises(ConfigurationError):
        ApplicationConfig.from_environment({"MILLRACE_OS_PORT": "0"})


def test_unsafe_owner_marker_or_token_refuses_before_spawn(tmp_path, monkeypatch):
    root = private_root(tmp_path / "demo")

    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe root must refuse before spawn")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ValueError):
        start_demo_service(root, owner_token=b"short")
    owner = json.loads((root / "owner.json").read_text())
    owner["root_inode"] += 1
    (root / "owner.json").write_text(json.dumps(owner))
    with pytest.raises(DemoServiceError):
        start_demo_service(root, owner_token=os.urandom(32))
