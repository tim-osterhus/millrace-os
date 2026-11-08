"""Command-line entrypoint for the local Millrace OS daemon."""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import sys
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

# `millrace-os presentation` mirrors the fifteen OS-G0 7.4.1 alternatives.
PRESENTATION_COMMANDS = (
    "get",
    "put",
    "replace-unreadable",
    "list",
    "read",
    "default",
    "export",
    "create",
    "update",
    "rename",
    "duplicate",
    "delete",
    "set-default",
    "reset",
    "import",
)
PRESENTATION_MUTATIONS = frozenset(
    {
        "create",
        "update",
        "rename",
        "duplicate",
        "delete",
        "set-default",
        "reset",
        "import",
    }
)
PRESENTATION_ROUTE = "/api/v1/client/presentation"


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
    mcp = subparsers.add_parser("mcp", help="optional MCP bridge").add_subparsers(
        dest="mcp_command", required=True
    )
    mcp_serve = mcp.add_parser("serve", help="serve existing OS controls")
    mcp_serve.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    mcp_serve.add_argument("--config", required=True)
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
            command.add_argument(
                "--bounded",
                action="store_true",
                help="strict bounded observation; requires --instance",
            )
        elif name == "validate":
            command.add_argument("--instance", required=True)
            command.add_argument("--run")
            command.add_argument("--draft")
            command.add_argument(
                "--summary", action="store_true", help="bounded validation summary"
            )
    homes = commands.add_parser("homes", help="list configured state-home names")
    _add_config_options(homes)
    views = commands.add_parser("views", help="bounded Core inspection views")
    _add_config_options(views)
    views.add_argument("--home", required=True)
    for option in ("--instance", "--run", "--revision", "--section"):
        views.add_argument(option)
    views.add_argument("--request-id")
    views.add_argument("--invocation-id")
    for command in (homes, views):
        command.add_argument("--limit", type=int)
        command.add_argument("--cursor")
    outputs = commands.add_parser(
        "outputs", help="generic workflow output records"
    ).add_subparsers(dest="outputs_command", required=True)
    for name in ("publish", "list", "read"):
        command = outputs.add_parser(name)
        _add_config_options(command)
        command.add_argument("--home", required=True)
        if name == "publish":
            command.add_argument("--file", required=True)
            continue
        command.add_argument("--instance", required=True)
        command.add_argument("--run", required=True)
        if name == "list":
            command.add_argument("--limit", type=int)
            command.add_argument("--cursor")
        else:
            command.add_argument("--output", required=True)
            command.add_argument("--record-sha256", required=True)
            command.add_argument(
                "--representation", required=True, choices=("report", "json")
            )
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
    presentation = subparsers.add_parser(
        "presentation", help="shared presentation preferences (display data only)"
    ).add_subparsers(dest="presentation_command", required=True)
    for name in PRESENTATION_COMMANDS:
        # No abbreviations: `--config` must never be read as `--config-dir`.
        command = presentation.add_parser(name, allow_abbrev=False)
        _add_config_options(command)
        if name in {"put", "import"}:
            command.add_argument("--file", required=True)
        if name == "replace-unreadable":
            command.add_argument("--observed-sha256", required=True)
            command.add_argument("--store-file", required=True)
        if name not in {"get", "put", "replace-unreadable", "import"}:
            # NS: the explicit pair, optional only for `list`.
            command.add_argument("--frontend", required=name != "list")
            command.add_argument("--format", required=name != "list")
        if name in {"read", "export", "duplicate", "set-default"}:
            reference = command.add_mutually_exclusive_group(required=True)
            reference.add_argument("--config", metavar="ID")
            reference.add_argument("--bundled", metavar="ID")
            if name == "set-default":
                reference.add_argument("--none", action="store_true")
        elif name in {"update", "rename", "delete", "reset"}:
            command.add_argument("--config", metavar="ID", required=True)
        if name in {"create", "rename", "duplicate", "import"}:
            command.add_argument("--name", required=name != "import")
        if name in {"create", "update"}:
            command.add_argument("--payload-file", required=True)
        if name in PRESENTATION_MUTATIONS:
            command.add_argument(
                "--expected-version",
                metavar="TOKEN|null",
                help="require this GET token; null requires an absent store",
            )
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
    body = raw = None
    command = args.router_command
    query = {} if command == "homes" else {"home": args.home}
    if command in {"homes", "views"}:
        # The service query allowlist refuses options a view does not accept.
        if command == "homes":
            route += "/homes"
        else:
            if args.run and not args.instance:
                raise ConfigurationError("--run requires --instance")
            if args.revision and not args.run:
                raise ConfigurationError("--revision requires --run")
            route += "/views/instances"
            for prefix, value in (
                ("/", args.instance),
                ("/runs/", args.run),
                ("/revisions/", args.revision),
            ):
                if value:
                    route += prefix + urllib.parse.quote(value, safe="")
            query.update(
                (key, value)
                for key, value in (
                    ("section", args.section),
                    ("request_id", args.request_id),
                    ("invocation_id", args.invocation_id),
                )
                if value is not None
            )
        query.update(
            (key, value)
            for key, value in (("limit", args.limit), ("cursor", args.cursor))
            if value is not None
        )
    elif command == "instances":
        route += "/instances"
    elif command == "inspect":
        route += "/instances/" + urllib.parse.quote(args.instance, safe="")
        if args.run:
            route += "/runs/" + urllib.parse.quote(args.run, safe="")
    elif command == "request":
        if args.bounded and args.file:
            raise ConfigurationError("--bounded observes an existing --request-id")
        if args.file:
            request = json.loads(Path(args.file).read_bytes())
            if not isinstance(request, dict):
                raise ConfigurationError(
                    "request file must contain an exact saved JSON envelope"
                )
            method, body = "POST", {"home": args.home, "request": request}
            route += "/requests"
        else:
            route += ("/views" if args.bounded else "") + "/requests/"
            route += urllib.parse.quote(args.request_id, safe="")
            if args.instance:
                query["instance_id"] = args.instance
            if args.run:
                query["run_id"] = args.run
    elif command == "validate":
        if args.summary and args.draft:
            raise ConfigurationError("--summary validates the configured revision")
        method = "POST"
        route += "/views/validate" if args.summary else "/validate"
        body = {
            "home": args.home,
            "instance_id": args.instance,
            "run_id": args.run,
        }
        if not args.summary:
            body["draft_id"] = args.draft
    elif command == "outputs":
        action = args.outputs_command
        route += "/outputs"
        if action == "publish":
            from .contracts import strict_json

            # The saved record is sent exactly, as compact UTF-8, never retried.
            try:
                record = strict_json(Path(args.file).read_bytes())
                raw = json.dumps(
                    {"home": args.home, "record": record},
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode()
            except ValueError as exc:
                raise ConfigurationError(
                    "record file must be strict UTF-8 JSON with unique keys and "
                    "finite numbers"
                ) from exc
            method = "POST"
        else:
            route += "/instances/" + urllib.parse.quote(args.instance, safe="")
            route += "/runs/" + urllib.parse.quote(args.run, safe="")
            if action == "read":
                route += "/" + urllib.parse.quote(args.output, safe="")
                query.update(
                    record_sha256=args.record_sha256,
                    representation=args.representation,
                )
            else:
                query.update(
                    (key, value)
                    for key, value in (("limit", args.limit), ("cursor", args.cursor))
                    if value is not None
                )
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
    if method == "GET" and query:
        route += "?" + urllib.parse.urlencode(query)
    token = CredentialStore(config.credential_path).ensure()
    endpoint = f"http://{_endpoint_host(config.host)}:{config.port}"
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        raw = json.dumps(body, allow_nan=False).encode()
    if raw is not None:
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


def _local_call(config, token, method, route, raw=None):
    """One bearer-authenticated local request; returns (HTTP status, body)."""
    endpoint = f"http://{_endpoint_host(config.host)}:{config.port}"
    headers = {"Authorization": "Bearer " + token}
    if raw is not None:
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
            return response.status, response.read(4 * 1048576)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096)


def _compact(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _presentation(args):
    """GET, PUT, or one GET -> shared helper -> PUT `compare` with the GET token.

    Helpers validate before any PUT. A refusal or conflict is printed and
    returned, never retried; nothing is written implicitly.
    """
    from .presentation import HELPERS, PresentationError, strict_loads

    config = _config(args)
    command = args.presentation_command
    operation = {"default": "read_default"}.get(command, command.replace("-", "_"))

    def document(path):
        try:
            return strict_loads(Path(path).read_bytes())
        except ValueError as exc:
            raise ConfigurationError(
                f"{path} must be strict UTF-8 JSON with unique keys and finite "
                "numbers"
            ) from exc

    arguments = {}
    frontend = getattr(args, "frontend", None)
    presentation_format = getattr(args, "format", None)
    if (frontend is None) != (presentation_format is None):
        raise ConfigurationError("--frontend and --format name one namespace")
    if frontend is not None:
        arguments["namespace"] = {
            "frontend_id": frontend,
            "presentation_format": presentation_format,
        }
    reference = None
    if getattr(args, "bundled", None) is not None:
        reference = {"kind": "bundled", "id": args.bundled}
    elif getattr(args, "config", None) is not None:
        reference = {"kind": "user", "id": args.config}
    if operation == "put":
        arguments["write"] = document(args.file)
    elif operation == "replace_unreadable":
        arguments["observed_file_sha256"] = args.observed_sha256
        arguments["store"] = document(args.store_file)
    elif operation == "import":
        arguments["config"] = document(args.file)
    elif operation in {"read", "export"}:
        arguments["ref"] = reference
    elif operation == "duplicate":
        arguments["source"] = reference
    elif operation == "set_default":
        arguments["default"] = reference  # None for --none
    elif operation in {"update", "rename", "delete", "reset"}:
        arguments["config_id"] = args.config
    if getattr(args, "name", None) is not None:
        arguments["name"] = args.name
    if operation in {"create", "update"}:
        arguments["payload"] = document(args.payload_file)
    if command in PRESENTATION_MUTATIONS and args.expected_version is not None:
        # Omitted means no caller precondition; the literal `null` is a value.
        version = args.expected_version
        arguments["expected_version"] = None if version == "null" else version
    token = CredentialStore(config.credential_path).ensure()
    try:
        if operation in {"put", "replace_unreadable"}:
            write = HELPERS[operation](None, **arguments)
        else:
            status, body = _local_call(config, token, "GET", PRESENTATION_ROUTE)
            if status != 200 or operation == "get":
                print(body.decode())
                return 0 if status == 200 else 1
            result = HELPERS[operation](strict_loads(body), **arguments)
            if command not in PRESENTATION_MUTATIONS:
                print(_compact(result).decode())
                return 0
            write = result
    except PresentationError as exc:
        print(json.dumps({"error": exc.code}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"error": "daemon_unavailable"}))
        return 1
    try:
        status, body = _local_call(
            config, token, "PUT", PRESENTATION_ROUTE, _compact(write)
        )
    except OSError:
        # The PUT may have been applied: GET and compare before any retry.
        print(json.dumps({"error": "presentation_write_unknown"}))
        return 1
    print(body.decode())
    return 0 if status == 200 else 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "mcp":
            try:
                if sys.version_info[:2] not in {(3, 12), (3, 13)}:
                    raise ValueError("unsupported MCP profile")
                from .mcp_bridge import serve
                serve(args.config, args.transport)
                return 0
            except (ImportError, ValueError, OSError):
                print(json.dumps({
                    "error": "mcp_unavailable",
                    "message": "MCP requires the explicit mcp extra, Python "
                    "3.12/3.13 and valid configuration/backend.",
                }), file=sys.stderr)
                return 1
        if args.command == "router":
            return _router(args)
        if args.command == "presentation":
            return _presentation(args)
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
