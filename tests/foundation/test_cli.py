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
