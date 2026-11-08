"""Strict optional MCP configuration and the fixed v1 and v2 tool surfaces."""

from __future__ import annotations

import functools
import hashlib
import ipaddress
import json
import math
import operator
import re
from typing import Annotated, Any, Literal, NamedTuple
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    WithJsonSchema,
    create_model,
    model_validator,
)

from .contracts import Identifier

__all__ = [
    "V1",
    "V2",
    "MCPConfig",
    "Profile",
    "TOOL_MODELS",
    "TOOL_MODELS_V2",
    "access_error_v2",
    "admit_arguments",
    "admit_arguments_v2",
    "schema_bundle",
    "schema_bundle_v2",
]
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


# --- v2: a separate, explicitly selected bridge profile (OS-G0 7.4-7.6). ---
# The v1 objects above are unchanged. Framing follows G0 7.5 as amended by the
# N11 source fit (B7-os/n11-framing-amendment.json). The pinned SDK writes a
# finite float token of Python length p in at most 2p bytes (`1e-05` becomes
# `0.00001`), so a request embedding a B-byte compact API body may need 2B on
# the wire; A stays the allowance for everything else, counted in wire bytes
# including the stdio LF. Per JSON token, escaped text plus the structured
# copy is at most 3p, so the result stays within 3R + 1,024 without assuming
# the structured copy is shorter than the text. The measured SDK wire check
# remains authoritative.

CONTRACT_V2 = "millrace-os-mcp-tools/v2"
V2_LARGE_BODY = 1048576  # B: the largest admitted API body
V2_MAX_INPUT = 2 * V2_LARGE_BODY + MAX_INPUT  # I2 = 2B + A = 2,162,688
V2_MAX_API_RESPONSE = V2_LARGE_BODY + 1024  # R = 1,049,600
V2_MAX_OUTPUT = V2_MAX_INPUT + 1024 + (3 * V2_MAX_API_RESPONSE + 1024)  # O2
V2_TIMEOUT = 5  # backend and tool observation seconds, as v1
V2_CONCURRENCY = 8


class Profile(NamedTuple):
    """The bounds one selected tool profile applies at every boundary."""

    tools: str
    max_input: int
    max_output: int
    max_api_response: int


V1 = Profile("v1", MAX_INPUT, MAX_OUTPUT, MAX_OUTPUT)  # the existing values
V2 = Profile("v2", V2_MAX_INPUT, V2_MAX_OUTPUT, V2_MAX_API_RESPONSE)


def access_error_v2(
    code: str,
    *,
    delivery: Literal["not_sent", "unknown"],
    status: int = 503,
    retryable: bool = False,
    correlation: dict | None = None,
) -> dict:
    """A v2 transport refusal. `not_sent` only if no HTTP byte was sent."""
    return {
        "contract": CONTRACT_V2,
        "access_error": {
            "code": code,
            "status": status,
            "retryable": retryable,
            "delivery": delivery,
            "correlation": correlation or {},
        },
    }
OPERATIONS_V2 = frozenset(
    {
        "create_instance",
        "start_run",
        "recover_run",
        "pause_run",
        "cancel_run",
        "resume_run",
        "submit_revision",
        "accept_revision",
        "retry_invocation",
        "pause_plane",
        "resume_plane",
        "cancel_plane",
        "redirect_plane",
        "reconcile_invocation",
        "update_instance_config",
        "release_project_hold",
    }
)
STOCK_FRONTEND = "stock"
STOCK_FORMAT = "millrace-stock-presentation/v1"
STOCK_BUNDLED = ("precision", "command", "studio")

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Cursor = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
]
Limit = Annotated[int, Field(ge=1, le=50)]
OutputId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
FrontendId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
FormatId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")]
DraftPath = Annotated[str, Field(min_length=1, max_length=512)]
Content = Annotated[str, Field(max_length=1048576)]
LongText = Annotated[str, Field(min_length=1, max_length=4096)]
Observation = Literal["millrace-os-router-call/v1", "millrace-os-router-call/v2"]
_NOT_LABEL = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")


def _label(value: str) -> str:
    """A one-line plain-text display label (no control characters)."""
    if _NOT_LABEL.search(value):
        raise ValueError("label must be one line of plain text")
    return value


def _ratio(value: Any) -> Any:
    # Compared without float conversion, so a huge integer cannot overflow.
    if type(value) not in (int, float) or not 0 < value < 1:
        raise ValueError("split ratio must be a number strictly between 0 and 1")
    return value


Label = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_label)]
Ratio = Annotated[
    Any,
    AfterValidator(_ratio),
    WithJsonSchema({"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1}),
]


def _finite(value: object) -> None:
    if type(value) is float and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if type(value) is list:
        for item in value:
            _finite(item)
    elif type(value) is dict:
        for item in value.values():
            _finite(item)


class StockPanels(Strict):
    planes: bool
    current_work: bool = Field(alias="current-work")
    graph: bool
    inspector: bool
    history: bool
    revisions: bool


class StockRatios(Strict):
    detail_split: Ratio = Field(alias="detail-split")
    status_stack: Ratio = Field(alias="status-stack")
    graph_split: Ratio = Field(alias="graph-split")


class StockPayload(Strict):
    """`millrace-stock-presentation/v1` (OS-G0 6.3)."""

    preset: Literal["precision", "command", "studio"]
    theme: Literal["system", "light", "dark"]
    density: Literal["comfortable", "compact"]
    arrangement: Literal["detail-split", "status-stack", "graph-split"]
    panels: StockPanels
    split_ratios: StockRatios


def _payload(stock: bool, payload: dict) -> None:
    """Stock payloads validate strictly; custom ones are finite JSON objects."""
    if stock:
        StockPayload.model_validate(payload)
    else:
        _finite(payload)


class Namespace(Strict):
    frontend_id: FrontendId
    presentation_format: FormatId

    @model_validator(mode="after")
    def stock_format(self) -> Namespace:
        if self.frontend_id == STOCK_FRONTEND and self.presentation_format != (
            STOCK_FORMAT
        ):
            raise ValueError("the stock frontend uses only the stock format")
        return self

    @property
    def stock(self) -> bool:
        return self.presentation_format == STOCK_FORMAT and (
            self.frontend_id == STOCK_FRONTEND
        )


class UserRef(Strict):
    kind: Literal["user"]
    id: UUID


class BundledRef(Strict):
    kind: Literal["bundled"]
    id: Literal["precision", "command", "studio", "default"]


ConfigRef = Annotated[UserRef | BundledRef, Field(discriminator="kind")]


def _bundled_fits(stock: bool, ref: object) -> None:
    if isinstance(ref, BundledRef) and (ref.id in STOCK_BUNDLED) != stock:
        raise ValueError("bundled ID is not valid for this namespace")


class _Namespaced(Strict):
    """Base of a presentation alternative with a `namespace` field: references
    and payload must fit that namespace."""

    @model_validator(mode="after")
    def namespace_rules(self) -> _Namespaced:
        stock = self.namespace.stock
        fields = type(self).model_fields
        for name in ("ref", "source", "default"):
            if name in fields:
                ref = getattr(self, name)
                if ref is None and stock:
                    raise ValueError("a stock namespace always has a default")
                _bundled_fits(stock, ref)
        if "payload" in fields:
            _payload(stock, self.payload)
        return self


class PortableConfig(Strict):
    """`millrace-os-presentation-config/v2` (OS-G0 6.6)."""

    format: Literal["millrace-os-presentation-config/v2"]
    frontend_id: FrontendId
    presentation_format: FormatId
    name: Label
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def namespace_rules(self) -> PortableConfig:
        namespace = Namespace(
            frontend_id=self.frontend_id, presentation_format=self.presentation_format
        )
        _payload(namespace.stock, self.payload)
        return self


class WriteCompare(Strict):
    format: Literal["millrace-os-presentation-write/v1"]
    mode: Literal["compare"]
    expected_version: UUID | None
    store: dict[str, JsonValue]


class WriteReplace(Strict):
    format: Literal["millrace-os-presentation-write/v1"]
    mode: Literal["replace_unreadable"]
    observed_file_sha256: Digest
    store: dict[str, JsonValue]


PresentationWrite = Annotated[WriteCompare | WriteReplace, Field(discriminator="mode")]


class _LifecycleTarget(Strict):
    @model_validator(mode="after")
    def run_needs_instance(self) -> _LifecycleTarget:
        if "run_id" in self.model_fields_set and "instance_id" not in (
            self.model_fields_set
        ):
            raise ValueError("run_id requires instance_id")
        return self


class _CopySource(Strict):
    @model_validator(mode="after")
    def one_source(self) -> _CopySource:
        if len({"source", "draft_id"} & self.model_fields_set) != 1:
            raise ValueError("copy takes exactly one of source or draft_id")
        return self


class DraftFile(Strict):
    path: DraftPath
    content_base64: Content


Files = Annotated[list[DraftFile], Field(max_length=256)]
_HOME = (Identifier, ...)
Version = UUID | None  # expected_version: nullable, and omitted differs from null


def _no_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


def _omit(kind: object) -> tuple[object, Any]:
    """An omittable field: absence is meaningful, so its Python default None
    is not advertised as a schema default. Each field gets its own FieldInfo,
    because Pydantic may update a FieldInfo passed as a default."""
    return kind, Field(default=None, json_schema_extra=_no_default)


def _alternatives(tool: str, field: str, members: dict) -> object:
    """A strict discriminated union, one model per alternative."""
    models = []
    for value, spec in members.items():
        base, fields = spec if isinstance(spec, tuple) else (Strict, spec)
        title = "".join(part.title() for part in f"{tool}_{value}".split("_"))
        models.append(
            create_model(
                title, __base__=base, **{field: (Literal[value], ...)}, **fields
            )
        )
    return Annotated[functools.reduce(operator.or_, models), Field(discriminator=field)]


def _model(title: str, **fields: object) -> type[BaseModel]:
    return create_model(title, __base__=Strict, **fields)


def _page() -> dict[str, object]:
    return {"limit": _omit(Limit), "cursor": _omit(Cursor)}


_TARGET = {"instance_id": (UUID, ...), "run_id": (UUID, ...)}
_NAMESPACED = {"namespace": (Namespace, ...)}

TOOL_SPECS_V2 = {
    "millrace_v2_discover": _alternatives(
        "discover",
        "kind",
        {
            "bridge": {},
            "health": {},
            "identity": {},
            "version": {},
            "contracts": {},
            "schema": {"contract_id": (Identifier, ...), "digest": (Digest, ...)},
            "diagnostics": {},
            "providers": {},
            "actions": {},
            "availability": {},
            "capabilities": {},
            "observation": {"home": _HOME},
        },
    ),
    "millrace_v2_inspect": _alternatives(
        "inspect",
        "target",
        {
            "homes": _page(),
            "instances": {"home": _HOME, **_page()},
            "instance": {"home": _HOME, "instance_id": (UUID, ...), **_page()},
            "run": {
                "home": _HOME,
                **_TARGET,
                "section": (
                    Literal["summary", "invocations", "transitions", "controls"],
                    ...,
                ),
                **_page(),
                "request_id": _omit(UUID),
                "invocation_id": _omit(UUID),
            },
            "revision": {"home": _HOME, **_TARGET, "revision_id": (Digest, ...)},
        },
    ),
    "millrace_v2_request": _model(
        "Request",
        home=_HOME,
        request=(dict[str, JsonValue], ...),
        observation_contract=(Observation, ...),
    ),
    "millrace_v2_receipt": _alternatives(
        "receipt",
        "mode",
        {
            "lifecycle": (
                _LifecycleTarget,
                {
                    "home": _HOME,
                    "request_id": (UUID, ...),
                    "instance_id": _omit(UUID),
                    "run_id": _omit(UUID),
                    "observation_contract": (Observation, ...),
                },
            ),
            "bounded": {
                "home": _HOME,
                "request_id": (UUID, ...),
                "instance_id": (UUID, ...),
                "run_id": _omit(UUID),
                "observation_contract": (Observation, ...),
            },
        },
    ),
    "millrace_v2_validate": _alternatives(
        "validate",
        "kind",
        {
            "configured": {
                "home": _HOME,
                "instance_id": (UUID, ...),
                "run_id": _omit(UUID),
            },
            "draft": {
                "home": _HOME,
                "draft_id": (UUID, ...),
                "instance_id": (UUID, ...),
                "run_id": _omit(UUID),
            },
            "summary": {
                "home": _HOME,
                "instance_id": (UUID, ...),
                "run_id": (UUID | None, ...),
            },
        },
    ),
    "millrace_v2_catalog": _alternatives(
        "catalog",
        "operation",
        {
            "list": {"home": _HOME},
            "init": {
                "home": _HOME,
                "selector": (Annotated[str, Field(min_length=1, max_length=256)], ...),
                "destination": (LongText, ...),
            },
            "copy": (
                _CopySource,
                {
                    "home": _HOME,
                    "destination": (LongText, ...),
                    "source": _omit(LongText),
                    "draft_id": _omit(UUID),
                },
            ),
        },
    ),
    "millrace_v2_draft": _alternatives(
        "draft",
        "operation",
        {
            "create": {"home": _HOME, "files": (Files, ...)},
            "read": {"home": _HOME, "draft_id": (UUID, ...)},
            "replace_files": {
                "home": _HOME,
                "draft_id": (UUID, ...),
                "files": (Files, ...),
            },
            "put_file": {
                "home": _HOME,
                "draft_id": (UUID, ...),
                "path": (DraftPath, ...),
                "content_base64": (Content, ...),
            },
            "manifest": {"home": _HOME, "draft_id": (UUID, ...), **_page()},
            "read_file": {
                "home": _HOME,
                "draft_id": (UUID, ...),
                "path": (DraftPath, ...),
            },
            "apply": {
                "home": _HOME,
                "draft_id": (UUID, ...),
                **_TARGET,
                "base_state_version": (Annotated[int, Field(ge=1)], ...),
                "request_id": (UUID, ...),
            },
            "repair_publication": {"home": _HOME, "instance_id": (UUID, ...)},
            "publication_status": {
                "home": _HOME,
                "instance_id": (UUID, ...),
                "request_id": (UUID, ...),
            },
        },
    ),
    "millrace_v2_local_call": _model(
        "LocalCall", home=_HOME, request_id=(UUID, ...)
    ),
    "millrace_v2_outputs": _alternatives(
        "outputs",
        "operation",
        {
            "publish": {"home": _HOME, "record": (dict[str, JsonValue], ...)},
            "list": {"home": _HOME, **_TARGET, **_page()},
            "read": {
                "home": _HOME,
                **_TARGET,
                "output_id": (OutputId, ...),
                "record_sha256": (Digest, ...),
                "representation": (Literal["report", "json"], ...),
            },
        },
    ),
    # Exactly the fifteen OS-G0 7.4.1 alternatives; no home.
    "millrace_v2_presentation": _alternatives(
        "presentation",
        "operation",
        {
            "get": {},
            "put": {"write": (PresentationWrite, ...)},
            "replace_unreadable": {
                "observed_file_sha256": (Digest, ...),
                "store": (dict[str, JsonValue], ...),
            },
            "list": {"namespace": _omit(Namespace)},
            "read": (_Namespaced, {**_NAMESPACED, "ref": (ConfigRef, ...)}),
            "read_default": (_Namespaced, _NAMESPACED),
            "export": (_Namespaced, {**_NAMESPACED, "ref": (ConfigRef, ...)}),
            "create": (
                _Namespaced,
                {
                    **_NAMESPACED,
                    "name": (Label, ...),
                    "payload": (dict[str, JsonValue], ...),
                    "expected_version": _omit(Version),
                },
            ),
            "update": (
                _Namespaced,
                {
                    **_NAMESPACED,
                    "config_id": (UUID, ...),
                    "payload": (dict[str, JsonValue], ...),
                    "expected_version": _omit(Version),
                },
            ),
            "rename": (
                _Namespaced,
                {
                    **_NAMESPACED,
                    "config_id": (UUID, ...),
                    "name": (Label, ...),
                    "expected_version": _omit(Version),
                },
            ),
            "duplicate": (
                _Namespaced,
                {
                    **_NAMESPACED,
                    "source": (ConfigRef, ...),
                    "name": (Label, ...),
                    "expected_version": _omit(Version),
                },
            ),
            "delete": (
                _Namespaced,
                {
                    **_NAMESPACED,
                    "config_id": (UUID, ...),
                    "expected_version": _omit(Version),
                },
            ),
            "set_default": (
                _Namespaced,
                {
                    **_NAMESPACED,
                    "default": (ConfigRef | None, ...),
                    "expected_version": _omit(Version),
                },
            ),
            "reset": (
                _Namespaced,
                {
                    **_NAMESPACED,
                    "config_id": (UUID, ...),
                    "expected_version": _omit(Version),
                },
            ),
            "import": {
                "config": (PortableConfig, ...),
                "name": _omit(Label),
                "expected_version": _omit(Version),
            },
        },
    ),
}
TOOL_MODELS_V2 = {name: TypeAdapter(spec) for name, spec in TOOL_SPECS_V2.items()}


def admit_arguments_v2(name: str, arguments: object, homes: list[str]) -> dict:
    """Admit v2 arguments before any HTTP: alternative, grammar, home allowlist.

    The dump keeps only supplied fields, so an omitted `expected_version`
    stays distinct from an explicit null. Request bodies stay unchanged. No
    JSON value anywhere may be NaN or infinite.
    """
    if name not in TOOL_MODELS_V2:
        raise ValueError("unknown tool")
    values = TOOL_MODELS_V2[name].validate_python(arguments).model_dump(
        exclude_unset=True, by_alias=True
    )
    _finite(values)
    if "home" in values and values["home"] not in homes:
        raise ValueError("unknown home")
    if name == "millrace_v2_request":
        request = values["request"]
        if request.get("operation") not in OPERATIONS_V2:
            raise ValueError("unsupported operation")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", request_id
        ):
            raise ValueError("invalid request UUID")
    return values


def schema_bundle_v2() -> dict:
    """The full fixed headless v2 bundle; advertisement filters, never edits it."""
    tools = [
        {"name": name, "inputSchema": {"type": "object", **adapter.json_schema()}}
        for name, adapter in TOOL_MODELS_V2.items()
    ]
    raw = json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
    return {
        "contract": CONTRACT_V2,
        "tools": tools,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
