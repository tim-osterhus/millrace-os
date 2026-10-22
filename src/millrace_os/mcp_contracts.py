"""Strict optional MCP configuration and the fixed v1 tool surface."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .contracts import Identifier

__all__ = ["MCPConfig", "TOOL_MODELS", "admit_arguments", "schema_bundle"]
PROTOCOL = "2025-11-25"
SDK_VERSION = "1.30.0"
CONTRACT = "millrace-os-mcp-tools/v1"
MAX_INPUT = 65536
MAX_OUTPUT = 1048576
OPERATIONS = frozenset(
    {
        "create_instance",
        "start_run",
        "pause_run",
        "cancel_run",
        "resume_run",
        "update_instance_config",
        "submit_revision",
        "accept_revision",
        "pause_plane",
        "resume_plane",
        "cancel_plane",
        "redirect_plane",
        "retry_invocation",
    }
)
UUID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Entry = Annotated[str, Field(min_length=1, max_length=512)]
Entries = Annotated[list[Entry], Field(min_length=1, max_length=32)]
Endpoint = Annotated[str, Field(min_length=1, max_length=2048)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MCPConfig(Strict):
    format: Literal["millrace-os-mcp-config/v1"]
    os_origin: Endpoint
    os_credential_file: Annotated[str, Field(min_length=1, max_length=4096)]
    homes: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    transport: Literal["stdio", "streamable-http"]
    resource_url: Endpoint | None = None
    issuer_url: Endpoint | None = None
    jwks_url: Endpoint | None = None
    trusted_subjects: Entries | None = None
    allowed_hosts: Entries | None = None
    allowed_origins: Entries | None = None
    listen_port: Annotated[int, Field(ge=1024, le=65535)] = 8792
    proxy_peer: str = "127.0.0.1"

    @model_validator(mode="after")
    def deployment(self) -> MCPConfig:
        origin = urlsplit(self.os_origin)
        if (
            origin.scheme != "http"
            or not ipaddress.ip_address(origin.hostname).is_loopback
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
            or origin.port is None
            or origin.port == 0
            or "@" in origin.netloc
            or "?" in self.os_origin
            or "#" in self.os_origin
            or any(c.isspace() for c in self.os_origin)
        ):
            raise ValueError("os_origin must be an exact literal loopback origin")
        if len(self.homes) != len(set(self.homes)):
            raise ValueError("duplicate home")
        remote = {
            "resource_url",
            "issuer_url",
            "jwks_url",
            "trusted_subjects",
            "allowed_hosts",
            "allowed_origins",
            "listen_port",
            "proxy_peer",
        }
        if self.transport == "stdio":
            if self.model_fields_set & remote:
                raise ValueError("stdio rejects HTTP deployment settings")
            return self
        for name in remote - {"listen_port", "proxy_peer"}:
            if getattr(self, name) is None:
                raise ValueError("HTTP deployment settings are required")
        if not ipaddress.ip_address(self.proxy_peer).is_loopback:
            raise ValueError("proxy peer must be loopback")
        for name in ("resource_url", "issuer_url", "jwks_url"):
            value = getattr(self, name)
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or "@" in parsed.netloc
                or "?" in value
                or "#" in value
                or any(c.isspace() for c in value)
                or (parsed.port is not None and parsed.port == 0)
            ):
                raise ValueError("deployment URLs must be fixed HTTPS URLs")
        for name in ("trusted_subjects", "allowed_hosts", "allowed_origins"):
            values = getattr(self, name)
            if len(values) != len(set(values)) or any(
                not v.strip() or "*" in v or any(c.isspace() for c in v) for v in values
            ):
                raise ValueError("invalid deployment list")
        for host in self.allowed_hosts:
            if any(c in host for c in "/?#@"):
                raise ValueError("invalid host")
        for value in self.allowed_origins:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or "@" in parsed.netloc
                or "?" in value
                or "#" in value
            ):
                raise ValueError("invalid origin")
        return self


class Discover(Strict):
    pass


class Home(Strict):
    home: Identifier


class Instance(Home):
    instance_id: UUID


class Run(Instance):
    run_id: UUID


class Request(Home):
    request: dict[str, JsonValue]


class Validate(Instance):
    run_id: UUID | None = None


class Receipt(Validate):
    request_id: UUID


TOOL_MODELS = {
    "millrace_v1_discover": Discover,
    "millrace_v1_instances": Home,
    "millrace_v1_instance": Instance,
    "millrace_v1_run": Run,
    "millrace_v1_request": Request,
    "millrace_v1_receipt": Receipt,
    "millrace_v1_validate": Validate,
}


def admit_arguments(name: str, arguments: object, homes: list[str]) -> dict:
    """Check raw arguments, preserving the original public Core request body."""
    if name not in TOOL_MODELS:
        raise ValueError("unknown tool")
    model = TOOL_MODELS[name].model_validate(arguments)
    values = model.model_dump(exclude_unset=True)
    if "home" in values and values["home"] not in homes:
        raise ValueError("unknown home")
    if name == "millrace_v1_request":
        request = values["request"]
        if request.get("operation") not in OPERATIONS:
            raise ValueError("unsupported operation")
        request_id = request.get("request_id")
        if (
            not isinstance(request_id, str)
            or len(request_id) != 32
            or any(c not in "0123456789abcdef" for c in request_id)
        ):
            raise ValueError("invalid request UUID")
        # Full request/v2 field/operation validation belongs to public Core.
    return values


def schema_bundle() -> dict:
    tools = [
        {"name": name, "inputSchema": model.model_json_schema()}
        for name, model in TOOL_MODELS.items()
    ]
    raw = json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
    return {
        "contract": CONTRACT,
        "tools": tools,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def access_error(
    code: str,
    *,
    status: int = 503,
    retryable: bool = False,
    correlation: dict | None = None,
) -> dict:
    return {
        "contract": CONTRACT,
        "access_error": {
            "code": code,
            "status": status,
            "retryable": retryable,
            "observation": "unknown",
            "correlation": correlation or {},
        },
    }
