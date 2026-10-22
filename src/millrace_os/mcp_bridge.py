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
from .mcp_client import BackendClient
from .mcp_contracts import (
    CONTRACT,
    MAX_INPUT,
    MAX_OUTPUT,
    PROTOCOL,
    SDK_VERSION,
    MCPConfig,
    access_error,
    admit_arguments,
    schema_bundle,
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
    ) -> None:
        self.config, self.backend = config, backend
        self.active = 0
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
            max_request_body_size=MAX_INPUT,
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
        return [Tool(**tool) for tool in schema_bundle()["tools"]]

    async def call_tool(self, name: str, arguments: dict) -> dict:
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

    def __init__(self, stream) -> None:
        self.stream, self.pending = stream, bytearray()
        self.overflow = False
        self.lock = threading.Lock()

    def writable(self):
        return True

    def write(self, data):
        with self.lock:
            for piece in bytes(data).splitlines(keepends=True):
                if not self.overflow:
                    self.pending.extend(piece)
                    self.overflow = len(self.pending) > MAX_OUTPUT
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
    def __init__(self, stream, output: _Output) -> None:
        self.stream, self.output = stream, output
        self.pending = b""

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.pending:
            line = self.stream.readline(MAX_INPUT + 1)
            if not line:
                return 0
            if len(line) > MAX_INPUT:
                while line and not line.endswith(b"\n"):
                    line = self.stream.readline(MAX_INPUT + 1)
                self.output.write(_wire_error("request_too_large"))
                continue
            self.pending = line
        count = min(len(buffer), len(self.pending))
        buffer[:count], self.pending = self.pending[:count], self.pending[count:]
        return count


@contextmanager
def bounded_stdio():
    """Restore process streams after the unchanged public SDK stdio runner."""
    stdin, stdout = sys.stdin, sys.stdout
    output = _Output(stdout.buffer)
    sys.stdin = io.TextIOWrapper(
        io.BufferedReader(_Input(stdin.buffer, output)), encoding="utf-8"
    )
    sys.stdout = io.TextIOWrapper(io.BufferedWriter(output), encoding="utf-8")
    try:
        yield
    finally:
        try:
            if not sys.stdout.closed:
                sys.stdout.flush()
        finally:
            sys.stdin, sys.stdout = stdin, stdout


async def _serve(config: MCPConfig) -> None:
    backend = BackendClient(config)
    verifier = (
        ResourceVerifier(config) if config.transport == "streamable-http" else None
    )
    try:
        await backend.startup()
        bridge = Bridge(config, backend, verifier)
        if config.transport == "stdio":
            with bounded_stdio():
                await bridge.run_stdio_async()
        else:
            import uvicorn
            from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

            # Admission observes the real peer before trusted forwarding is applied.
            inner = ProxyHeadersMiddleware(
                bridge.streamable_http_app(), trusted_hosts=config.proxy_peer
            )
            app = HTTPGuard(inner, config)
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
        await backend.close()
        if verifier:
            await verifier.close()


def serve(path: str, transport: str) -> None:
    require_profile(sys.version_info[:2], importlib.metadata.version("mcp"))
    raw = Path(path).read_bytes()
    if len(raw) > MAX_INPUT:
        raise ValueError("invalid MCP configuration")
    config = MCPConfig.model_validate_json(raw)
    if config.transport != transport:
        raise ValueError("transport mismatch")
    asyncio.run(_serve(config))
