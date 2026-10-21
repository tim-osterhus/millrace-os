"""OS-owned version-one contracts. No lower-layer DTO or durable job claim."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

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
    version: Literal["1.0.0"] = "1.0.0"
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


MODELS = [
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
