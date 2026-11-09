"""Generic workflow output routes (V1): strict bodies, queries, envelopes, errors."""

import ast
import copy
import importlib
import json
import re
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_auth import setup

from millrace_os import api
from millrace_os.contracts import WorkflowOutputItem
from millrace_os.router_client import OUTPUT_STATUS

INSTANCE, RUN, OUTPUT = "a" * 32, "b" * 32, "report-a"
RECORD_SHA256 = "e" * 64
PUBLISH = "/api/v1/router/outputs"
LIST = f"/api/v1/router/outputs/instances/{INSTANCE}/runs/{RUN}"
READ = f"{LIST}/{OUTPUT}"
READ_QUERY = f"home=default&record_sha256={RECORD_SHA256}&representation=report"
REFERENCE = {
    "instance_id": INSTANCE,
    "run_id": RUN,
    "output_id": OUTPUT,
    "record_sha256": RECORD_SHA256,
}
PRODUCER = {"id": "example.exporter", "version": "1.0.0"}
INVOCATION = {
    "plane_id": "main",
    "node_id": "measure",
    "invocation_id": "c" * 32,
    "producing_revision_id": "9" * 64,
}
REPORT = {
    "format": "millrace-report-display/v1",
    "media_type": "application/json",
    "sha256": "f" * 64,
}
AVAILABLE = {
    "output_id": OUTPUT,
    "availability": "available",
    "reason": None,
    "record_sha256": RECORD_SHA256,
    "producer": PRODUCER,
    "label": "Measurement",
    "published_at": "2026-11-07T12:00:00Z",
    "invocation": INVOCATION,
    "representations": {
        "report": REPORT,
        "json": dict(REPORT, format="example.measure/v1", sha256="1" * 64),
    },
}
KNOWN = ("record_sha256", "producer", "label", "published_at", "invocation")
UNAVAILABLE = {
    "output_id": "broken",
    "availability": "unavailable",
    "reason": "output_integrity_error",
    **dict.fromkeys((*KNOWN, "representations")),
}
RESULTS = {
    "publish_output": {
        "reference": REFERENCE,
        "report_sha256": "f" * 64,
        "json_sha256": None,
        "status": "published",
    },
    "list_outputs": {
        "instance_id": INSTANCE,
        "run_id": RUN,
        "items": [AVAILABLE, UNAVAILABLE],
        "next_cursor": None,
    },
    "read_output": {
        "reference": REFERENCE,
        "producer": PRODUCER,
        "label": "Measurement",
        "published_at": "2026-11-07T12:00:00Z",
        "invocation": INVOCATION,
        "representation": {"name": "report", **REPORT, "data": {"title": "café"}},
    },
}


class FakeViewsError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def forbidden(*args, **kwargs):
    raise AssertionError("this path must not be reached")


def helper(calls, *, outcomes=None, marker="millrace-workflow-views/v1"):
    """A stand-in for the public Plus output helper recording exact calls."""
    module = types.SimpleNamespace(WorkflowViewsError=FakeViewsError)
    if marker is not None:
        module.WORKFLOW_VIEWS_CONTRACT = marker

    def forward(name):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            outcome = (outcomes or {}).get(name, RESULTS[name])
            if isinstance(outcome, BaseException):
                raise outcome
            return copy.deepcopy(outcome)

        return call

    for name in RESULTS:
        setattr(module, name, forward(name))
    return module


def outputs_service(tmp_path, monkeypatch, calls, **options):
    service, client, bearer = setup(tmp_path)
    home = tmp_path / "core"
    service.router_client.homes = {"default": home}
    module = helper(calls, **options)
    monkeypatch.setattr(service.router_client, "_views", lambda: module)
    monkeypatch.setattr(service.router_client, "_inspection", lambda: None)
    return service, client, bearer, home


def post(client, bearer, raw: bytes):
    headers = {**bearer, "Content-Type": "application/json"}
    return client.post(PUBLISH, content=raw, headers=headers)


def body(record: object) -> bytes:
    payload = {"home": "default", "record": record}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def test_output_routes_require_authentication(tmp_path, monkeypatch) -> None:
    calls = []
    _, client, _, _ = outputs_service(tmp_path, monkeypatch, calls)
    unauthenticated = client.post(PUBLISH, json={"home": "default", "record": {}})
    assert unauthenticated.status_code == 401
    assert client.get(LIST + "?home=default").status_code == 401
    assert client.get(READ + "?" + READ_QUERY).status_code == 401
    assert calls == []


def test_output_query_allowlist_and_strict_selectors(tmp_path, monkeypatch) -> None:
    for path, keys in (
        (LIST, {"home", "limit", "cursor"}),
        (READ, {"home", "record_sha256", "representation"}),
        ("/api/v1/router/outputs", set()),
        (f"/api/v1/router/outputs/instances/{INSTANCE}", set()),
        (f"/api/v1/router/outputs/instances/{INSTANCE}/runs", set()),
        (f"{READ}/extra", set()),
    ):
        assert api.query_allowlist(path, "GET") == frozenset(keys), path
    assert api.query_allowlist(PUBLISH, "POST") == frozenset()
    calls = []
    _, client, bearer, home = outputs_service(tmp_path, monkeypatch, calls)
    for route in (
        LIST + "?home=default&home=default",
        LIST + "?home=default&section=summary",
        LIST + "?home=default&limit=0",
        LIST + "?home=default&limit=51",
        LIST + "?home=default&limit=1.0",
        LIST + "?home=default&cursor=a!b",
        LIST,
        READ + "?" + READ_QUERY + "&limit=1",
        READ + "?home=default&representation=report",
        READ + f"?home=default&record_sha256={RECORD_SHA256}",
        READ + f"?home=default&record_sha256={RECORD_SHA256}&representation=html",
        READ + f"?home=default&record_sha256={'E' * 64}&representation=report",
        READ + "?home=default&record_sha256=e&representation=report",
    ):
        response = client.get(route, headers=bearer)
        assert response.status_code == 422, (route, response.text)
        assert response.json()["code"] == "invalid_request"
    response = client.post(
        PUBLISH + "?home=default",
        headers=bearer,
        json={"home": "default", "record": {}},
    )
    assert response.status_code == 422
    unknown = client.get(LIST + "?home=unknown", headers=bearer)
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "unknown_router_home"
    assert calls == [] and not home.exists()


def test_publish_raw_body_is_strict_before_model_validation(
    tmp_path, monkeypatch
) -> None:
    calls = []
    _, client, bearer, _ = outputs_service(tmp_path, monkeypatch, calls)
    for raw in (
        b'{"home":"default","home":"other","record":{}}',
        b'{"home":"default","record":{"label":"a","label":"b"}}',
        b'{"home":"default","record":{"value":NaN}}',
        b'{"home":"default","record":{"value":Infinity}}',
        b'{"home":"default","record":{"value":-Infinity}}',
        b'{"home":"default","record":{"label":"\xff"}}',
        b'\xef\xbb\xbf{"home":"default","record":{}}',
        b'{"home":"default","record":{}} trailing',
        b'{"home":"default","record":',
    ):
        response = post(client, bearer, raw)
        assert response.status_code == 422, raw
        assert response.json()["code"] == "invalid_request"
    for payload in (
        {"home": "default"},
        {"home": "default", "record": []},
        {"home": "default", "record": {}, "extra": True},
        {"home": "bad/name", "record": {}},
    ):
        response = post(client, bearer, json.dumps(payload).encode())
        assert response.status_code == 422, payload
    assert calls == []


def test_publish_body_limit_is_one_mebibyte(tmp_path, monkeypatch) -> None:
    calls = []
    service, client, bearer, home = outputs_service(tmp_path, monkeypatch, calls)
    assert api.body_limit(PUBLISH, "POST") == 1_048_576
    assert api.body_limit(PUBLISH, "PUT") == api.DEFAULT_BODY_LIMIT
    empty = body({"pad": ""})
    exact = body({"pad": "x" * (1_048_576 - len(empty))})
    assert len(exact) == 1_048_576
    response = post(client, bearer, exact)
    assert response.status_code == 200, response.text[:200]
    record = json.loads(exact)["record"]
    assert calls == [("publish_output", (record,), {"state_home": home})]
    calls.clear()
    over = body({"pad": "x" * (1_048_577 - len(empty))})
    response = post(client, bearer, over)
    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert calls == []
    document = json.loads(api.export_artifacts(service.app)["openapi.json"])
    request = document["paths"][PUBLISH]["post"]["requestBody"]
    assert request["x-max-body-bytes"] == 1_048_576


def test_envelopes_are_exact_compact_and_labeled(tmp_path, monkeypatch) -> None:
    calls = []
    _, client, bearer, home = outputs_service(tmp_path, monkeypatch, calls)
    record = {"output_id": OUTPUT, "label": "Résumé"}
    published = post(client, bearer, body(record))
    assert published.status_code == 200, published.text
    envelope = published.json()
    assert list(envelope) == [
        "contract",
        "home",
        "authority",
        "observed_at",
        "reference",
        "report_sha256",
        "json_sha256",
        "status",
    ]
    assert envelope["contract"] == "millrace-os-workflow-output-publication/v1"
    assert envelope["authority"] == "workflow-produced-evidence"
    assert {key: envelope[key] for key in RESULTS["publish_output"]} == RESULTS[
        "publish_output"
    ]
    listed = client.get(LIST + "?home=default&limit=2&cursor=o_x", headers=bearer)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page == {
        "contract": "millrace-os-workflow-outputs/v1",
        "home": "default",
        "instance_id": INSTANCE,
        "run_id": RUN,
        "observed_at": page["observed_at"],
        "items": [AVAILABLE, UNAVAILABLE],
        "next_cursor": None,
    }
    read = client.get(READ + "?" + READ_QUERY, headers=bearer)
    assert read.status_code == 200, read.text
    document = read.json()
    assert document == {
        "contract": "millrace-os-workflow-output/v1",
        "home": "default",
        "authority": "workflow-produced-evidence",
        "observed_at": document["observed_at"],
        **RESULTS["read_output"],
    }
    compact = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    assert read.content == compact.encode() and "café".encode() in read.content
    assert calls == [
        ("publish_output", (record,), {"state_home": home}),
        (
            "list_outputs",
            (INSTANCE, RUN),
            {"state_home": home, "limit": 2, "cursor": "o_x"},
        ),
        (
            "read_output",
            (INSTANCE, RUN, OUTPUT, RECORD_SHA256, "report"),
            {"state_home": home},
        ),
    ]


@pytest.mark.parametrize(
    "code,status",
    [
        *OUTPUT_STATUS.items(),
        ("output_conflict", 409),
        ("output_identity_mismatch", 409),
        ("output_integrity_error", 409),
        ("outputs_changed", 409),
        ("some_future_refusal", 409),
    ],
)
def test_typed_helper_refusals_map_to_os_statuses(
    tmp_path, monkeypatch, code, status
) -> None:
    calls = []
    refusal = FakeViewsError(code)
    _, client, bearer, _ = outputs_service(
        tmp_path,
        monkeypatch,
        calls,
        outcomes={name: refusal for name in RESULTS},
    )
    for response in (
        post(client, bearer, body({})),
        client.get(LIST + "?home=default", headers=bearer),
        client.get(READ + "?" + READ_QUERY, headers=bearer),
    ):
        assert response.status_code == status
        assert response.json()["code"] == code
        assert response.json()["contract"] == "os.error.v1"


def test_publish_io_and_capability_failures_are_typed(tmp_path, monkeypatch) -> None:
    for outcome, code in (
        (OSError(28, "no space left"), "output_publication_uncertain"),
        (ImportError("core_contract_missing"), "router_inspection_unavailable"),
    ):
        calls = []
        _, client, bearer, _ = outputs_service(
            tmp_path / code, monkeypatch, calls, outcomes={"publish_output": outcome}
        )
        response = post(client, bearer, body({}))
        assert (response.status_code, response.json()["code"]) == (503, code)


@pytest.mark.parametrize("variant", ["import", "absent", "old"])
def test_missing_or_mismatched_helper_is_a_typed_503(
    tmp_path, monkeypatch, variant
) -> None:
    service, client, bearer = setup(tmp_path)
    service.router_client.homes = {"default": tmp_path / "core"}
    original = importlib.import_module
    stand_in = helper([], marker=None if variant == "absent" else "views/v0")

    def resolve(name, *args, **kwargs):
        if name == "millrace_plus.workflow_views":
            if variant == "import":
                raise ImportError("optional helper unavailable")
            return stand_in
        if name.startswith(("millrace_plus", "millrace.")):
            raise AssertionError("no other optional module is needed first")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", resolve)
    monkeypatch.setattr(service.router_client, "_inspection", forbidden)
    for response in (
        post(client, bearer, body({})),
        client.get(LIST + "?home=default", headers=bearer),
        client.get(READ + "?" + READ_QUERY, headers=bearer),
    ):
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "workflow_views_unavailable"
        assert response.json()["retryable"] is False


def test_publish_refuses_while_closing_and_reads_continue(
    tmp_path, monkeypatch
) -> None:
    calls = []
    service, client, bearer, _ = outputs_service(tmp_path, monkeypatch, calls)
    service.router_workers._closing = True
    response = post(client, bearer, body({}))
    assert (response.status_code, response.json()["code"]) == (503, "router_closing")
    assert calls == []
    assert client.get(LIST + "?home=default", headers=bearer).status_code == 200
    assert client.get(READ + "?" + READ_QUERY, headers=bearer).status_code == 200


def test_output_routes_share_the_existing_eight_read_slots(
    tmp_path, monkeypatch
) -> None:
    calls = []
    service, client, bearer, _ = outputs_service(tmp_path, monkeypatch, calls)
    module = service.router_client._views()
    entered, release = threading.Semaphore(0), threading.Event()

    def blocking(*args, **kwargs):
        entered.release()
        assert release.wait(10)
        return copy.deepcopy(RESULTS["list_outputs"])

    module.list_outputs = blocking
    # Build the middleware stack once before concurrent first requests.
    assert client.get("/api/v1/router/homes", headers=bearer).status_code == 200
    statuses = []

    def read():
        own = TestClient(service.app, base_url="http://127.0.0.1:8731")
        statuses.append(own.get(LIST + "?home=default", headers=bearer).status_code)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(8):
            assert entered.acquire(timeout=10)
        for response in (
            post(client, bearer, body({})),
            client.get(READ + "?" + READ_QUERY, headers=bearer),
        ):
            assert response.status_code == 429
            assert response.json()["code"] == "router_read_busy"
        assert calls == []
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
    assert statuses == [200] * 8


def test_read_response_is_bounded(tmp_path, monkeypatch) -> None:
    large = copy.deepcopy(RESULTS["read_output"])
    large["representation"]["data"] = {"blob": "x" * api.RESPONSE_LIMIT}
    _, client, bearer, _ = outputs_service(
        tmp_path, monkeypatch, [], outcomes={"read_output": large}
    )
    response = client.get(READ + "?" + READ_QUERY, headers=bearer)
    assert (response.status_code, response.json()["code"]) == (
        409,
        "response_too_large",
    )


def test_unavailable_items_carry_only_their_identity() -> None:
    assert WorkflowOutputItem.model_validate(AVAILABLE).model_dump(
        by_alias=True
    ) == AVAILABLE
    assert WorkflowOutputItem.model_validate(UNAVAILABLE).model_dump(
        by_alias=True
    ) == UNAVAILABLE
    for item in (
        dict(UNAVAILABLE, label="leaked"),
        dict(UNAVAILABLE, reason=None),
        dict(AVAILABLE, producer=None),
        dict(AVAILABLE, reason="output_integrity_error"),
        dict(AVAILABLE, availability="unknown"),
        dict(AVAILABLE, representations={"report": REPORT}),
        dict(AVAILABLE, representations={"report": REPORT, "json": None, "x": None}),
    ):
        with pytest.raises(ValidationError):
            WorkflowOutputItem.model_validate(item)


def test_exported_schemas_use_standard_bounds_and_the_json_wire_name(
    tmp_path,
) -> None:
    service, _, _ = setup(tmp_path)
    artifacts = api.export_artifacts(service.app)
    document = json.loads(artifacts["openapi.json"])
    route = "/api/v1/router/outputs/instances/{instance_id}/runs/{run_id}"
    parameters = {
        parameter["name"]: parameter
        for parameter in document["paths"][route]["get"]["parameters"]
    }
    assert parameters["limit"]["schema"] == {
        "default": 50,
        "maximum": 50,
        "minimum": 1,
        "title": "Limit",
        "type": "integer",
    }
    read = {
        parameter["name"]: parameter
        for parameter in document["paths"][route + "/{output_id}"]["get"][
            "parameters"
        ]
    }
    assert read["representation"]["required"] is True
    assert read["representation"]["schema"]["enum"] == ["report", "json"]
    assert read["record_sha256"]["schema"]["pattern"] == "^[0-9a-f]{64}$"
    contracts = json.loads(artifacts["contracts.json"])
    registry = {entry["contract_id"] for entry in json.loads(
        artifacts["registry.json"]
    )["schemas"]}
    assert {
        "WorkflowOutputPublishInput",
        "WorkflowOutputPublication",
        "WorkflowOutputs",
        "WorkflowOutput",
    } <= registry
    representations = contracts["WorkflowOutputs"]["$defs"]["OutputRepresentations"]
    assert set(representations["properties"]) == {"report", "json"}
    assert set(representations["required"]) == {"report", "json"}


def test_output_code_has_no_domain_specific_branch() -> None:
    source = Path(api.__file__).parent
    pattern = re.compile(r"(?i)\b(lad|cicd|factory|demo|millforge|paperclip)\b")
    allowed = {
        "millrace.router.execution",
        "millrace_plus.catalog",
        "millrace_plus.router",
        "millrace_plus.router.control",
        "millrace_plus.workflow_views",
    }
    output_targets = {
        "api.py": {"output_publish", "output_list", "output_read"},
        "contracts.py": {
            "OutputReadQuery", "WorkflowOutputPublishInput", "OutputReference",
            "OutputProducer", "OutputInvocation", "OutputRepresentationInfo",
            "OutputRepresentations", "WorkflowOutputItem", "WorkflowOutputs",
            "WorkflowOutputPublication", "OutputRepresentation", "WorkflowOutput",
        },
    }
    for name in ("api.py", "router_client.py", "contracts.py", "cli.py"):
        tree = ast.parse((source / name).read_text())
        texts = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        domain_texts = texts
        if name in output_targets:
            expected = output_targets[name]
            node_type = ast.AsyncFunctionDef if name == "api.py" else ast.ClassDef
            targets = [
                node for node in ast.walk(tree)
                if isinstance(node, node_type) and node.name in expected
            ]
            assert targets and {node.name for node in targets} == expected, name
            assert len(targets) == len(expected), name
            domain_texts = [
                node.value
                for target in targets
                for node in ast.walk(target)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
        assert not [text for text in domain_texts if pattern.search(text)], name
        modules = {
            text for text in texts if text.startswith(("millrace.", "millrace_"))
        }
        assert modules <= allowed | {"millrace_os", "millrace_os.recovery"}, name


def run_cli(monkeypatch, tmp_path, *argv):
    from millrace_os.cli import main

    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            return Response()

    monkeypatch.setattr(
        "millrace_os.cli.urllib.request.build_opener", lambda *handlers: Opener()
    )
    config = [
        "--config-dir",
        str(tmp_path / "config"),
        "--state-dir",
        str(tmp_path / "state"),
    ]
    assert main(["router", "outputs", *argv, *config]) == 0
    return captured[-1]


def test_cli_outputs_send_exact_routes_and_compact_saved_bytes(
    tmp_path, monkeypatch
) -> None:
    base = "http://127.0.0.1:8731/api/v1/router/outputs"
    saved = tmp_path / "record.json"
    saved.write_bytes('{"label": "Résumé", "items": [1, 2.5]}'.encode())
    request = run_cli(
        monkeypatch, tmp_path, "publish", "--home", "h", "--file", str(saved)
    )
    assert (request.full_url, request.get_method()) == (base, "POST")
    assert request.data == (
        '{"home":"h","record":{"label":"Résumé","items":[1,2.5]}}'.encode()
    )
    assert request.headers["Content-type"] == "application/json"
    request = run_cli(
        monkeypatch,
        tmp_path,
        "list",
        "--home",
        "h",
        "--instance",
        "i",
        "--run",
        "r",
        "--limit",
        "2",
        "--cursor",
        "o_x",
    )
    assert request.full_url == base + "/instances/i/runs/r?home=h&limit=2&cursor=o_x"
    assert (request.get_method(), request.data) == ("GET", None)
    request = run_cli(
        monkeypatch,
        tmp_path,
        "read",
        "--home",
        "h",
        "--instance",
        "i",
        "--run",
        "r",
        "--output",
        "o",
        "--record-sha256",
        RECORD_SHA256,
        "--representation",
        "json",
    )
    assert request.full_url == (
        base
        + f"/instances/i/runs/r/o?home=h&record_sha256={RECORD_SHA256}"
        + "&representation=json"
    )
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b"\xff", b"[1,"):
        saved.write_bytes(raw)
        with pytest.raises(SystemExit) as exit_:
            run_cli(
                monkeypatch, tmp_path, "publish", "--home", "h", "--file", str(saved)
            )
        assert exit_.value.code == 2, raw
    selectors = ("--home", "h", "--instance", "i", "--run", "r", "--output", "o")
    for argv in (
        ("read", *selectors, "--record-sha256", RECORD_SHA256, "--representation", "x"),
        ("list", "--home", "h", "--instance", "i"),
    ):
        with pytest.raises(SystemExit) as exit_:
            run_cli(monkeypatch, tmp_path, *argv)
        assert exit_.value.code == 2, argv
