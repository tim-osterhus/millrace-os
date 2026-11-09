from __future__ import annotations

import json
import socket

from millrace_os.cli import main
from millrace_os.config import ApplicationConfig
from millrace_os.service import Service


def _free_port(host: str = "127.0.0.1") -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def test_health_command_queries_an_ipv6_loopback_service(tmp_path, capsys) -> None:
    service = Service(
        ApplicationConfig(
            config_dir=tmp_path / "config",
            state_dir=tmp_path / "state",
            credential_path=tmp_path / "config" / "credentials.json",
            host="::1",
            port=_free_port("::1"),
        )
    )
    service.start()
    try:
        assert (
            main(
                [
                    "health",
                    "--host",
                    "::1",
                    "--port",
                    str(service.bound_port),
                    "--state-dir",
                    str(tmp_path / "state"),
                    "--config-dir",
                    str(tmp_path / "config"),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
    finally:
        service.stop()


def test_health_command_formats_ipv6_endpoint_for_http(
    monkeypatch, tmp_path, capsys
) -> None:
    requested: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    def fake_urlopen(url: str, timeout: int):
        requested.append(url)
        assert timeout == 5
        return Response()

    monkeypatch.setattr("millrace_os.cli.urllib.request.urlopen", fake_urlopen)

    assert (
        main(
            [
                "health",
                "--host",
                "::1",
                "--port",
                "8731",
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 0
    )

    assert requested == ["http://[::1]:8731/health"]
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_router_cli_preserves_saved_envelope_and_disables_proxy_redirects(
    monkeypatch, tmp_path, capsys
):
    from millrace_os.credentials import CredentialStore

    config = tmp_path / "config"
    token = CredentialStore(config / "credentials.json").ensure()
    envelope = {
        "format": "millrace-router-request/v2",
        "operation": "pause_plane",
        "instance_id": "instance",
        "run_id": "run",
        "plane_id": "explicit",
        "request_id": "request",
        "expected_state_version": None,
        "payload": {},
    }
    saved = tmp_path / "request.json"
    saved.write_text(json.dumps(envelope))
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit == 4 * 1048576
            return b'{"phase":"call_active","core_receipt":null}'

    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            assert timeout == 5
            return Response()

    def build(*handlers):
        import urllib.request

        assert any(
            isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {}
            for handler in handlers
        )
        redirect = next(
            handler
            for handler in handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        )
        assert redirect.redirect_request(None, None, None, None, None, None) is None
        return Opener()

    monkeypatch.setattr("millrace_os.cli.urllib.request.build_opener", build)
    assert (
        main(
            [
                "router",
                "request",
                "--home",
                "selected",
                "--file",
                str(saved),
                "--config-dir",
                str(config),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 0
    )
    request = captured[0]
    assert request.full_url == "http://127.0.0.1:8731/api/v1/router/requests"
    assert json.loads(request.data) == {"home": "selected", "request": envelope}
    assert request.headers["Authorization"] == "Bearer " + token
    output = capsys.readouterr().out
    assert token not in output and json.loads(output)["core_receipt"] is None
    assert json.loads(saved.read_text()) == envelope


def _fake_trust(monkeypatch, opened: list[str], requests: list | None = None):
    nonce = "n" * 43

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return json.dumps({"nonce": nonce, "expires_in": 60}).encode()

    class Opener:
        def open(self, request, timeout):
            if requests is None:
                raise AssertionError("an invalid target must not contact the service")
            requests.append(request)
            return Response()

    monkeypatch.setattr(
        "millrace_os.cli.urllib.request.build_opener", lambda *handlers: Opener()
    )
    monkeypatch.setattr(
        "millrace_os.cli.webbrowser.open", lambda url: opened.append(url) or True
    )
    return nonce


def _open_args(tmp_path, *extra: str) -> list[str]:
    return [
        "open",
        *extra,
        "--config-dir",
        str(tmp_path / "config"),
        "--state-dir",
        str(tmp_path / "state"),
    ]


def test_open_targets_the_exact_stock_base_and_keeps_the_nonce_in_the_fragment(
    monkeypatch, tmp_path, capsys
):
    instance, run = "a" * 32, "b" * 32
    cases = [
        ((), "/ui/stock/"),
        (("--home", "main"), "/ui/stock/?home=main"),
        (
            ("--home", "main", "--instance", instance),
            f"/ui/stock/?home=main&instance={instance}",
        ),
        (
            ("--home", "main", "--instance", instance, "--run", run),
            f"/ui/stock/?home=main&instance={instance}&run={run}",
        ),
        (("--recovery",), "/recovery"),
    ]
    for extra, path in cases:
        opened, requests = [], []
        nonce = _fake_trust(monkeypatch, opened, requests)
        assert main(_open_args(tmp_path, *extra)) == 0
        assert opened == ["http://127.0.0.1:8731" + path + "#" + nonce]
        assert requests[0].full_url == "http://127.0.0.1:8731/api/v1/auth/bootstrap"
        assert json.loads(requests[0].data) == {"recovery": extra == ("--recovery",)}
        output = capsys.readouterr().out
        assert json.loads(output) == {"status": "browser_opened"}
        assert nonce not in output


def test_open_refuses_invalid_targets_before_contacting_the_service(
    monkeypatch, tmp_path, capsys
):
    instance = "a" * 32
    for extra in (
        ("--instance", instance),
        ("--home", "main", "--run", instance),
        ("--home=-bad",),
        ("--home", "main", "--instance", "ABC"),
        ("--recovery", "--home", "main"),
        ("--recovery", "--home", "main", "--instance", instance),
    ):
        opened: list[str] = []
        _fake_trust(monkeypatch, opened)
        assert main(_open_args(tmp_path, *extra)) == 2
        assert opened == []
        assert json.loads(capsys.readouterr().out) == {"error": "invalid_target"}
