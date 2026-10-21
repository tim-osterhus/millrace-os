"""Command-line entrypoint for the local Millrace OS daemon."""

from __future__ import annotations

import argparse
import json
import os
import signal
import urllib.error
import urllib.request
import webbrowser
from dataclasses import replace

from .config import ApplicationConfig, ConfigurationError, _endpoint_host
from .credentials import CredentialStore
from .process import (
    ProcessIdentity,
    ProcessIdentityError,
    ProcessStopError,
    read_identity,
    stop_verified,
    write_identity,
)
from .service import BindError, PortCollisionError, Service


class _PidFile:
    """Owner-only identity record for the local service process."""

    def __init__(self, path) -> None:
        from pathlib import Path

        self.path = Path(path).expanduser().absolute()
        self._identity: ProcessIdentity | None = None

    def acquire(self) -> None:
        from .paths import ensure_directory

        ensure_directory(self.path.parent)
        identity = ProcessIdentity.current()
        write_identity(self.path, identity)
        self._identity = identity

    def release(self) -> None:
        if self._identity is None:
            return
        try:
            recorded = read_identity(self.path)
        except ProcessIdentityError:
            return
        if recorded != self._identity:
            return
        self.path.unlink(missing_ok=True)
        self._identity = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="millrace-os")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser(
        "serve", help="start the local daemon in the foreground"
    )
    _add_config_options(serve)
    serve.add_argument("--no-pid-file", action="store_true")
    for name in ("health", "readiness"):
        query = subparsers.add_parser(name, help="query the local daemon")
        _add_config_options(query)
    browser = subparsers.add_parser("open", help="authenticate a local browser")
    _add_config_options(browser)
    browser.add_argument("--recovery", action="store_true")
    rotate = subparsers.add_parser(
        "rotate-credential", help="rotate credential and revoke browser trust"
    )
    _add_config_options(rotate)
    stop = subparsers.add_parser("stop", help="stop the recorded local daemon")
    stop.add_argument("--state-dir")
    return parser


def _add_config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--config-dir")
    parser.add_argument("--state-dir")
    parser.add_argument("--credential-path")


def _config(args: argparse.Namespace) -> ApplicationConfig:
    config = ApplicationConfig.from_environment()
    updates = {
        key: value
        for key, value in {
            "host": args.host,
            "port": args.port,
            "config_dir": args.config_dir,
            "state_dir": args.state_dir,
            "credential_path": args.credential_path,
        }.items()
        if value is not None
    }
    if args.config_dir and args.credential_path is None:
        updates["credential_path"] = os.path.join(args.config_dir, "credentials.json")
    return replace(config, **updates)


def _serve(args: argparse.Namespace) -> int:
    config = _config(args)
    service = Service(config)
    pid_file = None if args.no_pid_file else _PidFile(config.pid_path)
    if pid_file is not None:
        pid_file.acquire()
    try:
        service.start()
        print(f"READY {service.browser_endpoint}", flush=True)

        def request_stop(_signum: int, _frame: object) -> None:
            service.stop()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        service.wait_for_stop()
        return 0
    finally:
        service.stop()
        if pid_file is not None:
            pid_file.release()


def _query(args: argparse.Namespace) -> int:
    config = _config(args)
    path = "/ready" if args.command == "readiness" else "/health"
    endpoint = f"http://{_endpoint_host(config.host)}:{config.port}{path}"
    try:
        with urllib.request.urlopen(endpoint, timeout=5) as response:
            body = response.read().decode()
            print(body)
            return 0 if response.status < 400 else 1
    except urllib.error.HTTPError as error:
        print(error.read().decode())
        return 1
    except OSError as error:
        print(json.dumps({"error": "daemon_unavailable", "message": str(error)}))
        return 1


def _trust_command(args: argparse.Namespace) -> int:
    config = _config(args)
    # Explicitly configured local credential is used only in the request header.
    token = CredentialStore(config.credential_path).ensure()
    endpoint = f"http://{_endpoint_host(config.host)}:{config.port}"
    recovery = bool(getattr(args, "recovery", False))
    route = "bootstrap" if args.command == "open" else "rotate"
    request = urllib.request.Request(
        endpoint + "/api/v1/auth/" + route,
        data=json.dumps({"recovery": recovery}).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    # Never follow a redirect carrying a bearer header to a different endpoint.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            payload = json.loads(response.read(4096))
        if args.command == "open":
            path = "/recovery" if recovery else "/"
            if not webbrowser.open(endpoint + path + "#" + payload["nonce"]):
                print(json.dumps({"error": "browser_unavailable"}))
                return 1
            print(json.dumps({"status": "browser_opened"}))
        else:
            print(json.dumps({"status": "rotated"}))
        return 0
    except (OSError, ValueError, KeyError):
        print(json.dumps({"error": "local_trust_unavailable"}))
        return 1


def _stop(args: argparse.Namespace) -> int:
    config = ApplicationConfig.from_environment()
    if args.state_dir:
        config = replace(config, state_dir=args.state_dir)
    identity = read_identity(config.pid_path)
    stop_verified(identity, timeout=5.0)
    print(json.dumps({"status": "stopped", "pid": identity.pid}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(args)
        if args.command in {"open", "rotate-credential"}:
            return _trust_command(args)
        if args.command in {"health", "readiness"}:
            return _query(args)
        return _stop(args)
    except ProcessIdentityError as error:
        print(json.dumps({"error": error.code, "message": str(error)}))
        return 1
    except ProcessStopError as error:
        print(json.dumps({"error": error.code, "message": str(error)}))
        return 1
    except (
        ConfigurationError,
        BindError,
        PortCollisionError,
        PermissionError,
        OSError,
    ) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
