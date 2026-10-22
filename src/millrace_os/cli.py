"""Command-line entrypoint for the local Millrace OS daemon."""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import replace
from pathlib import Path

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
    serve.add_argument(
        "--router-state-home", action="append", default=[], metavar="NAME=PATH"
    )
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
    stop.add_argument("--timeout", type=float, default=5.0)
    router = subparsers.add_parser(
        "router", help="operate public Core controls headlessly"
    )
    commands = router.add_subparsers(dest="router_command", required=True)
    for name in ("instances", "inspect", "request", "validate"):
        command = commands.add_parser(name)
        _add_config_options(command)
        command.add_argument("--home", required=True)
        if name == "inspect":
            command.add_argument("--instance", required=True)
            command.add_argument("--run")
        elif name == "request":
            choice = command.add_mutually_exclusive_group(required=True)
            choice.add_argument("--file")
            choice.add_argument("--request-id")
            command.add_argument("--instance")
            command.add_argument("--run")
        elif name == "validate":
            command.add_argument("--instance", required=True)
            command.add_argument("--run")
            command.add_argument("--draft")
    catalog = commands.add_parser("catalog").add_subparsers(
        dest="catalog_command", required=True
    )
    for name in ("list", "init", "copy"):
        command = catalog.add_parser(name)
        _add_config_options(command)
        command.add_argument("--home", required=True)
        if name == "init":
            command.add_argument("--selector", required=True)
            command.add_argument("--destination", required=True)
        elif name == "copy":
            choice = command.add_mutually_exclusive_group(required=True)
            choice.add_argument("--source")
            choice.add_argument("--draft")
            command.add_argument("--destination", required=True)
    drafts = commands.add_parser("draft").add_subparsers(
        dest="draft_command", required=True
    )
    for name in ("create", "read", "put", "validate", "apply", "repair-publication"):
        command = drafts.add_parser(name)
        _add_config_options(command)
        command.add_argument("--home", required=True)
        if name not in {"create", "repair-publication"}:
            command.add_argument("--draft", required=True)
        if name == "put":
            command.add_argument("--path", required=True)
            command.add_argument("--file", required=True)
        if name in {"validate", "apply", "repair-publication"}:
            command.add_argument("--instance", required=True)
        if name in {"validate", "apply"}:
            command.add_argument("--run", required=name == "apply")
        if name == "apply":
            command.add_argument("--base-state-version", type=int, required=True)
            command.add_argument("--request-id", required=True)
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
    selected_homes = getattr(args, "router_state_home", [])
    if selected_homes:
        homes = dict(config.router_state_homes)
        names = set()
        for item in selected_homes:
            name, separator, path = item.partition("=")
            if not separator or not name or not path or name in names:
                raise ConfigurationError(
                    "state homes require unique NAME=PATH selections"
                )
            names.add(name)
            homes[name] = Path(path)
        updates["router_state_homes"] = homes
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
            service.request_shutdown()

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
    if not 0 < args.timeout <= 86400:
        raise ConfigurationError(
            "stop observation timeout must be positive and at most 86400 seconds"
        )
    stop_verified(identity, timeout=args.timeout)
    print(json.dumps({"status": "stopped", "pid": identity.pid}))
    return 0


def _router(args):
    config = _config(args)
    route = "/api/v1/router"
    method = "GET"
    body = None
    query = {"home": args.home}
    command = args.router_command
    if command == "instances":
        route += "/instances"
    elif command == "inspect":
        route += "/instances/" + urllib.parse.quote(args.instance, safe="")
        if args.run:
            route += "/runs/" + urllib.parse.quote(args.run, safe="")
    elif command == "request":
        if args.file:
            request = json.loads(Path(args.file).read_bytes())
            if not isinstance(request, dict):
                raise ConfigurationError(
                    "request file must contain an exact saved JSON envelope"
                )
            method, body = "POST", {"home": args.home, "request": request}
            route += "/requests"
        else:
            route += "/requests/" + urllib.parse.quote(args.request_id, safe="")
            if args.instance:
                query["instance_id"] = args.instance
            if args.run:
                query["run_id"] = args.run
    elif command == "validate":
        method, route = "POST", route + "/validate"
        body = {
            "home": args.home,
            "instance_id": args.instance,
            "run_id": args.run,
            "draft_id": args.draft,
        }
    elif command == "catalog":
        action = args.catalog_command
        if action == "list":
            route += "/catalog/templates"
        else:
            method, route = "POST", route + "/catalog/" + action
            body = {"home": args.home, "destination": args.destination}
            if action == "init":
                body["selector"] = args.selector
            else:
                body.update(source=args.source, draft_id=args.draft)
    else:
        action = args.draft_command
        if action == "create":
            method, route, body = (
                "POST",
                route + "/drafts",
                {"home": args.home, "files": []},
            )
        elif action == "repair-publication":
            method, route, body = (
                "POST",
                route + "/repair-publication",
                {"home": args.home, "instance_id": args.instance},
            )
        else:
            route += "/drafts/" + urllib.parse.quote(args.draft, safe="")
            if action == "put":
                method, route = "PUT", route + "/file"
                body = {
                    "home": args.home,
                    "path": args.path,
                    "content_base64": base64.b64encode(
                        Path(args.file).read_bytes()
                    ).decode(),
                }
            elif action in {"validate", "apply"}:
                method, route = "POST", route + "/" + action
                body = {
                    "home": args.home,
                    "instance_id": args.instance,
                    "run_id": args.run,
                }
                if action == "apply":
                    body.update(
                        base_state_version=args.base_state_version,
                        request_id=args.request_id,
                    )
    if method == "GET":
        route += "?" + urllib.parse.urlencode(query)
    token = CredentialStore(config.credential_path).ensure()
    endpoint = f"http://{_endpoint_host(config.host)}:{config.port}"
    headers = {"Authorization": "Bearer " + token}
    raw = None
    if body is not None:
        raw = json.dumps(body, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        endpoint + route, data=raw, headers=headers, method=method
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            print(response.read(4 * 1048576).decode())
        return 0
    except urllib.error.HTTPError as exc:
        print(exc.read(4096).decode())
        return 1
    except (OSError, ValueError):
        print(json.dumps({"error": "router_observation_unavailable"}))
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "router":
            return _router(args)
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
