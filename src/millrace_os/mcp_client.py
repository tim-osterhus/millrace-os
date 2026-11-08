"""Bounded fixed-path OS API client; local credentials never cross outward."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from .api import body_limit, error_response
from .contracts import Error
from .mcp_contracts import (
    CONTRACT_V2,
    MAX_OUTPUT,
    TOOL_MODELS_V2,
    V1,
    V2_TIMEOUT,
    MCPConfig,
    access_error,
    access_error_v2,
)
from .paths import validate_file_target
from .presentation import HELPERS, PresentationError

__all__ = ["BackendClient", "V2_ROUTES", "advertised_v2", "mappings_v2"]

ROUTER = "/api/v1/router"
VIEWS = ROUTER + "/views/instances/{instance_id}"
RUN_OUTPUTS = ROUTER + "/outputs/instances/{instance_id}/runs/{run_id}"
PRESENTATION = "/api/v1/client/presentation"
READ, WRITE = (
    ("GET", PRESENTATION, "client.presentation.read"),
    ("PUT", PRESENTATION, "client.presentation.write"),
)
# Every v2 alternative and the exact HTTP steps it takes: (method, path,
# capability operation_id). Path fields come from the arguments; the other
# arguments form the query (GET/OPTIONS) or the JSON body. `discover`/`bridge`
# is bridge-local. Presentation conveniences make one GET, then at most one PUT.
V2_ROUTES = {
    ("millrace_v2_discover", "bridge"): (),
    ("millrace_v2_discover", "health"): (("GET", "/api/v1/health", "health.read"),),
    ("millrace_v2_discover", "identity"): (
        ("GET", "/api/v1/identity", "identity.read"),
    ),
    ("millrace_v2_discover", "version"): (("GET", "/api/v1/version", "version.read"),),
    ("millrace_v2_discover", "contracts"): (
        ("GET", "/api/v1/contracts", "contracts.read"),
    ),
    ("millrace_v2_discover", "schema"): (
        ("GET", "/api/v1/contracts/{contract_id}/1/{digest}", "contracts.schema.read"),
    ),
    ("millrace_v2_discover", "diagnostics"): (
        ("GET", "/api/v1/diagnostics", "diagnostics.read"),
    ),
    ("millrace_v2_discover", "providers"): (
        ("GET", "/api/v1/providers", "providers.list"),
    ),
    ("millrace_v2_discover", "actions"): (("GET", "/api/v1/actions", "actions.list"),),
    ("millrace_v2_discover", "availability"): (
        ("GET", "/api/v1/events", "events.availability"),
    ),
    ("millrace_v2_discover", "capabilities"): (
        ("GET", "/api/v1/client/capabilities", "client.capabilities.read"),
    ),
    ("millrace_v2_discover", "observation"): (
        ("OPTIONS", ROUTER + "/requests", "router.request.capabilities"),
    ),
    ("millrace_v2_inspect", "homes"): (
        ("GET", ROUTER + "/homes", "router.homes.list"),
    ),
    ("millrace_v2_inspect", "instances"): (
        ("GET", ROUTER + "/views/instances", "router.views.instances"),
    ),
    ("millrace_v2_inspect", "instance"): (("GET", VIEWS, "router.views.instance"),),
    ("millrace_v2_inspect", "run"): (
        ("GET", VIEWS + "/runs/{run_id}", "router.views.run"),
    ),
    ("millrace_v2_inspect", "revision"): (
        (
            "GET",
            VIEWS + "/runs/{run_id}/revisions/{revision_id}",
            "router.views.revision",
        ),
    ),
    ("millrace_v2_request", None): (
        ("POST", ROUTER + "/requests", "router.request.submit"),
    ),
    ("millrace_v2_receipt", "lifecycle"): (
        ("GET", ROUTER + "/requests/{request_id}", "router.request.observe"),
    ),
    ("millrace_v2_receipt", "bounded"): (
        ("GET", ROUTER + "/views/requests/{request_id}", "router.views.request"),
    ),
    ("millrace_v2_validate", "configured"): (
        ("POST", ROUTER + "/validate", "router.validate"),
    ),
    ("millrace_v2_validate", "draft"): (
        ("POST", ROUTER + "/drafts/{draft_id}/validate", "router.draft.validate"),
    ),
    ("millrace_v2_validate", "summary"): (
        ("POST", ROUTER + "/views/validate", "router.views.validate"),
    ),
    ("millrace_v2_catalog", "list"): (
        ("GET", ROUTER + "/catalog/templates", "router.catalog.list"),
    ),
    ("millrace_v2_catalog", "init"): (
        ("POST", ROUTER + "/catalog/init", "router.catalog.init"),
    ),
    ("millrace_v2_catalog", "copy"): (
        ("POST", ROUTER + "/catalog/copy", "router.catalog.copy"),
    ),
    ("millrace_v2_draft", "create"): (
        ("POST", ROUTER + "/drafts", "router.draft.create"),
    ),
    ("millrace_v2_draft", "read"): (
        ("GET", ROUTER + "/drafts/{draft_id}", "router.draft.read"),
    ),
    ("millrace_v2_draft", "replace_files"): (
        ("PUT", ROUTER + "/drafts/{draft_id}/files", "router.draft.replace_files"),
    ),
    ("millrace_v2_draft", "put_file"): (
        ("PUT", ROUTER + "/drafts/{draft_id}/file", "router.draft.put_file"),
    ),
    ("millrace_v2_draft", "manifest"): (
        ("GET", ROUTER + "/drafts/{draft_id}/manifest", "router.draft.manifest"),
    ),
    ("millrace_v2_draft", "read_file"): (
        ("GET", ROUTER + "/drafts/{draft_id}/file", "router.draft.read_file"),
    ),
    ("millrace_v2_draft", "apply"): (
        ("POST", ROUTER + "/drafts/{draft_id}/apply", "router.draft.apply"),
    ),
    ("millrace_v2_draft", "repair_publication"): (
        ("POST", ROUTER + "/repair-publication", "router.publication.repair"),
    ),
    ("millrace_v2_draft", "publication_status"): (
        ("GET", ROUTER + "/publications/{instance_id}", "router.publication.status"),
    ),
    ("millrace_v2_local_call", None): (
        ("GET", ROUTER + "/local-calls/{request_id}", "router.local_call.read"),
    ),
    ("millrace_v2_outputs", "publish"): (
        ("POST", ROUTER + "/outputs", "router.outputs.publish"),
    ),
    ("millrace_v2_outputs", "list"): (("GET", RUN_OUTPUTS, "router.outputs.list"),),
    ("millrace_v2_outputs", "read"): (
        ("GET", RUN_OUTPUTS + "/{output_id}", "router.outputs.read"),
    ),
    ("millrace_v2_presentation", "get"): (READ,),
    ("millrace_v2_presentation", "put"): (WRITE,),
    ("millrace_v2_presentation", "replace_unreadable"): (WRITE,),
    ("millrace_v2_presentation", "list"): (READ,),
    ("millrace_v2_presentation", "read"): (READ,),
    ("millrace_v2_presentation", "read_default"): (READ,),
    ("millrace_v2_presentation", "export"): (READ,),
    **{
        ("millrace_v2_presentation", name): (READ, WRITE)
        for name in (
            "create",
            "update",
            "rename",
            "duplicate",
            "delete",
            "set_default",
            "reset",
            "import",
        )
    },
}
DISCRIMINATORS = {
    "millrace_v2_discover": "kind",
    "millrace_v2_inspect": "target",
    "millrace_v2_receipt": "mode",
    "millrace_v2_validate": "kind",
    "millrace_v2_catalog": "operation",
    "millrace_v2_draft": "operation",
    "millrace_v2_outputs": "operation",
    "millrace_v2_presentation": "operation",
}
CALL_MEDIA_TYPES = {
    "millrace-os-router-call/v1": "application/vnd.millrace.router-call.v1+json",
    "millrace-os-router-call/v2": "application/vnd.millrace.router-call.v2+json",
}
CORRELATED = ("home", "instance_id", "run_id", "request_id", "draft_id", "output_id")
CORE_ID = re.compile(r"[0-9a-f]{32}")


def mappings_v2() -> list[dict]:
    """The fixed alternative -> HTTP step table, for bridge self-description."""
    return [
        {
            "tool": tool,
            "alternative": alternative,
            "method": method,
            "path": path,
            "operation_id": operation_id,
        }
        for (tool, alternative), steps in V2_ROUTES.items()
        for method, path, operation_id in steps
    ]


def advertised_v2(capabilities: dict) -> list[str]:
    """Tools whose every mapped operation the backend reports available."""
    return [
        tool
        for tool in TOOL_MODELS_V2
        if all(
            capabilities.get(operation_id) is True
            for (name, _), steps in V2_ROUTES.items()
            if name == tool
            for _, _, operation_id in steps
        )
    ]


def correlation_v2(args: dict) -> dict:
    """Known identities from the admitted arguments and any request body.

    Top-level fields already passed their grammars. The request body is
    forwarded unchanged and is not validated here, so only CoreId-shaped
    identities are copied from it; a correlation therefore stays small.
    """
    result = {k: args[k] for k in CORRELATED if isinstance(args.get(k), str)}
    request = args.get("request")
    if isinstance(request, dict):
        result.update(
            {
                k: request[k]
                for k in ("request_id", "instance_id", "run_id")
                if isinstance(request.get(k), str) and CORE_ID.fullmatch(request[k])
            }
        )
    return result


def _compact(value) -> bytes:
    """The API encoding: compact UTF-8, finite numbers only."""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _credential(path: str) -> str:
    target, exists = validate_file_target(Path(path))
    if not exists:
        raise ValueError("credential unavailable")
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise ValueError("credential unavailable")
        raw = os.read(fd, 4097)
        value = json.loads(raw)
        token = value["token"]
        if (
            set(value) != {"token"}
            or not isinstance(token, str)
            or not 32 <= len(token) <= 256
            or any(
                c
                not in (
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                )
                for c in token
            )
        ):
            raise ValueError("credential unavailable")
        return token
    finally:
        os.close(fd)


class BackendClient:
    """Own one HTTP pool, never the OS daemon or its workers."""

    def __init__(self, config: MCPConfig, *, transport=None, profile=V1) -> None:
        self.config = config
        self.profile = profile
        self.http = httpx.AsyncClient(
            base_url=config.os_origin,
            timeout=5,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self.version: dict = {}
        self.contracts: dict = {}
        self.capabilities: dict = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(
        self, method: str, path: str, *, params=None, body=None, correlation=None
    ) -> dict:
        try:
            token = await asyncio.to_thread(_credential, self.config.os_credential_file)
        except (OSError, ValueError, KeyError, TypeError):
            return access_error("local_credential_unavailable", correlation=correlation)
        try:
            async with (
                asyncio.timeout(5),
                self.http.stream(
                    method,
                    path,
                    params=params,
                    json=body,
                    headers={"Authorization": "Bearer " + token},
                ) as response,
            ):
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_OUTPUT:
                        return access_error(
                            "backend_response_too_large", correlation=correlation
                        )
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    code, retryable = "backend_http_" + str(status), status == 429
                    try:
                        error = Error.model_validate_json(chunks)
                        code = error.code
                        retryable = retryable or error.retryable
                    except ValidationError:
                        pass
                    return access_error(
                        code,
                        status=status,
                        retryable=retryable,
                        correlation=correlation,
                    )
                data = json.loads(chunks)
                if not isinstance(data, dict):
                    raise ValueError("invalid envelope")
                return data
        except (httpx.TimeoutException, TimeoutError):
            return access_error("backend_timeout", correlation=correlation)
        except (httpx.HTTPError, ValueError, UnicodeError):
            return access_error("backend_unavailable", correlation=correlation)

    async def startup(self) -> None:
        self.version = await self._request("GET", "/api/v1/version")
        self.contracts = await self._request("GET", "/api/v1/contracts")
        version = self.version.get("data", {})
        registry = self.contracts.get("data", {})
        if (
            not isinstance(version, dict)
            or not isinstance(registry, dict)
            or not isinstance(registry.get("schemas"), list)
        ):
            raise ValueError("unsupported backend contracts")
        ids = {
            x.get("contract_id")
            for x in registry.get("schemas", [])
            if isinstance(x, dict)
            and type(x.get("version")) is int
            and x.get("version") == 1
        }
        if (
            version.get("api") != "v1"
            or version.get("package") != "millrace-os"
            or registry.get("registry") != "os.registry.v1"
            or not {"RouterRead-JsonValue", "RouterCallObservation"} <= ids
        ):
            raise ValueError("unsupported backend contracts")
        if self.profile.tools == "v2":
            # The v2 profile advertises only tools the backend can serve.
            listed = await self._request("GET", "/api/v1/client/capabilities")
            operations = listed.get("operations")
            if (
                listed.get("contract") != "millrace-os-client-capabilities/v1"
                or not isinstance(operations, list)
                or not all(
                    isinstance(item, dict)
                    and isinstance(item.get("operation_id"), str)
                    and type(item.get("available")) is bool
                    for item in operations
                )
            ):
                raise ValueError("unsupported backend contracts")
            self.capabilities = {
                item["operation_id"]: item["available"] for item in operations
            }

    async def call(self, name: str, args: dict) -> dict:
        home = args["home"]
        params = {"home": home}
        correlation = {
            k: args[k]
            for k in ("home", "instance_id", "run_id", "request_id")
            if k in args
        }
        if name == "millrace_v1_request":
            request = args["request"]
            correlation.update(
                {
                    k: request[k]
                    for k in ("request_id", "instance_id", "run_id")
                    if k in request
                }
            )
            return await self._request(
                "POST", "/api/v1/router/requests", body=args, correlation=correlation
            )
        if name == "millrace_v1_validate":
            return await self._request(
                "POST", "/api/v1/router/validate", body=args, correlation=correlation
            )
        if name == "millrace_v1_receipt":
            params.update(
                {
                    k: args[k]
                    for k in ("instance_id", "run_id")
                    if k in args and args[k] is not None
                }
            )
            path = "/api/v1/router/requests/" + args["request_id"]
        else:
            path = "/api/v1/router/instances"
            if "instance_id" in args:
                path += "/" + args["instance_id"]
            if "run_id" in args:
                path += "/runs/" + args["run_id"]
        return await self._request("GET", path, params=params, correlation=correlation)

    # --- v2 profile: fixed routing, pre-HTTP size check, honest delivery. ---

    async def call_v2(self, name: str, args: dict) -> dict:
        """One admitted v2 tool call as a v2 result payload (never raises HTTP)."""
        correlation = correlation_v2(args)
        field = DISCRIMINATORS.get(name)
        alternative = args.get(field) if field else None
        if name == "millrace_v2_presentation":
            return await self._presentation(alternative, args, correlation)
        ((method, template, _),) = V2_ROUTES[(name, alternative)]
        values = {
            k: v
            for k, v in args.items()
            if k not in {field, "observation_contract"}
            and "{" + k + "}" not in template
        }
        path = template.format(
            **{k: quote(str(v), safe="") for k, v in args.items() if isinstance(v, str)}
        )
        accept = CALL_MEDIA_TYPES.get(args.get("observation_contract"))
        if method in {"GET", "OPTIONS"}:
            return await self._send_v2(
                method,
                path,
                params=values,
                accept=accept,
                correlation=correlation,
            )
        return await self._send_v2(
            method, path, body=values, accept=accept, correlation=correlation
        )

    async def _presentation(self, operation, args, correlation):
        """Raw GET/PUT, or GET -> shared helper -> PUT `compare` in one call.

        A helper refusal after the GET is the typed OS refusal for that state;
        a conflict is returned, never retried.
        """
        if operation == "get":
            return await self._send_v2("GET", PRESENTATION, correlation=correlation)
        if operation in {"put", "replace_unreadable"}:
            values = {k: v for k, v in args.items() if k != "operation"}
            try:
                write = HELPERS[operation](None, **values)
            except PresentationError:
                return access_error_v2(
                    "invalid_arguments", status=422, delivery="not_sent"
                )
            return await self._send_v2(
                "PUT", PRESENTATION, body=write, correlation=correlation
            )
        current = await self._send_v2("GET", PRESENTATION, correlation=correlation)
        if "access_error" in current or current["http_status"] != 200:
            return current
        values = {k: v for k, v in args.items() if k != "operation"}
        try:
            outcome = HELPERS[operation](current["os"], **values)
        except PresentationError as exc:
            refusal = error_response(exc.code, exc.status)
            return {
                "contract": CONTRACT_V2,
                "http_status": exc.status,
                "os": json.loads(refusal.body),
            }
        if operation in {"list", "read", "read_default", "export"}:
            return {
                "contract": CONTRACT_V2,
                "http_status": 200,
                "presentation": outcome,
            }
        result = await self._send_v2(
            "PUT", PRESENTATION, body=outcome, correlation=correlation
        )
        refusal = result.get("access_error")
        if refusal is not None and refusal["delivery"] == "not_sent":
            # This tool call already sent its GET, so as a whole it is unknown.
            refusal["delivery"] = "unknown"
        return result

    async def _send_v2(
        self, method, path, *, params=None, body=None, accept=None, correlation=None
    ) -> dict:
        raw = None
        if body is not None:
            raw = _compact(body)
            # The route's own API body policy, measured as sent, before HTTP.
            if len(raw) > body_limit(path, method):
                return access_error_v2(
                    "request_too_large",
                    status=413,
                    delivery="not_sent",
                    correlation=correlation,
                )
        try:
            token = await asyncio.to_thread(_credential, self.config.os_credential_file)
        except (OSError, ValueError, KeyError, TypeError):
            return access_error_v2(
                "local_credential_unavailable",
                delivery="not_sent",
                correlation=correlation,
            )
        headers = {"Authorization": "Bearer " + token}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        if accept is not None:
            headers["Accept"] = accept
        try:
            async with (
                asyncio.timeout(V2_TIMEOUT),
                self.http.stream(
                    method,
                    path,
                    params={k: str(v) for k, v in (params or {}).items()},
                    content=raw,
                    headers=headers,
                ) as response,
            ):
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > self.profile.max_api_response:
                        return access_error_v2(
                            "backend_response_too_large",
                            delivery="unknown",
                            correlation=correlation,
                        )
                status = response.status_code
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # No connection was made, so no request byte was sent.
            return access_error_v2(
                "backend_unavailable", delivery="not_sent", correlation=correlation
            )
        except (httpx.TimeoutException, TimeoutError):
            return access_error_v2(
                "backend_timeout", delivery="unknown", correlation=correlation
            )
        except (httpx.HTTPError, UnicodeError):
            return access_error_v2(
                "backend_unavailable", delivery="unknown", correlation=correlation
            )
        try:
            if 200 <= status < 300:
                if path == "/api/v1/events":
                    data = _event(bytes(chunks))
                else:
                    data = json.loads(chunks)
                return {"contract": CONTRACT_V2, "http_status": status, "os": data}
            Error.model_validate_json(chunks)
            return {
                "contract": CONTRACT_V2,
                "http_status": status,
                "os": json.loads(chunks),
            }
        except (ValidationError, ValueError, UnicodeError):
            return access_error_v2(
                "backend_http_" + str(status),
                status=status,
                delivery="unknown",
                correlation=correlation,
            )


def _event(raw: bytes) -> dict:
    """The single availability notification (`text/event-stream`) as JSON."""
    fields = {}
    for line in raw.decode("utf-8").splitlines():
        name, _, value = line.partition(": ")
        if name in {"event", "data"}:
            fields[name] = value
    if set(fields) != {"event", "data"}:
        raise ValueError("unexpected event stream")
    return {"event": fields["event"], "data": json.loads(fields["data"])}
