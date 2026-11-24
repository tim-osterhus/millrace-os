"""Optional foreground MCP transport over existing public OS controls."""

from __future__ import annotations

import asyncio
import importlib.metadata
import io
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import ValidationError

from . import __version__
from .mcp_auth import HTTPGuard, ResourceVerifier
from .mcp_client import BackendClient, advertised_v2, correlation_v2, mappings_v2
from .mcp_contracts import (
    CONTRACT,
    CONTRACT_V2,
    MAX_INPUT,
    MAX_OUTPUT,
    PROTOCOL,
    SDK_VERSION,
    V1,
    V2,
    V2_CONCURRENCY,
    V2_TIMEOUT,
    MCPConfig,
    access_error,
    access_error_v2,
    admit_arguments,
    admit_arguments_v2,
    schema_bundle,
    schema_bundle_v2,
)

__all__ = ["Bridge", "serve", "require_profile"]


def require_profile(python: tuple[int, int], sdk: str) -> None:
    if python not in {(3, 12), (3, 13)} or sdk != SDK_VERSION:
        raise ValueError("unsupported MCP profile")


def selected_protocol(version: object) -> bool:
    return version == PROTOCOL


class Bridge(FastMCP):
    """Use public FastMCP raw handlers to admit strict, fixed tools."""

    def __init__(
        self,
        config: MCPConfig,
        backend: BackendClient,
        verifier: ResourceVerifier | None = None,
        *,
        profile=V1,
    ) -> None:
        self.config, self.backend, self.profile = config, backend, profile
        self.active = 0
        if profile.tools == "v2":
            # The full fixed bundle; startup capabilities filter what is listed.
            self.bundle = schema_bundle_v2()
            self.advertised = advertised_v2(backend.capabilities)
        options = {}
        if verifier:
            options = {
                "token_verifier": verifier,
                "auth": AuthSettings(
                    issuer_url=config.issuer_url,
                    resource_server_url=config.resource_url,
                    required_scopes=["millrace:operator"],
                    validate_token_resource=True,
                ),
                "transport_security": TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=config.allowed_hosts,
                    allowed_origins=config.allowed_origins,
                ),
                "streamable_http_path": urlsplit(config.resource_url).path or "/mcp",
            }
        super().__init__(
            "Millrace OS",
            json_response=True,
            stateless_http=True,
            max_request_body_size=profile.max_input,
            log_level="CRITICAL",
            **options,
        )

    def _admitted(self) -> bool:
        context = self.get_context()
        request = context.request_context.request
        if self.config.transport == "streamable-http":
            return request is not None and selected_protocol(
                request.headers.get("mcp-protocol-version")
            )
        params = context.session.client_params
        return params is not None and selected_protocol(params.protocolVersion)

    async def list_tools(self) -> list[Tool]:
        if not self._admitted():
            raise ValueError("unsupported_protocol")
        if self.profile.tools == "v2":
            return [
                Tool(**tool)
                for tool in self.bundle["tools"]
                if tool["name"] in self.advertised
            ]
        return [Tool(**tool) for tool in schema_bundle()["tools"]]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if self.profile.tools == "v2":
            return await self._call_v2(name, arguments)
        if not self._admitted():
            return access_error("unsupported_protocol", status=400)
        try:
            args = admit_arguments(name, arguments, self.config.homes)
        except (ValidationError, ValueError, TypeError):
            return access_error("invalid_arguments", status=422)
        if self.active >= 8:
            return access_error("busy", status=429, retryable=True)
        self.active += 1
        try:
            if name == "millrace_v1_discover":
                result = {
                    "contract": CONTRACT,
                    "package": "millrace-os",
                    "package_version": __version__,
                    "sdk_version": SDK_VERSION,
                    "protocol": PROTOCOL,
                    "api": "v1",
                    "homes": self.config.homes,
                    "tool_schema_sha256": schema_bundle()["sha256"],
                    "tools": list(schema_bundle()["tools"]),
                    "router_read": "millrace-os-router-read/v1",
                    "router_call": "millrace-os-router-call/v1",
                }
            else:
                async with asyncio.timeout(5):
                    envelope = await self.backend.call(name, args)
                result = (
                    envelope
                    if "access_error" in envelope
                    else {"contract": CONTRACT, "os": envelope}
                )
            # SDK emits both structured content and a JSON-text fallback.
            # Bound their combined wire response rather than either copy alone.
            wire = CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result, indent=2))],
                structuredContent=result,
            ).model_dump_json(by_alias=True, exclude_none=True)
            # Reserve the entire possible incoming JSON-RPC ID and wrapper.
            if len(wire.encode()) + MAX_INPUT + 1024 > MAX_OUTPUT:
                return access_error(
                    "response_too_large", correlation=_correlation(args)
                )
            return result
        except TimeoutError:
            return access_error("backend_timeout", correlation=_correlation(args))
        except Exception:
            # The external boundary must never serialize exception text/inputs.
            return access_error("backend_unavailable", correlation=_correlation(args))
        finally:
            self.active -= 1

    async def _call_v2(self, name: str, arguments: dict) -> CallToolResult:
        """Admit, route and answer one v2 call; 202 is returned, never awaited."""
        if not self._admitted():
            return self._v2_result(
                access_error_v2("unsupported_protocol", status=400, delivery="not_sent")
            )
        try:
            args = admit_arguments_v2(name, arguments, self.config.homes)
        except (ValidationError, ValueError, TypeError):
            return self._v2_result(
                access_error_v2("invalid_arguments", status=422, delivery="not_sent")
            )
        correlation = correlation_v2(args)
        if name not in self.advertised:
            return self._v2_result(
                access_error_v2(
                    "tool_unavailable", delivery="not_sent", correlation=correlation
                )
            )
        if self.active >= V2_CONCURRENCY:
            return self._v2_result(
                access_error_v2(
                    "busy",
                    status=429,
                    retryable=True,
                    delivery="not_sent",
                    correlation=correlation,
                )
            )
        self.active += 1
        try:
            if name == "millrace_v2_discover" and args["kind"] == "bridge":
                payload = self._bridge()
            else:
                async with asyncio.timeout(V2_TIMEOUT):
                    payload = await self.backend.call_v2(name, args)
            return self._v2_result(payload, correlation)
        except TimeoutError:
            return self._v2_result(
                access_error_v2(
                    "backend_timeout", delivery="unknown", correlation=correlation
                )
            )
        except Exception:
            # The external boundary must never serialize exception text/inputs.
            return self._v2_result(
                access_error_v2(
                    "backend_unavailable", delivery="unknown", correlation=correlation
                )
            )
        finally:
            self.active -= 1

    def _v2_result(self, payload: dict, correlation=None) -> CallToolResult:
        """Compact text plus the same structured value; `isError` when refused."""
        result = _tool_result(payload)
        wire = result.model_dump_json(by_alias=True, exclude_none=True)
        # Reserve the entire possible incoming message (and its ID) plus the
        # JSON-RPC wrapper, as v1 does, with the v2 bounds.
        if len(wire.encode()) + self.profile.max_input + 1024 > (
            self.profile.max_output
        ):
            # One small fixed refusal (its correlation holds only checked IDs).
            # A refusal that happened before any HTTP stays `not_sent`.
            prior = payload.get("access_error")
            unsent = isinstance(prior, dict) and prior.get("delivery") == "not_sent"
            return _tool_result(
                access_error_v2(
                    "response_too_large",
                    delivery="not_sent" if unsent else "unknown",
                    correlation=correlation,
                )
            )
        return result

    def _bridge(self) -> dict:
        """Bridge-local self-description; reads no OS state."""
        return {
            "contract": CONTRACT_V2,
            "package_version": __version__,
            "sdk_version": SDK_VERSION,
            "protocol": PROTOCOL,
            "api": "v1",
            "homes": self.config.homes,
            "tool_schema_sha256": self.bundle["sha256"],
            "tools": list(self.advertised),
            "mappings": mappings_v2(),
            "limits": {
                "max_input": self.profile.max_input,
                "max_output": self.profile.max_output,
                "max_api_response": self.profile.max_api_response,
                "backend_timeout_seconds": V2_TIMEOUT,
                "concurrency": V2_CONCURRENCY,
            },
        }


def _tool_result(payload: dict) -> CallToolResult:
    error = "access_error" in payload or not (
        200 <= payload.get("http_status", 200) < 300
    )
    text = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=error,
    )


def _correlation(args: dict) -> dict:
    result = {
        k: args[k] for k in ("home", "instance_id", "run_id", "request_id") if k in args
    }
    request = args.get("request", {})
    result.update(
        {k: request[k] for k in ("request_id", "instance_id", "run_id") if k in request}
    )
    return result


class _Output(io.RawIOBase):
    """Process-owned binary stdout adapter; SDK still owns protocol handling."""

    def __init__(self, stream, limit=MAX_OUTPUT) -> None:
        self.stream, self.pending, self.limit = stream, bytearray(), limit
        self.overflow = False
        self.lock = threading.Lock()

    def writable(self):
        return True

    def write(self, data):
        with self.lock:
            for piece in bytes(data).splitlines(keepends=True):
                if not self.overflow:
                    self.pending.extend(piece)
                    self.overflow = len(self.pending) > self.limit
                if piece.endswith(b"\n"):
                    if self.overflow:
                        self.stream.write(_wire_error("response_too_large"))
                    else:
                        self.stream.write(_sanitize_wire(bytes(self.pending)))
                    self.stream.flush()
                    self.pending.clear()
                    self.overflow = False
        return len(data)

    def flush(self):
        self.stream.flush()


def _wire_error(code: str) -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32000,
                    "message": code,
                },
            }
        )
        + "\n"
    ).encode()


def _sanitize_wire(raw: bytes) -> bytes:
    """SDK validation errors must not reflect untrusted values into protocol output."""
    try:
        value = json.loads(raw)
        if isinstance(value.get("error"), dict):
            value["error"].pop("data", None)
            if value["error"].get("message") not in {
                "request_too_large",
                "response_too_large",
                "unsupported_protocol",
            }:
                value["error"]["message"] = "invalid_request"
            return (json.dumps(value) + "\n").encode()
    except (ValueError, AttributeError):
        return _wire_error("invalid_request")
    return raw


class _Input(io.RawIOBase):
    """Whole-line ingress bound; the limit counts wire bytes including LF."""

    def __init__(self, stream, output: _Output, limit=MAX_INPUT) -> None:
        self.stream, self.output, self.limit = stream, output, limit
        self.pending = b""

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.pending:
            line = self.stream.readline(self.limit + 1)
            if not line:
                return 0
            if len(line) > self.limit:
                while line and not line.endswith(b"\n"):
                    line = self.stream.readline(self.limit + 1)
                self.output.write(_wire_error("request_too_large"))
                continue
            self.pending = line
        count = min(len(buffer), len(self.pending))
        buffer[:count], self.pending = self.pending[:count], self.pending[count:]
        return count


@contextmanager
def bounded_stdio(profile=V1):
    """Restore process streams after the unchanged public SDK stdio runner."""
    stdin, stdout = sys.stdin, sys.stdout
    v1 = profile.tools == "v1"
    output = (
        _Output(stdout.buffer) if v1 else _Output(stdout.buffer, profile.max_output)
    )
    ingress = (
        _Input(stdin.buffer, output)
        if v1
        else _Input(stdin.buffer, output, profile.max_input)
    )
    sys.stdin = io.TextIOWrapper(io.BufferedReader(ingress), encoding="utf-8")
    sys.stdout = io.TextIOWrapper(io.BufferedWriter(output), encoding="utf-8")
    try:
        yield
    finally:
        try:
            if not sys.stdout.closed:
                sys.stdout.flush()
        finally:
            sys.stdin, sys.stdout = stdin, stdout


async def _close_http_resources(
    backend: BackendClient, verifier: ResourceVerifier
) -> None:
    try:
        await backend.close()
    finally:
        await verifier.close()


async def _serve(config: MCPConfig, profile=V1) -> None:
    # The v1 profile constructs every class exactly as before.
    selected = {} if profile.tools == "v1" else {"profile": profile}
    backend = BackendClient(config, **selected)
    verifier = (
        ResourceVerifier(config) if config.transport == "streamable-http" else None
    )
    try:
        await backend.startup()
        bridge = Bridge(config, backend, verifier, **selected)
        if config.transport == "stdio":
            with bounded_stdio(profile):
                await bridge.run_stdio_async()
        else:
            import uvicorn
            from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

            # Admission observes the real peer before trusted forwarding is applied.
            inner = ProxyHeadersMiddleware(
                bridge.streamable_http_app(), trusted_hosts=config.proxy_peer
            )
            app = HTTPGuard(inner, config, **selected)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=config.listen_port,
                    proxy_headers=False,
                    access_log=False,
                    log_level="critical",
                )
            )
            await server.serve()
    finally:
        if verifier is None:
            await backend.close()
        else:
            # Uvicorn can replay SIGINT after ASGI shutdown and cancel this task.
            closing = asyncio.create_task(_close_http_resources(backend, verifier))
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await closing
                raise


def serve(path: str, transport: str, tools: str = "v1") -> None:
    require_profile(sys.version_info[:2], importlib.metadata.version("mcp"))
    profile = {"v1": V1, "v2": V2}[tools]
    raw = Path(path).read_bytes()
    if len(raw) > MAX_INPUT:
        raise ValueError("invalid MCP configuration")
    config = MCPConfig.model_validate_json(raw)
    if config.transport != transport:
        raise ValueError("transport mismatch")
    asyncio.run(_serve(config) if profile is V1 else _serve(config, profile))
