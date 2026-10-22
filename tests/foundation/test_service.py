from __future__ import annotations

import errno
import json
import socket
import urllib.error
import urllib.request

import pytest

from millrace_os.config import ApplicationConfig
from millrace_os.service import (
    BindError,
    PortCollisionError,
    Service,
    ServiceDependencies,
    ServiceStartupError,
)


def _free_port(host: str = "127.0.0.1") -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _config(
    tmp_path, port: int | None = None, host: str = "127.0.0.1"
) -> ApplicationConfig:
    return ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credentials.json",
        host=host,
        port=_free_port(host) if port is None else port,
    )


def _get(url: str) -> tuple[int, str, str]:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return (
                response.status,
                response.headers["Content-Type"],
                response.read().decode(),
            )
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read().decode()


def test_service_serves_public_liveness_and_restarts_cleanly(tmp_path) -> None:
    config = _config(tmp_path)
    service = Service(config)

    service.start()
    try:
        assert service.browser_endpoint.startswith("http://127.0.0.1:")
        status, content_type, body = _get(service.browser_endpoint)
        assert status == 200
        assert content_type.startswith("text/html")
        assert "schema" not in body

        status, _, body = _get(service.health_endpoint)
        assert status == 200
        health = json.loads(body)
        assert health["status"] == "ok"
        assert set(health) == {"status"}

        status, _, body = _get(service.recovery_endpoint)
        assert status == 200
        assert "schema" not in body
        assert "corrupt_state" not in body
    finally:
        service.stop()

    restarted = Service(config)
    restarted.start()
    try:
        status, _, body = _get(restarted.health_endpoint)
        assert status == 200
        assert json.loads(body)["status"] == "ok"
    finally:
        restarted.stop()


def test_service_restarts_immediately_on_the_same_port(tmp_path) -> None:
    first = Service(_config(tmp_path))
    first.start()
    port = first.bound_port
    assert _get(first.health_endpoint)[0] == 200
    first.stop()

    restarted_config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credentials.json",
        host="127.0.0.1",
        port=port,
    )
    restarted = Service(restarted_config)

    restarted.start()
    restarted.stop()


def test_port_collision_is_typed_and_does_not_replace_running_service(tmp_path) -> None:
    first = Service(_config(tmp_path / "first"))
    first.start()
    second_config = _config(tmp_path / "second", port=first.bound_port)
    second_config = ApplicationConfig(
        config_dir=second_config.config_dir,
        state_dir=second_config.state_dir,
        credential_path=second_config.credential_path,
        host="127.0.0.1",
        port=first.bound_port,
    )
    second = Service(second_config)

    try:
        with pytest.raises(PortCollisionError, match="already in use"):
            second.start()
        assert PortCollisionError.code == "port_collision"
        status, _, body = _get(first.health_endpoint)
        assert status == 200
        assert json.loads(body)["status"] == "ok"
    finally:
        first.stop()


def test_other_bind_error_is_typed_and_closes_owned_state(
    monkeypatch, tmp_path
) -> None:
    port = _free_port()

    class FailingSocket:
        def setsockopt(self, *_args) -> None:
            return None

        def bind(self, _address) -> None:
            raise OSError(errno.EACCES, "permission denied")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "millrace_os.service.socket.socket", lambda *_args: FailingSocket()
    )
    service = Service(_config(tmp_path, port=port))

    with pytest.raises(BindError, match="could not bind"):
        service.start()

    assert BindError.code == "bind_error"
    assert service.lifecycle == "stopped"
    assert service.state is None


def test_asgi_startup_failure_never_reports_ready(monkeypatch, tmp_path) -> None:
    def fail_run(self, *, sockets):
        raise RuntimeError("startup failed")

    monkeypatch.setattr("millrace_os.service.uvicorn.Server.run", fail_run)
    service = Service(_config(tmp_path))

    with pytest.raises(ServiceStartupError, match="ASGI server failed"):
        service.start()

    assert not service.wait_until_ready(0)
    assert service.lifecycle == "stopped"


def test_corrupt_state_keeps_recovery_shell_available(tmp_path) -> None:
    config = _config(tmp_path)
    config.state_dir.mkdir(parents=True)
    config.database_path.write_bytes(b"corrupt")
    service = Service(config)

    service.start()
    try:
        status, _, body = _get(service.health_endpoint)
        assert status == 503
        health = json.loads(body)
        assert health == {"status": "unavailable"}

        status, _, body = _get(service.recovery_endpoint)
        assert status == 200
        assert "corrupt_state" not in body
    finally:
        service.stop()


def test_shutdown_does_not_stop_provider_hosted_processes(tmp_path) -> None:
    class ProviderBoundary:
        def __init__(self) -> None:
            self.stop_calls = 0

        def stop_all(self) -> None:
            self.stop_calls += 1

    provider = ProviderBoundary()
    service = Service(
        _config(tmp_path),
        dependencies=ServiceDependencies(provider_processes=provider),
    )
    service.start()
    service.stop()

    assert provider.stop_calls == 0


def test_ipv6_loopback_service_starts_when_explicitly_configured(tmp_path) -> None:
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credentials.json",
        host="::1",
        port=_free_port("::1"),
    )
    service = Service(config)

    service.start()
    try:
        status, _, body = _get(service.health_endpoint)
        assert status == 200
        assert json.loads(body)["status"] == "ok"
    finally:
        service.stop()


def test_localhost_loopback_service_starts_when_explicitly_configured(tmp_path) -> None:
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credentials.json",
        host="localhost",
        port=_free_port(),
    )
    service = Service(config)

    service.start()
    try:
        status, _, body = _get(service.health_endpoint)
        assert status == 200
        assert json.loads(body)["status"] == "ok"
    finally:
        service.stop()


def test_late_public_stop_cannot_escape_final_drain_seal(tmp_path, monkeypatch):
    import threading

    import httpx

    from millrace_os.credentials import CredentialStore
    from millrace_os.router_client import RouterClient

    entered, release = threading.Event(), threading.Event()

    class DelayedStop(RouterClient):
        def execute_request(self, home, request, on_started=None):
            entered.set()
            assert release.wait(5)
            return super().execute_request(home, request, on_started)

    config = _config(tmp_path)
    router = DelayedStop({"default": tmp_path / "core"})
    service = Service(config, dependencies=ServiceDependencies(router_client=router))
    service.start()
    drained, finish = threading.Event(), threading.Event()
    original_finish = service._finish_stop

    def finish_boundary():
        drained.set()
        assert finish.wait(5)
        original_finish()

    monkeypatch.setattr(service, "_finish_stop", finish_boundary)
    http = httpx.Client(
        base_url=service.browser_endpoint,
        headers={
            "Authorization": "Bearer "
            + CredentialStore(config.credential_path).ensure()
        },
        timeout=2,
        trust_env=False,
    )
    try:
        service.request_shutdown()
        assert drained.wait(2)
        assert service.state is not None
        # The final boundary has no outstanding calls. A stop arriving after
        # that atomic handoff must be refused before it can enter any facade.
        request = {
            "format": "millrace-router-request/v2",
            "operation": "pause_run",
            "instance_id": "0" * 32,
            "run_id": "1" * 32,
            "plane_id": None,
            "request_id": "late-stop",
            "expected_state_version": None,
            "payload": {},
        }
        response = http.post(
            "/api/v1/router/requests", json={"home": "default", "request": request}
        )
        assert response.status_code == 503, response.text
        assert not entered.is_set()
        assert service.router_workers.snapshot() == []
        finish.set()
        assert service.wait_for_stop(3)
        assert service.state is None
    finally:
        release.set()
        finish.set()
        service.stop()
        http.close()
