"""MCP v2 tool contracts: unions, exact fields, strictness and v1 unchanged;
then the fixed route mapping, advertisement, results and errors in process.

Transport framing over the pinned SDK is in `test_v2_transport.py`. The
packaged v2 bundle file is produced by the official export.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from importlib.resources import files

import httpx
import pytest
from pydantic import ValidationError

from millrace_os import mcp_contracts as mc
from millrace_os import presentation as p
from millrace_os.api import CLIENT_OPERATIONS
from millrace_os.mcp_bridge import Bridge
from millrace_os.mcp_client import V2_ROUTES, BackendClient, advertised_v2

HOMES = ["main"]
A, B, C, D = "a" * 32, "b" * 32, "c" * 32, "d" * 64
OBSERVATION = "millrace-os-router-call/v2"
STOCK = {
    "frontend_id": "stock",
    "presentation_format": "millrace-stock-presentation/v1",
}
BOARD = {
    "frontend_id": "example.board",
    "presentation_format": "example.board-layout/v1",
}
PRECISION = {
    "preset": "precision",
    "theme": "system",
    "density": "comfortable",
    "arrangement": "detail-split",
    "panels": {
        "planes": True,
        "current-work": True,
        "graph": True,
        "inspector": True,
        "history": True,
        "revisions": True,
    },
    "split_ratios": {"detail-split": 0.5, "status-stack": 0.5, "graph-split": 0.5},
}
STORE = {"format": "millrace-os-presentation-store/v2", "namespaces": []}
WRITE = {
    "format": "millrace-os-presentation-write/v1",
    "mode": "compare",
    "expected_version": None,
    "store": STORE,
}
CONFIG = {
    "format": "millrace-os-presentation-config/v2",
    **BOARD,
    "name": "Board",
    "payload": {"columns": 3},
}
FILE = {"path": "workflow.json", "content_base64": "e30="}
USER = {"kind": "user", "id": A}
COMMAND = {"kind": "bundled", "id": "command"}
DEFAULT = {"kind": "bundled", "id": "default"}
LOCAL = {"kind": "local", "path": "/opt/board", "bundle_sha256": D}

# (tool, alternative) -> (required fields, optional fields, valid sample).
# The discriminator is a required field; the sample uses every required field.
FIELDS = {
    ("discover", "bridge"): ({"kind"}, set(), {}),
    ("discover", "health"): ({"kind"}, set(), {}),
    ("discover", "identity"): ({"kind"}, set(), {}),
    ("discover", "version"): ({"kind"}, set(), {}),
    ("discover", "contracts"): ({"kind"}, set(), {}),
    ("discover", "schema"): (
        {"kind", "contract_id", "digest"},
        set(),
        {"contract_id": "Error", "digest": D},
    ),
    ("discover", "diagnostics"): ({"kind"}, set(), {}),
    ("discover", "providers"): ({"kind"}, set(), {}),
    ("discover", "actions"): ({"kind"}, set(), {}),
    ("discover", "availability"): ({"kind"}, set(), {}),
    ("discover", "capabilities"): ({"kind"}, set(), {}),
    ("discover", "observation"): ({"kind", "home"}, set(), {"home": "main"}),
    ("inspect", "homes"): ({"target"}, {"limit", "cursor"}, {}),
    ("inspect", "instances"): (
        {"target", "home"},
        {"limit", "cursor"},
        {"home": "main"},
    ),
    ("inspect", "instance"): (
        {"target", "home", "instance_id"},
        {"limit", "cursor"},
        {"home": "main", "instance_id": A},
    ),
    ("inspect", "run"): (
        {"target", "home", "instance_id", "run_id", "section"},
        {"limit", "cursor", "request_id", "invocation_id"},
        {"home": "main", "instance_id": A, "run_id": B, "section": "controls"},
    ),
    ("inspect", "revision"): (
        {"target", "home", "instance_id", "run_id", "revision_id"},
        set(),
        {"home": "main", "instance_id": A, "run_id": B, "revision_id": D},
    ),
    ("receipt", "lifecycle"): (
        {"mode", "home", "request_id", "observation_contract"},
        {"instance_id", "run_id"},
        {"home": "main", "request_id": C, "observation_contract": OBSERVATION},
    ),
    ("receipt", "bounded"): (
        {"mode", "home", "request_id", "instance_id", "observation_contract"},
        {"run_id"},
        {
            "home": "main",
            "request_id": C,
            "instance_id": A,
            "observation_contract": OBSERVATION,
        },
    ),
    ("validate", "configured"): (
        {"kind", "home", "instance_id"},
        {"run_id"},
        {"home": "main", "instance_id": A},
    ),
    ("validate", "draft"): (
        {"kind", "home", "draft_id", "instance_id"},
        {"run_id"},
        {"home": "main", "draft_id": C, "instance_id": A},
    ),
    ("validate", "summary"): (
        {"kind", "home", "instance_id", "run_id"},
        set(),
        {"home": "main", "instance_id": A, "run_id": None},
    ),
    ("catalog", "list"): ({"operation", "home"}, set(), {"home": "main"}),
    ("catalog", "init"): (
        {"operation", "home", "selector", "destination"},
        set(),
        {"home": "main", "selector": "bundled:lad", "destination": "/tmp/flow"},
    ),
    ("catalog", "copy"): (
        {"operation", "home", "destination"},
        {"source", "draft_id"},
        {"home": "main", "destination": "/tmp/flow", "source": "/tmp/source"},
    ),
    ("draft", "create"): (
        {"operation", "home", "files"},
        set(),
        {"home": "main", "files": [FILE]},
    ),
    ("draft", "read"): (
        {"operation", "home", "draft_id"},
        set(),
        {"home": "main", "draft_id": C},
    ),
    ("draft", "replace_files"): (
        {"operation", "home", "draft_id", "files"},
        set(),
        {"home": "main", "draft_id": C, "files": [FILE]},
    ),
    ("draft", "put_file"): (
        {"operation", "home", "draft_id", "path", "content_base64"},
        set(),
        {"home": "main", "draft_id": C, **FILE},
    ),
    ("draft", "manifest"): (
        {"operation", "home", "draft_id"},
        {"limit", "cursor"},
        {"home": "main", "draft_id": C},
    ),
    ("draft", "read_file"): (
        {"operation", "home", "draft_id", "path"},
        set(),
        {"home": "main", "draft_id": C, "path": "workflow.json"},
    ),
    ("draft", "apply"): (
        {
            "operation",
            "home",
            "draft_id",
            "instance_id",
            "run_id",
            "base_state_version",
            "request_id",
        },
        set(),
        {
            "home": "main",
            "draft_id": C,
            "instance_id": A,
            "run_id": B,
            "base_state_version": 3,
            "request_id": C,
        },
    ),
    ("draft", "repair_publication"): (
        {"operation", "home", "instance_id"},
        set(),
        {"home": "main", "instance_id": A},
    ),
    ("draft", "publication_status"): (
        {"operation", "home", "instance_id", "request_id"},
        set(),
        {"home": "main", "instance_id": A, "request_id": C},
    ),
    ("outputs", "publish"): (
        {"operation", "home", "record"},
        set(),
        {"home": "main", "record": {"format": "x", "nested": [1, None]}},
    ),
    ("outputs", "list"): (
        {"operation", "home", "instance_id", "run_id"},
        {"limit", "cursor"},
        {"home": "main", "instance_id": A, "run_id": B},
    ),
    ("outputs", "read"): (
        {
            "operation",
            "home",
            "instance_id",
            "run_id",
            "output_id",
            "record_sha256",
            "representation",
        },
        set(),
        {
            "home": "main",
            "instance_id": A,
            "run_id": B,
            "output_id": "report-a",
            "record_sha256": D,
            "representation": "json",
        },
    ),
    ("presentation", "get"): ({"operation"}, set(), {}),
    ("presentation", "put"): ({"operation", "write"}, set(), {"write": WRITE}),
    ("presentation", "replace_unreadable"): (
        {"operation", "observed_file_sha256", "store"},
        set(),
        {"observed_file_sha256": D, "store": STORE},
    ),
    ("presentation", "list"): ({"operation"}, {"namespace"}, {}),
    ("presentation", "read"): (
        {"operation", "namespace", "ref"},
        set(),
        {"namespace": BOARD, "ref": USER},
    ),
    ("presentation", "read_default"): (
        {"operation", "namespace"},
        set(),
        {"namespace": STOCK},
    ),
    ("presentation", "export"): (
        {"operation", "namespace", "ref"},
        set(),
        {"namespace": STOCK, "ref": {"kind": "bundled", "id": "studio"}},
    ),
    ("presentation", "create"): (
        {"operation", "namespace", "name", "payload"},
        {"expected_version"},
        {"namespace": STOCK, "name": "Mine", "payload": PRECISION},
    ),
    ("presentation", "update"): (
        {"operation", "namespace", "config_id", "payload"},
        {"expected_version"},
        {"namespace": BOARD, "config_id": A, "payload": {"columns": 4}},
    ),
    ("presentation", "rename"): (
        {"operation", "namespace", "config_id", "name"},
        {"expected_version"},
        {"namespace": BOARD, "config_id": A, "name": "Board"},
    ),
    ("presentation", "duplicate"): (
        {"operation", "namespace", "source", "name"},
        {"expected_version"},
        {"namespace": BOARD, "source": DEFAULT, "name": "Copy"},
    ),
    ("presentation", "delete"): (
        {"operation", "namespace", "config_id"},
        {"expected_version"},
        {"namespace": BOARD, "config_id": A},
    ),
    ("presentation", "set_default"): (
        {"operation", "namespace", "default"},
        {"expected_version"},
        {"namespace": BOARD, "default": None},
    ),
    ("presentation", "reset"): (
        {"operation", "namespace", "config_id"},
        {"expected_version"},
        {"namespace": STOCK, "config_id": A},
    ),
    ("presentation", "import"): (
        {"operation", "config"},
        {"name", "expected_version"},
        {"config": CONFIG},
    ),
    ("frontend", "inspect"): ({"operation"}, set(), {}),
    ("frontend", "validate"): ({"operation", "path"}, set(), {"path": "/opt/board"}),
    ("frontend", "select"): (
        {"operation", "expected_version", "desired"},
        set(),
        {"expected_version": None, "desired": LOCAL},
    ),
    ("frontend", "reset"): (
        {"operation", "expected_version"},
        set(),
        {"expected_version": A},
    ),
}
DISCRIMINATORS = {
    "discover": "kind",
    "inspect": "target",
    "receipt": "mode",
    "validate": "kind",
    "catalog": "operation",
    "draft": "operation",
    "outputs": "operation",
    "presentation": "operation",
    "frontend": "operation",
}
SINGLE = {
    "request": (
        {"home", "request", "observation_contract"},
        {
            "home": "main",
            "request": {"operation": "start_run", "request_id": C, "keep": {"n": 1}},
            "observation_contract": "millrace-os-router-call/v1",
        },
    ),
    "local_call": ({"home", "request_id"}, {"home": "main", "request_id": C}),
}


def arguments(tool: str, alternative: str) -> dict:
    sample = copy.deepcopy(FIELDS[(tool, alternative)][2])
    return {DISCRIMINATORS[tool]: alternative, **sample}


def admit(tool: str, values: object) -> dict:
    return mc.admit_arguments_v2("millrace_v2_" + tool, values, HOMES)


def refuses(tool: str, values: object) -> None:
    with pytest.raises((ValidationError, ValueError)):
        admit(tool, values)


def test_v1_surface_is_unchanged() -> None:
    assert list(mc.TOOL_MODELS) == [
        "millrace_v1_discover",
        "millrace_v1_instances",
        "millrace_v1_instance",
        "millrace_v1_run",
        "millrace_v1_request",
        "millrace_v1_receipt",
        "millrace_v1_validate",
    ]
    bundle = mc.schema_bundle()
    assert bundle["contract"] == "millrace-os-mcp-tools/v1"
    assert bundle["sha256"] == (
        "ee849426232e7257f1c8c20cd017e441a1348f1c413d7f1f15c661727fd640cd"
    )
    packaged = files("millrace_os").joinpath("schemas/mcp-tools-v1.json").read_text()
    assert json.loads(packaged) == bundle
    assert (mc.MAX_INPUT, mc.MAX_OUTPUT) == (65536, 1048576)
    assert len(mc.OPERATIONS) == 13 and "release_project_hold" not in mc.OPERATIONS


def test_v2_framing_constants_follow_the_n11_amended_equations() -> None:
    # N11: the pinned SDK may spell an embedded B-byte body in up to 2B bytes.
    assert mc.CONTRACT_V2 == "millrace-os-mcp-tools/v2"
    assert mc.V2_MAX_INPUT == 2 * 1_048_576 + 65_536 == 2_162_688
    assert mc.V2_MAX_API_RESPONSE == 1_049_600
    assert mc.V2_MAX_OUTPUT == 2_162_688 + 1_024 + 3 * 1_049_600 + 1_024
    assert mc.V2_MAX_OUTPUT == 5_313_536
    # The effective CallToolResult ceiling stays 3R + 1,024.
    assert mc.V2_MAX_OUTPUT - mc.V2_MAX_INPUT - 1_024 == 3 * 1_049_600 + 1_024
    assert mc.V2 == ("v2", 2_162_688, 5_313_536, 1_049_600)
    # v1 keeps 64 KiB in, 1 MiB out and its 982,016-byte result ceiling.
    assert mc.V1 == ("v1", 65_536, 1_048_576, 1_048_576)
    assert mc.MAX_OUTPUT - mc.MAX_INPUT - 1_024 == 982_016


def test_bundle_has_exactly_the_eleven_tools() -> None:
    bundle = mc.schema_bundle_v2()
    assert bundle == mc.schema_bundle_v2()  # deterministic
    names = [tool["name"] for tool in bundle["tools"]]
    assert names == [
        "millrace_v2_" + name
        for name in (
            "discover",
            "inspect",
            "request",
            "receipt",
            "validate",
            "catalog",
            "draft",
            "local_call",
            "outputs",
            "presentation",
            "frontend",
        )
    ]
    assert not any("demo" in name for name in names)
    raw = json.dumps(bundle["tools"], sort_keys=True, separators=(",", ":")).encode()
    assert bundle["contract"] == "millrace-os-mcp-tools/v2"
    assert bundle["sha256"] == hashlib.sha256(raw).hexdigest()
    assert bundle["sha256"] != mc.schema_bundle()["sha256"]
    for tool in bundle["tools"]:
        assert tool["inputSchema"]["type"] == "object", tool["name"]


def schema(tool: str) -> dict:
    (entry,) = [
        item
        for item in mc.schema_bundle_v2()["tools"]
        if item["name"] == "millrace_v2_" + tool
    ]
    return entry["inputSchema"]


def test_every_alternative_declares_exactly_its_g0_fields() -> None:
    for tool, field in DISCRIMINATORS.items():
        document = schema(tool)
        assert document["discriminator"]["propertyName"] == field
        mapping = document["discriminator"]["mapping"]
        expected = {alt for (name, alt) in FIELDS if name == tool}
        assert set(mapping) == expected, tool
        for alternative, reference in mapping.items():
            model = document["$defs"][reference.rsplit("/", 1)[1]]
            required, optional, _ = FIELDS[(tool, alternative)]
            assert set(model["required"]) == required, (tool, alternative)
            assert set(model["properties"]) == required | optional, (
                tool,
                alternative,
            )
            assert model["additionalProperties"] is False
            # Omission is meaningful: no property advertises a default value.
            properties = model["properties"].items()
            hints = [key for key, prop in properties if "default" in prop]
            assert hints == [], (tool, alternative)
    assert len([key for key in FIELDS if key[0] == "presentation"]) == 15
    for tool, (required, _) in SINGLE.items():
        document = schema(tool)
        assert set(document["required"]) == set(document["properties"]) == required


def test_every_alternative_accepts_its_sample_and_refuses_shape_errors() -> None:
    for tool, alternative in FIELDS:
        values = arguments(tool, alternative)
        required = FIELDS[(tool, alternative)][0]
        assert admit(tool, copy.deepcopy(values)) == values, (tool, alternative)
        refuses(tool, values | {"extra": 1})
        refuses(tool, json.dumps(values))
        for name in required:
            missing = {key: value for key, value in values.items() if key != name}
            refuses(tool, missing)
        if "home" in values:
            refuses(tool, values | {"home": True})
            refuses(tool, values | {"home": "elsewhere"})  # not an allowed home
    for tool, (_, values) in SINGLE.items():
        assert admit(tool, copy.deepcopy(values)) == values
        refuses(tool, values | {"extra": 1})
        refuses(tool, values | {"home": "elsewhere"})
    for tool in DISCRIMINATORS:
        refuses(tool, {DISCRIMINATORS[tool]: "undeclared"})
        refuses(tool, {})
    with pytest.raises(ValueError):
        mc.admit_arguments_v2("millrace_v2_demo", {}, HOMES)


def test_optional_fields_round_trip_without_coercion() -> None:
    values = arguments("inspect", "run") | {
        "limit": 50,
        "cursor": "c_9",
        "request_id": C,
        "invocation_id": A,
    }
    assert admit("inspect", values) == values
    for bad in (
        {"limit": "5"},
        {"limit": True},
        {"limit": 0},
        {"limit": 51},
        {"limit": None},
        {"cursor": ""},
        {"cursor": "a!b"},
        {"request_id": "A" * 32},
        {"section": "history"},
    ):
        refuses("inspect", values | bad)
    refuses("inspect", arguments("inspect", "revision") | {"revision_id": "E" * 64})
    record = arguments("outputs", "publish")
    for nonfinite in (float("nan"), float("inf")):
        refuses("outputs", record | {"record": {"value": nonfinite}})
    refuses("outputs", arguments("outputs", "read") | {"representation": "html"})
    refuses("outputs", arguments("outputs", "read") | {"output_id": "Report"})
    refuses("draft", arguments("draft", "apply") | {"base_state_version": 0})
    refuses("draft", arguments("draft", "apply") | {"base_state_version": True})
    refuses("draft", arguments("draft", "create") | {"files": [FILE] * 257})


def test_correlated_fields_refuse_before_any_request() -> None:
    lifecycle = arguments("receipt", "lifecycle")
    assert admit("receipt", lifecycle | {"instance_id": A, "run_id": B})
    refuses("receipt", lifecycle | {"run_id": B})  # a run needs its instance
    refuses("receipt", lifecycle | {"instance_id": None})
    summary = arguments("validate", "summary")
    assert admit("validate", summary | {"run_id": B})["run_id"] == B
    refuses("validate", {k: v for k, v in summary.items() if k != "run_id"})
    configured = arguments("validate", "configured")
    refuses("validate", configured | {"run_id": None})  # omittable, not null
    copy_values = arguments("catalog", "copy")
    refuses("catalog", copy_values | {"draft_id": C})  # both sources
    neither = {k: v for k, v in copy_values.items() if k != "source"}
    refuses("catalog", neither)
    assert admit("catalog", neither | {"draft_id": C})["draft_id"] == C


def test_request_tool_admits_exactly_the_sixteen_operations_unchanged() -> None:
    assert len(mc.OPERATIONS_V2) == 16
    assert mc.OPERATIONS_V2 == mc.OPERATIONS | {
        "recover_run",
        "reconcile_invocation",
        "release_project_hold",
    }
    base = SINGLE["request"][1]
    for operation in sorted(mc.OPERATIONS_V2):
        body = {"operation": operation, "request_id": C, "untouched": {"n": [1, 2]}}
        values = base | {"request": copy.deepcopy(body)}
        assert admit("request", values)["request"] == body
    for operation in ("rotate", "draft_apply", "frontend_select", None):
        refuses(
            "request", base | {"request": {"operation": operation, "request_id": C}}
        )
    for request_id in ("C" * 32, "c" * 31, 7, None):
        request = {"operation": "start_run", "request_id": request_id}
        refuses("request", base | {"request": request})
    refuses("request", base | {"observation_contract": "millrace-os-router-call/v3"})
    refuses("local_call", SINGLE["local_call"][1] | {"request_id": "x"})


def test_presentation_namespace_reference_and_payload_rules() -> None:
    # Positional-only: an alternative's own `name` field passes in `changes`.
    def alt(alternative: str, /, **changes: object) -> dict:
        return arguments("presentation", alternative) | changes

    # Bundled IDs fit their namespace; stock never has a null default.
    for values in (
        alt("read", ref={"kind": "bundled", "id": "precision"}),
        alt("export", namespace=BOARD, ref={"kind": "bundled", "id": "studio"}),
        alt("duplicate", namespace=STOCK, source=DEFAULT, name="x"),
        alt("set_default", namespace=STOCK, default=None),
        alt("set_default", namespace=STOCK, default=DEFAULT),
        alt("set_default", namespace=BOARD, default=COMMAND),
        alt("read", ref={"kind": "user", "id": "A" * 32}),
        alt("read", ref={"kind": "remote", "id": A}),
    ):
        refuses("presentation", values)
    assert admit("presentation", alt("set_default"))["default"] is None
    assert admit(
        "presentation", alt("set_default", namespace=STOCK, default=USER)
    )["default"] == USER
    # Namespaces are explicit, strict pairs; stock owns only its format.
    for namespace in (
        {"frontend_id": "stock", "presentation_format": "example.other/v1"},
        {"frontend_id": "Example", "presentation_format": "example.other/v1"},
        {**BOARD, "extra": True},
        {"frontend_id": "example.board"},
        None,
    ):
        refuses("presentation", alt("read_default", namespace=namespace))
    refuses("presentation", alt("list", namespace=None))
    assert admit("presentation", alt("list", namespace=BOARD))["namespace"] == BOARD
    # Stock payloads validate strictly; custom payloads are finite JSON objects.
    ratios = PRECISION["split_ratios"]
    for payload in (
        PRECISION | {"theme": "neon"},
        PRECISION | {"status": True},
        PRECISION | {"split_ratios": ratios | {"graph-split": 1}},
        PRECISION | {"split_ratios": ratios | {"graph-split": True}},
        PRECISION | {"split_ratios": ratios | {"graph-split": 10**400}},
        PRECISION | {"panels": PRECISION["panels"] | {"graph": 1}},
        {key: value for key, value in PRECISION.items() if key != "panels"},
        {"columns": 3},
    ):
        refuses("presentation", alt("create", payload=payload))
    custom = alt("update", payload={"nested": [{"any": None}], "n": 1.5})
    assert admit("presentation", custom) == custom
    refuses("presentation", alt("update", payload={"n": float("nan")}))
    refuses("presentation", alt("update", payload=[1]))
    # Names are one-line labels; duplicates are a helper-level concern.
    for name in ("", "two\nlines", "tab\there", "x" * 257, True):
        refuses("presentation", alt("rename", name=name))
    # Portable documents follow the same namespace and payload rules.
    imported = alt("import", name="Renamed")
    assert admit("presentation", imported) == imported
    for config in (
        CONFIG | {"format": "millrace-os-presentation-config/v1"},
        CONFIG | {"extra": 1},
        {**CONFIG, **STOCK, "payload": {"columns": 3}},
        {**CONFIG, "frontend_id": "stock"},
    ):
        refuses("presentation", alt("import", config=config))
    # Write bodies: compare needs expected_version present (null allowed).
    compare_only = {k: v for k, v in WRITE.items() if k != "expected_version"}
    refuses("presentation", alt("put", write=compare_only))
    replace = {
        "format": WRITE["format"],
        "mode": "replace_unreadable",
        "observed_file_sha256": D,
        "store": STORE,
    }
    assert admit("presentation", alt("put", write=replace))["write"] == replace
    refuses("presentation", alt("put", write=replace | {"observed_file_sha256": "x"}))
    refuses("presentation", alt("put", write=WRITE | {"format": "x/v2"}))
    # No alternative carries a home: the bridge never selects an OS home here.
    refuses("presentation", alt("get", home="main"))


VERSIONED = (
    "create",
    "update",
    "rename",
    "duplicate",
    "delete",
    "set_default",
    "reset",
    "import",
)


def test_expected_version_omitted_differs_from_explicit_null() -> None:
    for name in VERSIONED:
        values = arguments("presentation", name)
        assert "expected_version" not in admit("presentation", values), name
        explicit = admit("presentation", values | {"expected_version": None})
        assert explicit["expected_version"] is None, name
        token = admit("presentation", values | {"expected_version": B})
        assert token["expected_version"] == B, name
        refuses("presentation", values | {"expected_version": "short"})


# --- Route mapping, advertisement, results and errors (in process). ---

ALL = {operation[0]: True for operation in CLIENT_OPERATIONS}
R = mc.V2_MAX_API_RESPONSE
ROUTER = "/api/v1/router"
# Concrete routes for the FIELDS/SINGLE samples, spelled out from OS-G0 7.3:
# (method, path, argument names carried in the path).
EXPECTED_ROUTES = {
    ("discover", "health"): ("GET", "/api/v1/health", set()),
    ("discover", "identity"): ("GET", "/api/v1/identity", set()),
    ("discover", "version"): ("GET", "/api/v1/version", set()),
    ("discover", "contracts"): ("GET", "/api/v1/contracts", set()),
    ("discover", "schema"): (
        "GET",
        f"/api/v1/contracts/Error/1/{D}",
        {"contract_id", "digest"},
    ),
    ("discover", "diagnostics"): ("GET", "/api/v1/diagnostics", set()),
    ("discover", "providers"): ("GET", "/api/v1/providers", set()),
    ("discover", "actions"): ("GET", "/api/v1/actions", set()),
    ("discover", "availability"): ("GET", "/api/v1/events", set()),
    ("discover", "capabilities"): ("GET", "/api/v1/client/capabilities", set()),
    ("discover", "observation"): ("OPTIONS", ROUTER + "/requests", set()),
    ("inspect", "homes"): ("GET", ROUTER + "/homes", set()),
    ("inspect", "instances"): ("GET", ROUTER + "/views/instances", set()),
    ("inspect", "instance"): (
        "GET",
        f"{ROUTER}/views/instances/{A}",
        {"instance_id"},
    ),
    ("inspect", "run"): (
        "GET",
        f"{ROUTER}/views/instances/{A}/runs/{B}",
        {"instance_id", "run_id"},
    ),
    ("inspect", "revision"): (
        "GET",
        f"{ROUTER}/views/instances/{A}/runs/{B}/revisions/{D}",
        {"instance_id", "run_id", "revision_id"},
    ),
    ("receipt", "lifecycle"): ("GET", f"{ROUTER}/requests/{C}", {"request_id"}),
    ("receipt", "bounded"): ("GET", f"{ROUTER}/views/requests/{C}", {"request_id"}),
    ("validate", "configured"): ("POST", ROUTER + "/validate", set()),
    ("validate", "draft"): ("POST", f"{ROUTER}/drafts/{C}/validate", {"draft_id"}),
    ("validate", "summary"): ("POST", ROUTER + "/views/validate", set()),
    ("catalog", "list"): ("GET", ROUTER + "/catalog/templates", set()),
    ("catalog", "init"): ("POST", ROUTER + "/catalog/init", set()),
    ("catalog", "copy"): ("POST", ROUTER + "/catalog/copy", set()),
    ("draft", "create"): ("POST", ROUTER + "/drafts", set()),
    ("draft", "read"): ("GET", f"{ROUTER}/drafts/{C}", {"draft_id"}),
    ("draft", "replace_files"): ("PUT", f"{ROUTER}/drafts/{C}/files", {"draft_id"}),
    ("draft", "put_file"): ("PUT", f"{ROUTER}/drafts/{C}/file", {"draft_id"}),
    ("draft", "manifest"): ("GET", f"{ROUTER}/drafts/{C}/manifest", {"draft_id"}),
    ("draft", "read_file"): ("GET", f"{ROUTER}/drafts/{C}/file", {"draft_id"}),
    ("draft", "apply"): ("POST", f"{ROUTER}/drafts/{C}/apply", {"draft_id"}),
    ("draft", "repair_publication"): ("POST", ROUTER + "/repair-publication", set()),
    ("draft", "publication_status"): (
        "GET",
        f"{ROUTER}/publications/{A}",
        {"instance_id"},
    ),
    ("outputs", "publish"): ("POST", ROUTER + "/outputs", set()),
    ("outputs", "list"): (
        "GET",
        f"{ROUTER}/outputs/instances/{A}/runs/{B}",
        {"instance_id", "run_id"},
    ),
    ("outputs", "read"): (
        "GET",
        f"{ROUTER}/outputs/instances/{A}/runs/{B}/report-a",
        {"instance_id", "run_id", "output_id"},
    ),
    ("frontend", "inspect"): ("GET", "/api/v1/client/frontend", set()),
    ("frontend", "validate"): ("POST", "/api/v1/client/frontend/validate", set()),
    ("frontend", "select"): ("PUT", "/api/v1/client/frontend", set()),
    ("request", None): ("POST", ROUTER + "/requests", set()),
    ("local_call", None): ("GET", f"{ROUTER}/local-calls/{C}", {"request_id"}),
}
EVENT = b'event: availability\ndata: {"contract":"os.event.v1"}\n\n'


def capability_routes():
    return {
        operation_id: (method, path.replace("{version}", "1"))
        for operation_id, method, path, *_ in CLIENT_OPERATIONS
    }


def test_every_alternative_maps_to_real_capability_operations() -> None:
    alternatives = {("millrace_v2_" + tool, alt) for tool, alt in FIELDS} | {
        ("millrace_v2_" + tool, None) for tool in SINGLE
    }
    assert set(V2_ROUTES) == alternatives | {("millrace_v2_discover", "bridge")}
    assert V2_ROUTES[("millrace_v2_discover", "bridge")] == ()
    routes = capability_routes()
    used = set()
    for key, steps in V2_ROUTES.items():
        if key != ("millrace_v2_discover", "bridge"):
            assert steps, key
        for method, path, operation_id in steps:
            assert routes[operation_id] == (method, path), (key, operation_id)
            used.add(operation_id)
    # Presentation conveniences read, then write; nothing else takes two steps.
    for key, steps in V2_ROUTES.items():
        if len(steps) > 1:
            assert key[0] == "millrace_v2_presentation"
            assert [step[2] for step in steps] == [
                "client.presentation.read",
                "client.presentation.write",
            ]
    # Unmapped by design: the always-refusing action shell and the legacy full
    # reads (v1 tools and the CLI keep those).
    assert set(routes) - used == {
        "actions.invoke",
        "router.instances.read",
        "router.instance.read",
        "router.run.read",
    }


def test_advertisement_hides_tools_with_any_unavailable_operation() -> None:
    names = {"millrace_v2_" + name for name in [*DISCRIMINATORS, *SINGLE]}
    assert set(advertised_v2(ALL)) == names and len(names) == 11
    assert advertised_v2({}) == []
    for missing, hidden in (
        ("router.draft.apply", "millrace_v2_draft"),
        ("router.request.submit", "millrace_v2_request"),
        ("router.views.run", "millrace_v2_inspect"),
        ("router.outputs.publish", "millrace_v2_outputs"),
        ("client.presentation.write", "millrace_v2_presentation"),
        ("router.local_call.read", "millrace_v2_local_call"),
        ("client.frontend.write", "millrace_v2_frontend"),
        ("client.frontend.validate", "millrace_v2_frontend"),
    ):
        listed = advertised_v2(ALL | {missing: False})
        assert hidden not in listed and len(listed) == 10, missing
    assert "millrace_v2_frontend" in advertised_v2(ALL)
    assert not any("demo" in n for n in advertised_v2(ALL))


def v2_call(config, handler, name, arguments, *, capabilities=None, admitted=True):
    async def run():
        transport = httpx.MockTransport(handler)
        backend = BackendClient(config, transport=transport, profile=mc.V2)
        backend.capabilities = dict(ALL if capabilities is None else capabilities)
        bridge = Bridge(config, backend, profile=mc.V2)
        bridge._admitted = lambda: admitted
        try:
            return await bridge.call_tool(name, arguments)
        finally:
            await backend.close()

    return asyncio.run(run())


def parsed(result):
    """The structured value, after checking the compact text carries the same."""
    text = result.content[0].text
    assert json.loads(text) == result.structuredContent
    compact = json.dumps(
        result.structuredContent, ensure_ascii=False, separators=(",", ":")
    )
    assert text == compact
    return result.structuredContent


def test_each_alternative_sends_exactly_its_route(bridge_config) -> None:
    samples = [(tool, alt, arguments(tool, alt)) for tool, alt in EXPECTED_ROUTES
               if alt is not None]
    samples += [(tool, None, copy.deepcopy(SINGLE[tool][1])) for tool in SINGLE]
    for tool, alternative, values in samples:
        sent = []

        def handler(request, sent=sent):
            sent.append(request)
            if request.url.path == "/api/v1/events":
                return httpx.Response(200, content=EVENT)
            return httpx.Response(200, json={"ok": True})

        result = v2_call(bridge_config, handler, "millrace_v2_" + tool, values)
        method, path, in_path = EXPECTED_ROUTES[(tool, alternative)]
        assert len(sent) == 1, (tool, alternative)
        request = sent[0]
        assert (request.method, request.url.path) == (method, path), (tool, alternative)
        rest = {
            k: v
            for k, v in values.items()
            if k not in in_path | {DISCRIMINATORS.get(tool), "observation_contract"}
        }
        if method in {"GET", "OPTIONS"}:
            assert dict(request.url.params) == {k: str(v) for k, v in rest.items()}
            assert not request.content
        else:
            sent_body = json.loads(request.content)
            assert sent_body == rest
            compact = json.dumps(sent_body, ensure_ascii=False, separators=(",", ":"))
            assert request.content == compact.encode()  # the API encoding
        assert request.headers["authorization"] == "Bearer " + "A" * 48
        if "observation_contract" in values:
            version = values["observation_contract"][-2:]
            media = f"application/vnd.millrace.router-call.{version}+json"
            assert request.headers["accept"] == media
        body = parsed(result)
        assert result.isError is False, (tool, alternative, body)
        assert (body["contract"], body["http_status"]) == (mc.CONTRACT_V2, 200)


def test_results_and_refusals_keep_their_meaning(bridge_config) -> None:
    run_args = arguments("inspect", "run")
    request_args = SINGLE["request"][1]
    sent = []

    def answer(status, **content):
        def handler(request):
            sent.append(request)
            return httpx.Response(status, **content)

        return handler

    # 202 is returned at once: one request, no waiting or polling.
    active = answer(202, json={"phase": "call_active"})
    accepted = v2_call(bridge_config, active, "millrace_v2_request", request_args)
    assert accepted.isError is False and parsed(accepted)["http_status"] == 202
    assert len(sent) == 1
    error = {"contract": "os.error.v1", "code": "core_stale_state_version",
             "category": "request", "summary": "core stale state version"}
    refused = v2_call(bridge_config, answer(409, json=error), "millrace_v2_request",
                      request_args)
    body = parsed(refused)
    assert refused.isError is True
    assert (body["http_status"], body["os"]["code"]) == (409, error["code"])
    broken = v2_call(bridge_config, answer(500, content=b"Internal Server Error"),
                     "millrace_v2_inspect", run_args)
    access = parsed(broken)["access_error"]
    assert broken.isError and access["code"] == "backend_http_500"
    assert access["delivery"] == "unknown"
    # Refusals before any HTTP byte are not_sent and reach no backend.
    sent.clear()
    for name, values, options, code in (
        ("millrace_v2_inspect", run_args | {"extra": 1}, {}, "invalid_arguments"),
        ("millrace_v2_demo", {}, {}, "invalid_arguments"),
        ("millrace_v2_frontend", {}, {}, "invalid_arguments"),
        ("millrace_v2_inspect", run_args | {"home": "other"}, {}, "invalid_arguments"),
        ("millrace_v2_inspect", run_args,
         {"capabilities": ALL | {"router.views.run": False}}, "tool_unavailable"),
        ("millrace_v2_inspect", run_args, {"admitted": False}, "unsupported_protocol"),
    ):
        result = v2_call(bridge_config, answer(200, json={}), name, values, **options)
        access = parsed(result)["access_error"]
        assert result.isError and access["code"] == code, name
        assert access["delivery"] == "not_sent"
    assert not sent
    # Timeout after sending is unknown, with the exact known correlation.
    def timeout(request):
        sent.append(request)
        raise httpx.ReadTimeout("do-not-echo")

    result = v2_call(bridge_config, timeout, "millrace_v2_request", request_args)
    access = parsed(result)["access_error"]
    assert (access["code"], access["delivery"]) == ("backend_timeout", "unknown")
    assert access["correlation"] == {"home": "main", "request_id": C}
    assert "do-not-echo" not in json.dumps(parsed(result))

    def unreachable(request):
        raise httpx.ConnectError("refused")

    result = v2_call(bridge_config, unreachable, "millrace_v2_inspect", run_args)
    access = parsed(result)["access_error"]
    assert (access["code"], access["delivery"]) == ("backend_unavailable", "not_sent")
    assert access["correlation"] == {"home": "main", "instance_id": A, "run_id": B}


def test_frontend_reset_is_the_stock_selection_through_the_same_put(
    bridge_config,
) -> None:
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    stock = {"expected_version": A, "desired": {"kind": "stock"}}
    for alternative, values in (
        ("reset", {"expected_version": A}),
        ("select", stock),
    ):
        result = v2_call(
            bridge_config,
            handler,
            "millrace_v2_frontend",
            {"operation": alternative, **values},
        )
        assert result.isError is False, alternative
    assert [(r.method, r.url.path) for r in sent] == [
        ("PUT", "/api/v1/client/frontend")
    ] * 2
    assert json.loads(sent[0].content) == json.loads(sent[1].content) == stock
    sent.clear()
    # Shape errors refuse before HTTP: no version, an extra field, a desired
    # stock choice carrying a path, an over-long path, a non-digest key.
    for values in (
        {"operation": "reset"},
        {"operation": "reset", "expected_version": A, "desired": {"kind": "stock"}},
        {"operation": "select", "expected_version": None,
         "desired": {"kind": "stock", "path": "/opt/board"}},
        {"operation": "validate", "path": "/" + "a" * 3968},
        {"operation": "select", "expected_version": None,
         "desired": LOCAL | {"bundle_sha256": "C" * 64}},
        {"operation": "inspect", "home": "main"},
    ):
        result = v2_call(bridge_config, handler, "millrace_v2_frontend", values)
        access = parsed(result)["access_error"]
        assert (access["code"], access["delivery"]) == (
            "invalid_arguments",
            "not_sent",
        ), values
    assert not sent


def test_busy_is_retryable_and_not_sent(bridge_config) -> None:
    async def run():
        backend = BackendClient(bridge_config, profile=mc.V2)
        backend.capabilities = dict(ALL)
        bridge = Bridge(bridge_config, backend, profile=mc.V2)
        bridge._admitted = lambda: True
        bridge.active = 8
        try:
            return await bridge.call_tool(
                "millrace_v2_local_call", SINGLE["local_call"][1]
            )
        finally:
            await backend.close()

    access = parsed(asyncio.run(run()))["access_error"]
    assert (access["code"], access["status"], access["retryable"]) == (
        "busy",
        429,
        True,
    )
    assert access["delivery"] == "not_sent"


def test_api_body_and_backend_response_bounds_are_exact(bridge_config) -> None:
    sent = []

    def accept(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    shell = {"home": "main", "path": "workflow.json", "content_base64": ""}
    overhead = len(json.dumps(shell, separators=(",", ":")))
    base = arguments("draft", "put_file")
    exact = base | {"content_base64": "A" * (1_048_576 - overhead)}
    assert v2_call(bridge_config, accept, "millrace_v2_draft", exact).isError is False
    assert len(sent[0].content) == 1_048_576
    over = base | {"content_base64": "A" * (1_048_576 - overhead + 1)}
    result = v2_call(bridge_config, accept, "millrace_v2_draft", over)
    access = parsed(result)["access_error"]
    assert (access["code"], access["status"], access["delivery"]) == (
        "request_too_large",
        413,
        "not_sent",
    )
    assert access["correlation"]["draft_id"] == C
    assert len(sent) == 1
    for size, refused in ((R, False), (R + 1, True)):
        filler = "x" * (size - len('{"x":""}'))
        raw = json.dumps({"x": filler}, separators=(",", ":")).encode()
        assert len(raw) == size

        def backend(request, raw=raw):
            return httpx.Response(200, content=raw)

        result = v2_call(
            bridge_config, backend, "millrace_v2_inspect", arguments("inspect", "run")
        )
        body = parsed(result)
        assert result.isError is refused, size
        if refused:
            assert body["access_error"]["code"] == "backend_response_too_large"
            assert body["access_error"]["delivery"] == "unknown"
        else:
            assert body["os"] == {"x": filler}


def test_result_check_uses_the_joint_text_and_structured_bound(bridge_config) -> None:
    backend = BackendClient(bridge_config, profile=mc.V2)
    backend.capabilities = dict(ALL)
    bridge = Bridge(bridge_config, backend, profile=mc.V2)
    ceiling = mc.V2_MAX_OUTPUT - mc.V2_MAX_INPUT - 1024
    wrapper = {"contract": mc.CONTRACT_V2, "http_status": 200, "os": {"x": ""}}
    empty = len(json.dumps(wrapper, separators=(",", ":")))
    # A maximal R-sized backend body whose string is all escapes, and one of
    # dense floats that the SDK spells longer than Python: both fit 3R + 1,024.
    quotes = "\\" * ((R - len('{"x":""}')) // 2)
    floats = [1e-05] * ((R - len('{"x":[]}')) // 6)
    for value in (quotes, floats):
        body = {"x": value}
        payload = wrapper | {"os": body}
        backend_size = len(json.dumps(body, separators=(",", ":")))
        assert R - 8 <= backend_size <= R  # a maximal backend body
        size = len(json.dumps(payload, separators=(",", ":")))
        assert size - backend_size == empty - len('{"x":""}') == 63  # W
        result = bridge._v2_result(payload)
        assert result.isError is False
        wire = result.model_dump_json(by_alias=True, exclude_none=True)
        assert len(wire.encode()) <= ceiling
    oversized = wrapper | {"os": {"x": '"' * (2 * R)}}
    result = bridge._v2_result(oversized, {"home": "main"})
    access = result.structuredContent["access_error"]
    assert result.isError and access["code"] == "response_too_large"
    assert (access["delivery"], access["correlation"]) == ("unknown", {"home": "main"})
    asyncio.run(backend.close())


def test_discover_bridge_is_local_and_describes_the_profile(bridge_config) -> None:
    def forbidden(request):
        raise AssertionError("bridge discovery reads no OS state")

    result = v2_call(
        bridge_config, forbidden, "millrace_v2_discover", {"kind": "bridge"},
        capabilities=ALL | {"router.draft.apply": False},
    )
    body = parsed(result)
    assert result.isError is False
    assert body["tool_schema_sha256"] == mc.schema_bundle_v2()["sha256"]
    assert "millrace_v2_draft" not in body["tools"] and len(body["tools"]) == 10
    assert body["homes"] == ["main"] and body["api"] == "v1"
    assert body["limits"] == {
        "max_input": 2_162_688,
        "max_output": 5_313_536,
        "max_api_response": 1_049_600,
        "backend_timeout_seconds": 5,
        "concurrency": 8,
    }
    assert len(body["mappings"]) == sum(len(s) for s in V2_ROUTES.values())


def presentation_state(tmp_path, *, ready):
    home = tmp_path / ("ready" if ready else "absent")
    home.mkdir(mode=0o700)
    if ready:
        store = {"format": p.STORE_FORMAT, "namespaces": []}
        p.write_state(
            home,
            {
                "format": p.WRITE_FORMAT,
                "mode": "compare",
                "expected_version": None,
                "store": store,
            },
        )
    return json.loads(json.dumps(p.read_state(home)))


def test_presentation_conveniences_make_one_get_then_at_most_one_put(
    bridge_config, tmp_path
) -> None:
    absent = presentation_state(tmp_path, ready=False)
    ready = presentation_state(tmp_path, ready=True)
    conflict = {"contract": "os.error.v1", "code": "presentation_conflict",
                "category": "request", "summary": "presentation conflict"}

    def server(state, put_status=200):
        sent = []

        def handler(request):
            sent.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=state)
            if put_status == 200:
                return httpx.Response(200, json=state)
            return httpx.Response(put_status, json=conflict)

        return sent, handler

    sent, handler = server(absent)
    create = arguments("presentation", "create")
    result = v2_call(bridge_config, handler, "millrace_v2_presentation", create)
    assert result.isError is False
    assert [r.method for r in sent] == ["GET", "PUT"]
    write = json.loads(sent[1].content)
    assert (write["mode"], write["expected_version"]) == ("compare", None)
    (namespace,) = write["store"]["namespaces"]
    assert namespace["configs"][0]["name"] == "Mine"
    # A conflicting PUT is returned once, never retried.
    sent, handler = server(absent, put_status=409)
    result = v2_call(bridge_config, handler, "millrace_v2_presentation", create)
    body = parsed(result)
    assert result.isError and (body["http_status"], body["os"]["code"]) == (
        409,
        "presentation_conflict",
    )
    assert [r.method for r in sent] == ["GET", "PUT"]
    # A helper refusal for the observed state comes before any PUT.
    for values, code, status in (
        (arguments("presentation", "update"), "presentation_config_not_found", 404),
        (create | {"expected_version": B}, "presentation_conflict", 409),
    ):
        sent, handler = server(absent)
        body = parsed(
            v2_call(bridge_config, handler, "millrace_v2_presentation", values)
        )
        assert (body["http_status"], body["os"]["code"]) == (status, code)
        assert [r.method for r in sent] == ["GET"]
    # Explicit null versus omission: null now requires an absent store.
    sent, handler = server(ready)
    body = parsed(
        v2_call(bridge_config, handler, "millrace_v2_presentation",
                create | {"expected_version": None})
    )
    assert body["os"]["code"] == "presentation_conflict" and len(sent) == 1
    sent, handler = server(ready)
    assert not v2_call(
        bridge_config, handler, "millrace_v2_presentation", create
    ).isError
    assert json.loads(sent[1].content)["expected_version"] == ready["version"]
    # Derived reads use the GET only.
    sent, handler = server(ready)
    body = parsed(
        v2_call(bridge_config, handler, "millrace_v2_presentation",
                arguments("presentation", "read_default"))
    )
    assert body["presentation"]["result"]["id"] == "precision"
    assert (body["http_status"], len(sent)) == (200, 1)
    # Raw writes are checked by the shared helper before any HTTP.
    sent, handler = server(absent)
    raw = arguments("presentation", "put")
    assert not v2_call(bridge_config, handler, "millrace_v2_presentation", raw).isError
    assert [r.method for r in sent] == ["PUT"]
    assert json.loads(sent[0].content) == raw["write"]
    stock = {"frontend_id": "stock", "presentation_format": p.STOCK_FORMAT,
             "default_ref": {"kind": "bundled", "id": "default"}, "configs": []}
    invalid = copy.deepcopy(raw)
    invalid["write"]["store"]["namespaces"] = [stock]
    sent, handler = server(absent)
    access = parsed(
        v2_call(bridge_config, handler, "millrace_v2_presentation", invalid)
    )["access_error"]
    assert (access["code"], access["delivery"]) == ("invalid_arguments", "not_sent")
    assert not sent


def test_cli_selects_the_tool_profile_and_keeps_v1_default(monkeypatch) -> None:
    from millrace_os import cli, mcp_bridge

    calls = []
    monkeypatch.setattr(mcp_bridge, "serve", lambda *a, **k: calls.append((a, k)))
    assert cli.main(["mcp", "serve", "--config", "bridge.json"]) == 0
    assert cli.main(["mcp", "serve", "--config", "bridge.json", "--tools", "v2"]) == 0
    assert calls == [
        (("bridge.json", "stdio"), {}),
        (("bridge.json", "stdio"), {"tools": "v2"}),
    ]
    with pytest.raises(SystemExit):
        cli.main(["mcp", "serve", "--config", "bridge.json", "--tools", "v3"])


def test_an_opaque_nested_id_never_enlarges_a_refusal(bridge_config) -> None:
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={})

    # The request body is forwarded unchanged; its non-ID "instance_id" is
    # admitted, then refused by the 4,096-byte route policy before HTTP.
    request = {
        "operation": "start_run",
        "request_id": C,
        "instance_id": '"' * 600_000,
        "run_id": B,
    }
    values = {
        "home": "main",
        "request": request,
        "observation_contract": "millrace-os-router-call/v1",
    }
    result = v2_call(bridge_config, handler, "millrace_v2_request", values)
    access = parsed(result)["access_error"]
    assert result.isError
    assert (access["code"], access["delivery"]) == ("request_too_large", "not_sent")
    assert access["correlation"] == {"home": "main", "request_id": C, "run_id": B}
    assert len(result.content[0].text) < 1024 and not sent
    # The wire-size fallback is one small refusal and keeps a proven not_sent.
    backend = BackendClient(bridge_config, profile=mc.V2)
    backend.capabilities = dict(ALL)
    bridge = Bridge(bridge_config, backend, profile=mc.V2)
    huge = mc.access_error_v2(
        "request_too_large",
        status=413,
        delivery="not_sent",
        correlation={"note": '"' * (2 * R)},
    )
    fallback = bridge._v2_result(huge, {"home": "main"})
    access = fallback.structuredContent["access_error"]
    assert (access["code"], access["delivery"], access["correlation"]) == (
        "response_too_large",
        "not_sent",
        {"home": "main"},
    )
    assert fallback.isError and len(fallback.content[0].text) < 1024
    asyncio.run(backend.close())


def test_a_convenience_put_refused_after_its_get_is_unknown(bridge_config) -> None:
    namespace = {
        **BOARD,
        "default_ref": None,
        "configs": [{"id": A, "name": "Big", "payload": {"blob": "x" * 900_000}}],
    }
    state = {
        "contract": p.STATE_CONTRACT,
        "status": "ready",
        "version": B,
        "file_sha256": D,
        "store": {"format": p.STORE_FORMAT, "version": B, "namespaces": [namespace]},
        "bundled_configs": p.bundled_configs(),
        "observed_at": "2026-11-07T00:00:00+00:00",
    }
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=state)

    # The derived write (current store plus this payload) exceeds the 1 MiB
    # body policy, so its PUT is never sent; the GET already was.
    values = {
        "operation": "create",
        "namespace": BOARD,
        "name": "More",
        "payload": {"blob": "y" * 300_000},
    }
    result = v2_call(bridge_config, handler, "millrace_v2_presentation", values)
    access = parsed(result)["access_error"]
    assert result.isError
    assert (access["code"], access["delivery"]) == ("request_too_large", "unknown")
    assert [request.method for request in sent] == ["GET"]
