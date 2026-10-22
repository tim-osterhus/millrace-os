"""OS-owned version-one contracts. No lower-layer DTO or durable job claim."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

Identifier = Annotated[
    str, Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
]
Text = Annotated[str, Field(max_length=256)]
Secret = Annotated[
    str, Field(min_length=43, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
]
T = TypeVar("T")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Health(Contract):
    status: Literal["ok", "unavailable"]


class Error(Contract):
    contract: Literal["os.error.v1"] = "os.error.v1"
    code: Identifier
    category: Literal["trust", "validation", "availability", "request"]
    summary: Text
    retryable: bool = False
    actionable: bool = False
    provider: Literal["os"] = "os"
    correlation: Identifier | None = None
    causation: Identifier | None = None
    detail: Text = ""
    diagnostic_reference: Identifier | None = None
    partial_result: bool = False
    suggested_actions: Annotated[list[Identifier], Field(max_length=8)] = []


class Read(Contract, Generic[T]):  # noqa: UP046 - Pydantic generic schema names
    contract: Identifier
    data: T
    source_provider: Literal["os"] = "os"
    authority: Literal["os-local"] = "os-local"
    observed_at: Annotated[str, Field(max_length=40)]
    source_cursor: None = None
    freshness: Literal["current"] = "current"
    redacted: Literal[True] = True
    compatibility: Literal["foundation-only"] = "foundation-only"
    warnings: Annotated[list[Text], Field(max_length=8)] = []
    partial_reasons: Annotated[list[Text], Field(max_length=8)] = []


class Version(Contract):
    package: Literal["millrace-os"] = "millrace-os"
    version: Literal["0.23.0"] = "0.23.0"
    api: Literal["v1"] = "v1"


class Diagnostics(Contract):
    status: Literal["ready", "recovery"]
    storage_error: Identifier | None = None
    actions_available: Literal[False] = False
    providers_available: Literal[False] = False
    recovery_operations: Literal["diagnostics-only"] = "diagnostics-only"


class Identity(Contract):
    identity: Literal["local-operator"] = "local-operator"
    authentication: Literal["bearer", "session"]
    recovery_only: bool


def _parse_query_integer(value: object) -> int:
    """Accept decimal integer query text without numeric coercion."""
    if isinstance(value, bool):
        raise ValueError("query integer must be decimal text")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise ValueError("query integer must be decimal text")


QueryInteger = Annotated[int, BeforeValidator(_parse_query_integer)]


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: QueryInteger = Field(default=20, ge=1, le=100)
    cursor: (
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
        | None
    ) = None


class ProviderInfo(Contract):
    provider_id: Identifier
    authority: Literal["workflow", "terminal"]
    available: Literal[False] = False
    reason: Literal["not_implemented"] = "not_implemented"


class ActionInfo(Contract):
    action_id: Identifier
    available: bool


class Page(Contract, Generic[T]):  # noqa: UP046 - Pydantic generic schema names
    items: Annotated[list[T], Field(max_length=100)]
    next_cursor: None = None


class ActionInput(Contract):
    target_id: Identifier


class ActionOutput(Contract):
    outcome: Literal["refused", "pending", "unknown"]
    operation_id: Identifier | None = None
    provider_executed: Literal[False] = False


class Receipt(Contract):
    receipt_id: Identifier
    action_id: Identifier
    outcome: Literal["refused", "pending", "unknown"]
    # Reserved distinct type; this packet never emits or persists a receipt.


class Event(Contract):
    contract: Literal["os.event.v1"] = "os.event.v1"
    kind: Literal["availability"] = "availability"
    scope: Literal["foundation-only"] = "foundation-only"
    durable: Literal[False] = False
    replay: Literal["unavailable"] = "unavailable"


class StorageRecord(Contract):
    key: Identifier
    schema_version: Literal[1] = 1


class BootstrapInput(Contract):
    recovery: bool = False


class BootstrapNonce(Contract):
    nonce: Secret
    expires_in: Literal[60] = 60


class ExchangeInput(Contract):
    nonce: Secret
    recovery: bool = False


class SessionOutput(Contract):
    csrf: Secret
    expires_in: Literal[900] = 900


class Rotation(Contract):
    status: Literal["rotated"] = "rotated"


class SchemaEntry(Contract):
    contract_id: Identifier
    version: Literal[1] = 1
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Registry(Contract):
    registry: Literal["os.registry.v1"] = "os.registry.v1"
    schemas: Annotated[list[SchemaEntry], Field(max_length=64)]


class RouterRead(Contract, Generic[T]):  # noqa: UP046
    contract: Literal["millrace-os-router-read/v1"] = "millrace-os-router-read/v1"
    home: Identifier
    data: T
    source_provider: Literal["core"] = "core"
    authority: Literal["core-state-home"] = "core-state-home"
    observed_at: Annotated[str, Field(max_length=40)]


class RouterCallObservation(Contract):
    contract: Literal["millrace-os-router-call/v1"] = "millrace-os-router-call/v1"
    home: Identifier
    request_id: Identifier
    request_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    instance_id: Identifier | None
    run_id: Identifier | None
    source: Literal["os_owned_call", "core_receipt"]
    phase: Literal[
        "admission_pending",
        "call_active",
        "call_returned",
        "call_failed",
        "receipt_observed",
    ]
    core_receipt: dict[str, JsonValue] | None
    observed_at: Annotated[str, Field(max_length=40)]


class RouterRequestInput(Contract):
    home: Identifier
    request: dict[str, JsonValue]


class RouterValidationContext(Contract):
    kind: Literal["instance", "run"]
    instance_id: Identifier
    run_id: Identifier | None
    settings_version: Annotated[int, Field(ge=1)]
    run_state_version: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def coherent_target(self):
        if self.kind == "run" and (
            self.run_id is None or self.run_state_version is None
        ):
            raise ValueError("run validation requires observed run state")
        if self.kind == "instance" and (
            self.run_id is not None or self.run_state_version is not None
        ):
            raise ValueError("instance validation has no run state")
        return self


class RouterValidation(Contract):
    valid: bool
    revision_id: str | None
    draft_revision: dict[str, JsonValue] | None = None
    diagnostics: list[str]
    diagnostic_details: list[str] = Field(default_factory=list)
    validation_context: RouterValidationContext
    resolved_nodes: dict[str, dict[str, dict[str, JsonValue]]]
    warnings: list[str]


class RouterValidateInput(Contract):
    home: Identifier
    instance_id: Identifier
    run_id: Identifier | None = None
    draft_id: Identifier | None = None


class DraftFile(Contract):
    path: Annotated[str, Field(min_length=1, max_length=512)]
    content_base64: Annotated[str, Field(max_length=1048576)]


class DraftCreateInput(Contract):
    home: Identifier
    files: Annotated[list[DraftFile], Field(max_length=256)] = Field(
        default_factory=list
    )


class DraftFilesInput(Contract):
    home: Identifier
    files: Annotated[list[DraftFile], Field(max_length=256)]


class DraftFileInput(DraftFile):
    home: Identifier


class DraftValidateInput(Contract):
    home: Identifier
    instance_id: Identifier
    run_id: Identifier | None = None


class DraftApplyInput(Contract):
    home: Identifier
    instance_id: Identifier
    run_id: Identifier
    base_state_version: Annotated[int, Field(ge=1)]
    request_id: Annotated[
        str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    ]


class PublicationRepairInput(Contract):
    home: Identifier
    instance_id: Identifier


class DraftRead(Contract):
    contract: Literal["millrace-os-draft/v1"] = "millrace-os-draft/v1"
    home: Identifier
    draft_id: Identifier
    files: list[DraftFile]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CatalogInitInput(Contract):
    home: Identifier
    selector: Annotated[str, Field(min_length=1, max_length=256)]
    destination: Annotated[str, Field(min_length=1, max_length=4096)]


class CatalogCopyInput(Contract):
    home: Identifier
    source: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    destination: Annotated[str, Field(min_length=1, max_length=4096)]
    draft_id: Identifier | None = None


class RouterLocalRead(Contract):
    contract: Literal["millrace-os-router-local/v1"] = "millrace-os-router-local/v1"
    home: Identifier
    authority: Literal["os-client"] = "os-client"
    observed_at: Annotated[str, Field(max_length=40)]
    data: JsonValue


MODELS = [
    RouterRead[JsonValue],
    RouterRead[RouterValidation],
    RouterCallObservation,
    RouterRequestInput,
    RouterValidationContext,
    RouterValidation,
    RouterValidateInput,
    DraftCreateInput,
    DraftFilesInput,
    DraftFileInput,
    DraftValidateInput,
    DraftApplyInput,
    PublicationRepairInput,
    DraftRead,
    CatalogInitInput,
    CatalogCopyInput,
    RouterLocalRead,
    Health,
    Error,
    Version,
    Diagnostics,
    Identity,
    ProviderInfo,
    ActionInfo,
    ActionInput,
    ActionOutput,
    Receipt,
    Event,
    StorageRecord,
    BootstrapInput,
    BootstrapNonce,
    ExchangeInput,
    SessionOutput,
    Rotation,
    Registry,
    Read[Version],
    Read[Diagnostics],
    Read[Identity],
    Read[Page[ProviderInfo]],
    Read[Page[ActionInfo]],
    Read[Registry],
]


def canonical(value) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode()


def schema_artifacts():
    schemas = {}
    entries = []
    for model in MODELS:
        key = model.__name__.replace("[", "-").replace("]", "")
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = "urn:millrace-os:" + key + ":1"
        schemas[key] = schema
        entries.append(
            SchemaEntry(
                contract_id=key, sha256=hashlib.sha256(canonical(schema)).hexdigest()
            )
        )
    return schemas, Registry(
        schemas=sorted(entries, key=lambda entry: entry.contract_id)
    )
