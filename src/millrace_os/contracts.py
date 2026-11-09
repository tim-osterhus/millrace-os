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
    RootModel,
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
    version: Literal["0.23.1"] = "0.23.1"
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


class SessionContext(Contract):
    """The existing session's lexical CSRF for a reloaded browser page.

    `expires_in` is the ceiling of the remaining lifetime; it never renews the
    session and is never zero.
    """

    contract: Literal["os.session-context.v1"] = "os.session-context.v1"
    csrf: Secret
    expires_in: Annotated[int, Field(ge=1, le=900)]


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


class RouterCallObservationV2(Contract):
    contract: Literal["millrace-os-router-call/v2"] = "millrace-os-router-call/v2"
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
    holder_joined: bool | None


class RouterObservationCapabilities(Contract):
    contract: Literal["millrace-os-router-observation-capabilities/v1"] = (
        "millrace-os-router-observation-capabilities/v1"
    )
    home: Identifier
    observation_contracts: list[
        Literal["millrace-os-router-call/v1", "millrace-os-router-call/v2"]
    ]
    holder_joined_supported: bool


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


# Bounded router views (UI1). Query models bind route parameters only and are
# not registered contracts, like `PageQuery`.
Cursor = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
]


class HomesQuery(Contract):
    limit: QueryInteger = Field(default=50, ge=1, le=50)
    cursor: Cursor | None = None


class ViewPageQuery(Contract):
    home: Identifier
    limit: QueryInteger = Field(default=50, ge=1, le=50)
    cursor: Cursor | None = None


class RunViewQuery(ViewPageQuery):
    section: Literal["summary", "invocations", "transitions", "controls"] = "summary"
    request_id: Identifier | None = None
    invocation_id: Identifier | None = None


class RouterHomeItem(Contract):
    name: Identifier


class RouterHomes(Contract):
    contract: Literal["millrace-os-router-homes/v1"] = "millrace-os-router-homes/v1"
    authority: Literal["os-local"] = "os-local"
    observed_at: Annotated[str, Field(max_length=40)]
    items: Annotated[list[RouterHomeItem], Field(max_length=50)]
    next_cursor: Cursor | None


class RouterView(Contract):
    contract: Literal["millrace-os-router-view/v1"] = "millrace-os-router-view/v1"
    home: Identifier
    data: dict[str, JsonValue]
    source_provider: Literal["core"] = "core"
    authority: Literal["core-state-home"] = "core-state-home"
    observed_at: Annotated[str, Field(max_length=40)]


class RouterViewValidateInput(Contract):
    home: Identifier
    instance_id: Identifier
    run_id: Identifier | None = None


# Generic workflow outputs (V1): OS envelopes over the public Plus helper's
# results. The helper owns record validation; reports stay workflow data.
CoreId = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
Sha256 = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
OutputId = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
]
NodeId = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
]
FormatId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
]
VersionText = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
]
Label = Annotated[str, Field(min_length=1, max_length=256)]
Timestamp = Annotated[str, Field(max_length=40)]
Evidence = Literal["workflow-produced-evidence"]


class OutputReadQuery(Contract):
    home: Identifier
    record_sha256: Sha256
    representation: Literal["report", "json"]


class WorkflowOutputPublishInput(Contract):
    home: Identifier
    record: dict[str, JsonValue]


class OutputReference(Contract):
    instance_id: CoreId
    run_id: CoreId
    output_id: OutputId
    record_sha256: Sha256


class OutputProducer(Contract):
    id: FormatId
    version: VersionText


class OutputInvocation(Contract):
    plane_id: NodeId
    node_id: NodeId
    invocation_id: CoreId
    producing_revision_id: Sha256


class OutputRepresentationInfo(Contract):
    format: FormatId
    media_type: Literal["application/json"]
    sha256: Sha256


class OutputRepresentations(Contract):
    # `json` would shadow a BaseModel attribute; the alias is the wire name
    # (bounded responses serialize by alias).
    report: OutputRepresentationInfo
    json_: OutputRepresentationInfo | None = Field(alias="json")


class WorkflowOutputItem(Contract):
    output_id: OutputId
    availability: Literal["available", "unavailable"]
    reason: Literal["output_integrity_error"] | None
    record_sha256: Sha256 | None
    producer: OutputProducer | None
    label: Label | None
    published_at: Timestamp | None
    invocation: OutputInvocation | None
    representations: OutputRepresentations | None

    @model_validator(mode="after")
    def identity_only_when_unavailable(self):
        known = (
            self.record_sha256,
            self.producer,
            self.label,
            self.published_at,
            self.invocation,
            self.representations,
        )
        available = self.availability == "available"
        if (self.reason is None) != available or any(
            (value is None) == available for value in known
        ):
            raise ValueError("an unavailable item carries only its output ID")
        return self


class WorkflowOutputs(Contract):
    contract: Literal["millrace-os-workflow-outputs/v1"] = (
        "millrace-os-workflow-outputs/v1"
    )
    home: Identifier
    instance_id: CoreId
    run_id: CoreId
    observed_at: Annotated[str, Field(max_length=40)]
    items: Annotated[list[WorkflowOutputItem], Field(max_length=50)]
    next_cursor: Cursor | None


class WorkflowOutputPublication(Contract):
    contract: Literal["millrace-os-workflow-output-publication/v1"] = (
        "millrace-os-workflow-output-publication/v1"
    )
    home: Identifier
    authority: Evidence = "workflow-produced-evidence"
    observed_at: Annotated[str, Field(max_length=40)]
    reference: OutputReference
    report_sha256: Sha256
    json_sha256: Sha256 | None
    status: Literal["published", "already_published"]


class OutputRepresentation(Contract):
    name: Literal["report", "json"]
    format: FormatId
    media_type: Literal["application/json"]
    sha256: Sha256
    data: JsonValue


class WorkflowOutput(Contract):
    contract: Literal["millrace-os-workflow-output/v1"] = (
        "millrace-os-workflow-output/v1"
    )
    home: Identifier
    authority: Evidence = "workflow-produced-evidence"
    observed_at: Annotated[str, Field(max_length=40)]
    reference: OutputReference
    producer: OutputProducer
    label: Label
    published_at: Timestamp
    invocation: OutputInvocation
    representation: OutputRepresentation


# Private demo context (UI2 owns the shape; the demo coordinator supplies it).
# An ordinary service has no reader and answers `demo_context_unavailable`.
class DemoOutputRef(Contract):
    reference: OutputReference
    report_sha256: Sha256
    json_sha256: Sha256 | None


class DemoContext(Contract):
    format: Literal["millrace-os-demo-context/v1"]
    home: Literal["demo"]
    instance_id: CoreId
    run_id: CoreId
    finish_available: bool
    accepted_output_refs: list[DemoOutputRef]
    output_error: Identifier | None


# Shared presentation preferences (P1): display data in the OS configuration
# directory, never execution authority. `presentation.py` owns the store rules
# (ordering, uniqueness, references, stock payloads); these strict shapes
# document the wire forms and refuse malformed bodies first.
FrontendId = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
]
BundledId = Literal["precision", "command", "studio", "default"]


class PresentationUserRef(Contract):
    kind: Literal["user"]
    id: CoreId


class PresentationBundledRef(Contract):
    kind: Literal["bundled"]
    id: BundledId


PresentationRef = Annotated[
    PresentationUserRef | PresentationBundledRef, Field(discriminator="kind")
]


class PresentationEntry(Contract):
    id: CoreId
    name: Label
    payload: dict[str, JsonValue]


class PresentationNamespace(Contract):
    frontend_id: FrontendId
    presentation_format: FormatId
    default_ref: PresentationRef | None
    configs: list[PresentationEntry]


class PresentationWriteStore(Contract):
    format: Literal["millrace-os-presentation-store/v2"]
    namespaces: list[PresentationNamespace]


class PresentationStore(PresentationWriteStore):
    version: CoreId


class PresentationBundledConfig(Contract):
    frontend_id: FrontendId
    presentation_format: FormatId
    id: BundledId
    name: Label
    payload: dict[str, JsonValue]
    payload_sha256: Sha256
    bundle_sha256: Sha256 | None


class PresentationState(Contract):
    contract: Literal["millrace-os-presentation-state/v1"] = (
        "millrace-os-presentation-state/v1"
    )
    status: Literal["absent", "ready", "incompatible", "invalid"]
    version: CoreId | None
    file_sha256: Sha256 | None
    store: PresentationStore | None
    bundled_configs: list[PresentationBundledConfig]
    observed_at: Timestamp


class PresentationCompare(Contract):
    format: Literal["millrace-os-presentation-write/v1"]
    mode: Literal["compare"]
    expected_version: CoreId | None
    store: PresentationWriteStore


class PresentationReplaceUnreadable(Contract):
    format: Literal["millrace-os-presentation-write/v1"]
    mode: Literal["replace_unreadable"]
    observed_file_sha256: Sha256
    store: PresentationWriteStore


class PresentationWrite(
    RootModel[
        Annotated[
            PresentationCompare | PresentationReplaceUnreadable,
            Field(discriminator="mode"),
        ]
    ]
):
    """`millrace-os-presentation-write/v1`: compare a token or replace by digest."""


class PresentationConfig(Contract):
    """The portable `millrace-os-presentation-config/v2` export/import document."""

    format: Literal["millrace-os-presentation-config/v2"]
    frontend_id: FrontendId
    presentation_format: FormatId
    name: Label
    payload: dict[str, JsonValue]


# Headless parity reads (M1): generic OS facts only, no workflow-specific schema.
OperationId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z_]*(\.[a-z][a-z_]*)+$"),
]
DraftPath = Annotated[str, Field(min_length=1, max_length=512)]


class ClientOperation(Contract):
    operation_id: OperationId
    method: Literal["GET", "POST", "PUT", "OPTIONS"]
    path: Annotated[str, Field(min_length=1, max_length=256)]
    request_contract: Identifier | None
    response_contract: Identifier | None
    available: bool
    reason: Identifier | None


class ClientCapabilities(Contract):
    contract: Literal["millrace-os-client-capabilities/v1"] = (
        "millrace-os-client-capabilities/v1"
    )
    api_version: Literal["v1"] = "v1"
    operations: list[ClientOperation]
    observed_at: Timestamp


class RouterLocalCall(Contract):
    contract: Literal["millrace-os-router-local-call/v1"] = (
        "millrace-os-router-local-call/v1"
    )
    home: Identifier
    request_id: CoreId
    request_digest: Sha256
    operation: Identifier
    instance_id: Identifier | None
    run_id: Identifier | None
    phase: Literal["admission_pending", "call_active", "call_returned", "call_failed"]
    holder_joined: bool
    error: Error | None
    result: JsonValue
    observed_at: Timestamp


class PublicationQuery(Contract):
    home: Identifier
    request_id: CoreId


class RouterPublication(Contract):
    contract: Literal["millrace-os-router-publication/v1"] = (
        "millrace-os-router-publication/v1"
    )
    home: Identifier
    instance_id: CoreId
    request_id: CoreId
    in_progress: dict[str, JsonValue] | None
    completed: dict[str, JsonValue] | None
    observed_at: Timestamp


class DraftManifestItem(Contract):
    path: Annotated[str, Field(min_length=1)]
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]


class DraftManifest(Contract):
    contract: Literal["millrace-os-draft-manifest/v1"] = "millrace-os-draft-manifest/v1"
    home: Identifier
    draft_id: Identifier
    sha256: Sha256
    items: Annotated[list[DraftManifestItem], Field(max_length=50)]
    next_cursor: Cursor | None
    observed_at: Timestamp


class DraftFileQuery(Contract):
    home: Identifier
    path: DraftPath


class DraftFileRead(Contract):
    contract: Literal["millrace-os-draft-file/v1"] = "millrace-os-draft-file/v1"
    home: Identifier
    draft_id: Identifier
    path: Annotated[str, Field(min_length=1)]
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]
    content_base64: Annotated[str, Field(max_length=1048576)]
    observed_at: Timestamp


MODELS = [
    RouterRead[JsonValue],
    RouterRead[RouterValidation],
    RouterCallObservation,
    RouterCallObservationV2,
    RouterObservationCapabilities,
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
    RouterHomes,
    RouterView,
    RouterViewValidateInput,
    WorkflowOutputPublishInput,
    WorkflowOutputPublication,
    WorkflowOutputs,
    WorkflowOutput,
    PresentationState,
    PresentationWrite,
    PresentationConfig,
    ClientCapabilities,
    RouterLocalCall,
    RouterPublication,
    DraftManifest,
    DraftFileRead,
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
    SessionContext,
    DemoContext,
    Rotation,
    Registry,
    Read[Version],
    Read[Diagnostics],
    Read[Identity],
    Read[Page[ProviderInfo]],
    Read[Page[ActionInfo]],
    Read[Registry],
]


def strict_json(raw: bytes) -> object:
    """Parse UTF-8 JSON, refusing duplicate keys and NaN/Infinity (ValueError)."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(name):
        raise ValueError("nonfinite number")

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except RecursionError as exc:
        raise ValueError("JSON nests too deeply") from exc


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
