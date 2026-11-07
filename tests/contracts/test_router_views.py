"""Bounded router views (UI1): query allowlist, envelopes and strict observation."""

import copy
import importlib
import json
import re
import threading
import types

import pytest
from fastapi.testclient import TestClient
from test_auth import setup

from millrace_os import api
from millrace_os.router_client import RouterUnavailable, request_digest
from millrace_os.router_workers import OwnedCall

MARKER = "millrace-router-inspection/v1"
FORWARDS = (
    "list_instances_page",
    "inspect_instance_page",
    "inspect_run_page",
    "inspect_revision_graph",
    "validate_revision_summary",
)
INSTANCE, OTHER, RUN, RUN2, REQUEST = "a" * 32, "f" * 32, "b" * 32, "9" * 32, "c" * 32
REVISION = "e" * 64
VIEWS = "/api/v1/router/views/instances"
STRICT = "/api/v1/router/views/requests/"
V2 = "application/vnd.millrace.router-call.v2+json"
VIEW_KEYS = {"contract", "home", "data", "source_provider", "authority", "observed_at"}


def forbidden(*args, **kwargs):
    raise AssertionError("this path must not be reached")


def facade(calls, *, marker=MARKER, result=None):
    """A stand-in Plus control facade that records exact forwarded arguments."""

    def forward(name):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            return {"format": MARKER, "kind": name} if result is None else result

        return call

    module = types.SimpleNamespace(**{name: forward(name) for name in FORWARDS})
    module.list_instances = forward("list_instances")
    if marker is not None:
        module.INSPECTION_CONTRACT = marker
    return module


def views_service(tmp_path, monkeypatch, module):
    service, client, bearer = setup(tmp_path)
    home = tmp_path / "core"
    service.router_client.homes = {"default": home}
    monkeypatch.setattr(service.router_client, "_control", lambda: module)
    return service, client, bearer, home


def legacy_refuses(path, method, keys):
    """The exact pre-view middleware decision, kept as the regression oracle."""
    if path.startswith("/api/v1/router/") and method == "GET":
        parts = path.split("/")
        documented = (
            path in {"/api/v1/router/instances", "/api/v1/router/catalog/templates"}
            or (len(parts) == 6 and parts[4] in {"instances", "requests", "drafts"})
            or (len(parts) == 8 and parts[4] == "instances" and parts[6] == "runs")
        )
        allowed = {"home"} if documented else set()
        if documented and parts[4] == "requests":
            allowed |= {"instance_id", "run_id"}
        return len(keys) != len(set(keys)) or bool(set(keys) - allowed)
    if path == "/api/v1/router/requests" and method == "OPTIONS":
        return keys != ["home"]
    return bool(keys) and path not in {"/api/v1/providers", "/api/v1/actions"}


def table_refuses(path, method, keys):
    allowed = api.query_allowlist(path, method)
    if allowed is None:
        return False
    if len(keys) != len(set(keys)) or set(keys) - allowed:
        return True
    exact = path == "/api/v1/router/requests" and method == "OPTIONS"
    return exact and keys != ["home"]


VIEW_GRANTS = {
    "/api/v1/router/homes": {"limit", "cursor"},
    VIEWS: {"home", "limit", "cursor"},
    VIEWS + "/x": {"home", "limit", "cursor"},
    VIEWS + "/x/runs/x": {
        "home",
        "section",
        "limit",
        "cursor",
        "request_id",
        "invocation_id",
    },
    VIEWS + "/x/runs/x/revisions/x": {"home"},
    STRICT + "x": {"home", "instance_id", "run_id"},
}
KEY_SAMPLES = (
    [],
    [""],
    ["home"],
    ["home", "home"],
    ["home", "instance_id"],
    ["home", "instance_id", "run_id"],
    ["home", "run_id", "run_id"],
    ["limit"],
    ["cursor"],
    ["limit", "cursor"],
    ["limit", "limit"],
    ["home", "limit", "cursor"],
    ["home", "section", "limit", "cursor", "request_id", "invocation_id"],
    ["home", "section", "section"],
    ["extra"],
    ["state_home"],
    ["home", "path"],
)
METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")


def test_query_allowlist_reproduces_every_preexisting_route_and_adds_views(tmp_path):
    service, _, _ = setup(tmp_path)
    routes = {
        re.sub(r"\{[^}]+\}", "x", route.path)
        for route in service.app.routes
        if getattr(route, "path", "").startswith("/")
    }
    edges = {
        "/api/v1/router",
        "/api/v1/router/",
        "/api/v1/router/views",
        "/api/v1/router/views/",
        "/api/v1/router/views/other",
        "/api/v1/router/views/requests",
        "/api/v1/router/views/instances/x/runs",
        "/api/v1/router/views/instances/x/runs/x/extra",
        "/api/v1/router/views/instances/x/runs/x/revisions",
        "/api/v1/router/views/instances/x/runs/x/revisions/x/extra",
        "/api/v1/router/homes/x",
        "/api/v1/router/viewsx/instances",
        "/api/v1/routerx/instances",
        "/api/v1/providers/x",
    }
    for path in sorted(routes):
        if not path.startswith(("/api/v1/router/views", "/api/v1/router/homes")):
            edges |= {path + "/", path + "/extra", path + "//"}
    corpus = sorted(routes | edges)
    preexisting = {p for p in routes if p.startswith("/api/v1/router/")} - set(
        VIEW_GRANTS
    )
    assert "/api/v1/router/requests/x" in preexisting
    assert "/api/v1/router/instances/x/runs/x" in preexisting
    assert set(VIEW_GRANTS) <= routes  # every view grant is a real route
    for path in corpus:
        for method in METHODS:
            if method == "GET" and path in VIEW_GRANTS:
                assert api.query_allowlist(path, method) == frozenset(
                    VIEW_GRANTS[path]
                ), path
                continue
            for keys in KEY_SAMPLES:
                assert table_refuses(path, method, keys) == legacy_refuses(
                    path, method, keys
                ), (path, method, keys)
    for path in ("/api/v1/providers", "/api/v1/actions"):
        assert api.query_allowlist(path, "GET") is None  # PageQuery validates


def test_middleware_applies_the_table_before_any_view_handler(tmp_path, monkeypatch):
    service, client, bearer = setup(tmp_path)
    service.router_client.homes = {"default": tmp_path / "core"}
    monkeypatch.setattr(service.router_client, "_control", forbidden)
    monkeypatch.setattr(service.router_client, "list_homes", forbidden)
    monkeypatch.setattr(service.router_workers, "observe", forbidden)
    for route in (
        "/api/v1/router/homes?home=default",
        "/api/v1/router/homes?limit=1&limit=2",
        "/api/v1/router/homes?cursor=x&extra=1",
        VIEWS + "?home=default&home=default",
        VIEWS + "?home=default&section=summary",
        f"{VIEWS}/{INSTANCE}?home=default&request_id={REQUEST}",
        f"{VIEWS}/{INSTANCE}/runs/{RUN}?home=default&state_home=/tmp/core",
        f"{VIEWS}/{INSTANCE}/runs/{RUN}?home=default&limit=1&limit=1",
        f"{VIEWS}/{INSTANCE}/runs/{RUN}/revisions/{REVISION}?home=default&limit=1",
        f"{STRICT}{REQUEST}?home=default&instance_id={INSTANCE}&section=controls",
        f"{STRICT}{REQUEST}?home=default&instance_id={INSTANCE}&run_id=x&run_id=y",
    ):
        response = client.get(route, headers=bearer)
        assert response.status_code == 422, (route, response.text)
        assert response.json()["code"] == "invalid_request"
    response = client.post(
        "/api/v1/router/views/validate?home=default",
        headers=bearer,
        json={"home": "default", "instance_id": INSTANCE},
    )
    assert response.status_code == 422
    assert not (tmp_path / "core").exists()


def test_new_routes_require_authentication(tmp_path, monkeypatch):
    service, client, _ = setup(tmp_path)
    monkeypatch.setattr(service.router_client, "_control", forbidden)
    monkeypatch.setattr(service.router_client, "list_homes", forbidden)
    for route in (
        "/api/v1/router/homes",
        VIEWS + "?home=default",
        f"{VIEWS}/{INSTANCE}?home=default",
        f"{VIEWS}/{INSTANCE}/runs/{RUN}?home=default",
        f"{VIEWS}/{INSTANCE}/runs/{RUN}/revisions/{REVISION}?home=default",
        f"{STRICT}{REQUEST}?home=default&instance_id={INSTANCE}",
    ):
        assert client.get(route).status_code == 401, route
    response = client.post(
        "/api/v1/router/views/validate",
        json={"home": "default", "instance_id": INSTANCE},
    )
    assert response.status_code == 401


def test_homes_page_configured_names_with_membership_bound_cursor(
    tmp_path, monkeypatch
):
    service, client, bearer = setup(tmp_path)
    names = [f"home-{index:03d}" for index in range(120)]
    service.router_client.homes = {
        name: tmp_path / "absent" / name for name in reversed(names)
    }
    monkeypatch.setattr(service.router_client, "_control", forbidden)

    def page(**query):
        response = client.get("/api/v1/router/homes", params=query, headers=bearer)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {
            "contract",
            "authority",
            "observed_at",
            "items",
            "next_cursor",
        }
        assert body["contract"] == "millrace-os-router-homes/v1"
        assert body["authority"] == "os-local"
        assert all(set(item) == {"name"} for item in body["items"])
        return [item["name"] for item in body["items"]], body["next_cursor"]

    seen, cursor, pages = [], None, 0
    while True:
        items, cursor = page(**({"cursor": cursor} if cursor else {}))
        seen += items
        pages += 1
        if cursor is None:
            break
        assert re.fullmatch(r"h[0-9a-f]{32}_[1-9][0-9]*", cursor)
        assert len(cursor) <= 128
    assert seen == names and pages == 3
    first, following = page(limit="2")
    assert first == names[:2]
    assert page(limit="2", cursor=following) == (names[2:4], following[:-1] + "4")
    prefix = following.rsplit("_", 1)[0]
    for invalid in (
        "h" + "0" * 32 + "_2",
        prefix + "_0",
        prefix + "_02",
        prefix + "_120",
        prefix + "_999",
        prefix,
        "x",
    ):
        response = client.get(
            "/api/v1/router/homes", params={"cursor": invalid}, headers=bearer
        )
        assert response.status_code == 422, invalid
        assert response.json()["code"] == "invalid_request"
    for limit in ("0", "51", "1.0", "+1", "-1", " 1", "true"):
        response = client.get(
            "/api/v1/router/homes", params={"limit": limit}, headers=bearer
        )
        assert response.status_code == 422, limit
    # A different membership refuses the old cursor instead of drifting.
    service.router_client.homes["home-zzz"] = tmp_path / "absent" / "zzz"
    response = client.get(
        "/api/v1/router/homes", params={"cursor": following}, headers=bearer
    )
    assert response.status_code == 422
    service.router_client.homes = {}
    assert page() == ([], None)
    assert not (tmp_path / "absent").exists()


def test_views_forward_exact_core_arguments_in_a_compact_view_envelope(
    tmp_path, monkeypatch
):
    calls = []
    result = {"format": MARKER, "kind": "instance", "label": "é", "items": []}
    service, client, bearer, home = views_service(
        tmp_path, monkeypatch, facade(calls, result=result)
    )
    run = f"{VIEWS}/{INSTANCE}/runs/{RUN}"
    page = {"limit": 50, "cursor": None}
    selectors = {**page, "section": "summary", "request_id": None}
    cases = [
        (VIEWS + "?home=default", "list_instances_page", (), page),
        (
            VIEWS + "?home=default&limit=7&cursor=i_x",
            "list_instances_page",
            (),
            {"limit": 7, "cursor": "i_x"},
        ),
        (
            f"{VIEWS}/{INSTANCE}?home=default&limit=3",
            "inspect_instance_page",
            (INSTANCE,),
            {"limit": 3, "cursor": None},
        ),
        (
            run + "?home=default",
            "inspect_run_page",
            (INSTANCE, RUN),
            {**selectors, "invocation_id": None},
        ),
        (
            run + "?home=default&section=invocations&invocation_id=" + "1" * 32,
            "inspect_run_page",
            (INSTANCE, RUN),
            {**selectors, "section": "invocations", "invocation_id": "1" * 32},
        ),
        (
            run + f"?home=default&section=controls&request_id={REQUEST}&limit=2",
            "inspect_run_page",
            (INSTANCE, RUN),
            {
                **selectors,
                "section": "controls",
                "limit": 2,
                "request_id": REQUEST,
                "invocation_id": None,
            },
        ),
        (
            run + "?home=default&section=transitions&cursor=c_9",
            "inspect_run_page",
            (INSTANCE, RUN),
            {
                **selectors,
                "section": "transitions",
                "cursor": "c_9",
                "invocation_id": None,
            },
        ),
        (
            f"{run}/revisions/{REVISION}?home=default",
            "inspect_revision_graph",
            (INSTANCE, RUN, REVISION),
            {},
        ),
    ]
    for route, name, args, kwargs in cases:
        calls.clear()
        response = client.get(route, headers=bearer)
        assert response.status_code == 200, (route, response.text)
        assert calls == [(name, args, {"state_home": home, **kwargs})]
        body = response.json()
        assert set(body) == VIEW_KEYS
        assert body["contract"] == "millrace-os-router-view/v1"
        assert (body["home"], body["data"]) == ("default", result)
        assert body["source_provider"] == "core"
        assert body["authority"] == "core-state-home"
        assert response.headers["content-type"] == "application/json"
        compact = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        assert response.content == compact.encode()
    for body, run_id in (
        ({"home": "default", "instance_id": INSTANCE}, None),
        ({"home": "default", "instance_id": INSTANCE, "run_id": RUN}, RUN),
        ({"home": "default", "instance_id": INSTANCE, "run_id": None}, None),
    ):
        calls.clear()
        response = client.post(
            "/api/v1/router/views/validate", json=body, headers=bearer
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == result
        assert calls == [
            (
                "validate_revision_summary",
                (INSTANCE,),
                {"state_home": home, "run_id": run_id},
            )
        ]
    assert not home.exists()


def test_view_shapes_refuse_before_core(tmp_path, monkeypatch):
    calls = []
    service, client, bearer, home = views_service(
        tmp_path, monkeypatch, facade(calls)
    )
    run = f"{VIEWS}/{INSTANCE}/runs/{RUN}"
    for route in (
        VIEWS + "?home=",
        VIEWS + "?home=bad/name",
        VIEWS + "?home=default&limit=0",
        VIEWS + "?home=default&limit=51",
        VIEWS + "?home=default&limit=1.0",
        VIEWS + "?home=default&cursor=",
        VIEWS + "?home=default&cursor=a!b",
        VIEWS + "?home=default&cursor=" + "a" * 129,
        f"{VIEWS}/{INSTANCE}",
        run + "?home=default&section=history",
        run + "?home=default&request_id=bad/id",
        f"{run}/revisions/{'E' * 64}?home=default",
        f"{run}/revisions/{'e' * 63}?home=default",
        f"{STRICT}{REQUEST}?home=default",
        f"{STRICT}{REQUEST}?home=default&run_id={RUN}",
    ):
        response = client.get(route, headers=bearer)
        assert response.status_code == 422, (route, response.text)
        assert response.json()["code"] == "invalid_request"
    for body in (
        {"home": "default"},
        {"home": "default", "instance_id": INSTANCE, "draft_id": "d"},
        {"home": "default", "instance_id": 7},
    ):
        response = client.post(
            "/api/v1/router/views/validate", json=body, headers=bearer
        )
        assert response.status_code == 422, body
    unknown = client.get(VIEWS + "?home=unknown", headers=bearer)
    assert unknown.status_code == 422 and unknown.json()["code"] == (
        "unknown_router_home"
    )
    assert calls == [] and not home.exists()


@pytest.mark.parametrize("variant", ["import", "absent", "old", "operation"])
def test_missing_inspection_capability_is_a_typed_503(
    tmp_path, monkeypatch, variant
):
    calls = []
    module = facade(
        calls,
        marker={"absent": None, "old": "millrace-router-inspection/v0"}.get(
            variant, MARKER
        ),
    )
    if variant == "operation":
        # Plus raises CoreContractError, an ImportError, for a missing operation.
        def missing(*args, **kwargs):
            raise ImportError("core_contract_missing")

        for name in FORWARDS:
            setattr(module, name, missing)
    service, client, bearer, home = views_service(tmp_path, monkeypatch, module)
    if variant == "import":

        def unavailable():
            raise RouterUnavailable("router_unavailable")

        monkeypatch.setattr(service.router_client, "_control", unavailable)
    monkeypatch.setattr(service.router_client, "inspect_run", forbidden)
    monkeypatch.setattr(service.router_client, "inspect_instance", forbidden)
    run = f"{VIEWS}/{INSTANCE}/runs/{RUN}"
    for route in (
        VIEWS + "?home=default",
        f"{VIEWS}/{INSTANCE}?home=default",
        run + "?home=default",
        f"{run}/revisions/{REVISION}?home=default",
        f"{STRICT}{REQUEST}?home=default&instance_id={INSTANCE}",
    ):
        response = client.get(route, headers=bearer)
        assert response.status_code == 503, (route, response.text)
        assert response.json()["code"] == "router_inspection_unavailable"
        assert response.json()["retryable"] is False
    response = client.post(
        "/api/v1/router/views/validate",
        json={"home": "default", "instance_id": INSTANCE},
        headers=bearer,
    )
    assert response.status_code == 503
    assert response.json()["code"] == "router_inspection_unavailable"
    assert [name for name, *_ in calls] == []
    if variant != "import":
        # Ordinary facade operations still work against the older capability.
        legacy = client.get("/api/v1/router/instances?home=default", headers=bearer)
        assert legacy.status_code == 200, legacy.text
        assert [name for name, *_ in calls] == ["list_instances"]


def test_construction_and_homes_import_no_optional_module(tmp_path, monkeypatch):
    original = importlib.import_module
    optional = []

    def refuse_optional(name, *args, **kwargs):
        if name == "millrace_plus" or name.startswith(("millrace_plus.", "millrace.")):
            optional.append(name)
            raise ImportError("optional router component unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", refuse_optional)
    service, client, bearer = setup(tmp_path)
    service.router_client.homes = {"main": tmp_path / "core"}
    response = client.get("/api/v1/router/homes", headers=bearer)
    assert response.status_code == 200
    assert response.json()["items"] == [{"name": "main"}]
    assert optional == []
    response = client.get(VIEWS + "?home=main", headers=bearer)
    assert response.status_code == 503
    assert response.json()["code"] == "router_inspection_unavailable"
    assert optional == ["millrace_plus.router"]
    assert not (tmp_path / "core").exists()


def test_exported_view_limits_use_standard_json_schema_bounds(tmp_path):
    service, _, _ = setup(tmp_path)
    document = json.loads(api.export_artifacts(service.app)["openapi.json"])
    for route in (
        "/api/v1/router/homes",
        VIEWS,
        VIEWS + "/{instance_id}",
        VIEWS + "/{instance_id}/runs/{run_id}",
    ):
        (limit,) = [
            parameter
            for parameter in document["paths"][route]["get"]["parameters"]
            if parameter["name"] == "limit"
        ]
        assert (limit["in"], limit["required"]) == ("query", False)
        assert limit["schema"] == {
            "default": 50,
            "maximum": 50,
            "minimum": 1,
            "title": "Limit",
            "type": "integer",
        }, route

    def nonstandard(value):
        if isinstance(value, dict):
            return any(
                (key in {"ge", "le", "gt", "lt"} and not isinstance(item, dict))
                or nonstandard(item)
                for key, item in value.items()
            )
        return isinstance(value, list) and any(nonstandard(item) for item in value)

    assert not nonstandard(document)


def test_response_bound_counts_compact_utf8_bytes(tmp_path, monkeypatch):
    calls = []
    module = facade(calls)
    service, client, bearer, _ = views_service(tmp_path, monkeypatch, module)
    for blob, status in (
        ("x" * (api.RESPONSE_LIMIT - 1024), 200),
        ("x" * api.RESPONSE_LIMIT, 409),
        # Under the limit in characters, over it in UTF-8 octets.
        ("é" * (api.RESPONSE_LIMIT // 2 + 1), 409),
    ):
        module.list_instances_page = lambda *a, _blob=blob, **k: {"blob": _blob}
        response = client.get(VIEWS + "?home=default", headers=bearer)
        assert response.status_code == status
        if status == 409:
            assert response.json()["code"] == "response_too_large"
            assert len(response.content) < 1024
        else:
            assert len(response.content) <= api.RESPONSE_LIMIT
            assert response.json()["data"] == {"blob": blob}
    assert api.RESPONSE_LIMIT == 1_049_600


def test_views_share_the_existing_eight_read_slots(tmp_path, monkeypatch):
    calls = []
    module = facade(calls)
    service, client, bearer, _ = views_service(tmp_path, monkeypatch, module)
    entered, release = threading.Semaphore(0), threading.Event()

    def blocking(*args, **kwargs):
        entered.release()
        assert release.wait(10)
        return {"format": MARKER}

    module.list_instances_page = blocking
    # Build the middleware stack once before concurrent first requests.
    assert client.get("/api/v1/router/homes", headers=bearer).status_code == 200
    statuses = []

    def read():
        own = TestClient(service.app, base_url="http://127.0.0.1:8731")
        statuses.append(own.get(VIEWS + "?home=default", headers=bearer).status_code)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(8):
            assert entered.acquire(timeout=10)
        busy = client.get(f"{VIEWS}/{INSTANCE}?home=default", headers=bearer)
        assert busy.status_code == 429
        assert busy.json()["code"] == "router_read_busy"
        assert busy.json()["retryable"] is True
        assert [name for name, *_ in calls] == []
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
    assert statuses == [200] * 8


def strict_service(tmp_path, monkeypatch, outcome):
    """Strict observation with a recorded bounded Core lookup."""
    service, client, bearer = setup(tmp_path)
    service.router_client.homes = {"default": tmp_path / "core"}
    seen = []

    def receipt(home, request_id, *, instance_id=None, run_id=None, bounded=False):
        seen.append((home, request_id, instance_id, run_id, bounded))
        if isinstance(outcome, Exception):
            raise outcome
        return copy.deepcopy(outcome)

    monkeypatch.setattr(service.router_client, "receipt", receipt)
    for name in ("inspect_run", "inspect_instance", "execute_request", "_control"):
        monkeypatch.setattr(service.router_client, name, forbidden)
    return service, client, bearer, seen


def owned(service, *, instance_id=INSTANCE, run_id=None, cached=None, started=False):
    """Register one exact owned call without a worker thread (never joined)."""
    request = {
        "format": "millrace-router-request/v2",
        "operation": "start_run",
        "instance_id": instance_id,
        "run_id": None,
        "plane_id": None,
        "request_id": REQUEST,
        "expected_state_version": 1,
        "payload": {"edit_mode": "locked"},
    }
    released = []
    call = OwnedCall(
        "default",
        request,
        request_digest(request),
        (str(service.router_client.state_home("default")), instance_id),
        "foreground",
        receipt=copy.deepcopy(cached),
        started=started,
        release_admission=lambda: released.append(True),
        instance_id=instance_id,
        run_id=run_id,
    )
    workers = service.router_workers
    workers._calls[workers._key("default", REQUEST)] = call
    return call, released


def receipt_for(digest, *, status="applied", instance_id=INSTANCE, run_id=RUN):
    return {
        "format": "millrace-router-receipt/v2",
        "request_id": REQUEST,
        "request_digest": digest,
        "operation": "start_run",
        "status": status,
        "instance_id": instance_id,
        "run_id": run_id,
    }


def strict_get(client, bearer, *, instance_id=INSTANCE, run_id=None, accept=None):
    params = {"home": "default", "instance_id": instance_id}
    if run_id is not None:
        params["run_id"] = run_id
    headers = {**bearer, **({"Accept": accept} if accept else {})}
    return client.get(STRICT + REQUEST, params=params, headers=headers)


def test_strict_observation_without_owned_call_reports_only_core(
    tmp_path, monkeypatch
):
    service, client, bearer, seen = strict_service(tmp_path, monkeypatch, None)
    response = strict_get(client, bearer, run_id=RUN)
    assert response.status_code == 404
    assert response.json()["code"] == "observation_unavailable"
    assert seen == [("default", REQUEST, INSTANCE, RUN, True)]
    durable = receipt_for("1" * 64)
    service, client, bearer, seen = strict_service(
        tmp_path / "found", monkeypatch, durable
    )
    response = strict_get(client, bearer)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "contract": "millrace-os-router-call/v1",
        "home": "default",
        "request_id": REQUEST,
        "request_digest": "1" * 64,
        "instance_id": INSTANCE,
        "run_id": RUN,
        "source": "core_receipt",
        "phase": "receipt_observed",
        "core_receipt": durable,
        "observed_at": response.json()["observed_at"],
    }
    v2 = strict_get(client, bearer, accept=V2)
    assert v2.headers["content-type"] == V2
    assert v2.json()["contract"] == "millrace-os-router-call/v2"
    assert v2.json()["holder_joined"] is None
    refused = strict_get(
        client, bearer, accept="application/vnd.millrace.router-call.v3+json"
    )
    assert refused.status_code == 406
    assert seen == [("default", REQUEST, INSTANCE, None, True)] * 2


def test_strict_budget_refusal_precedes_any_cached_owned_observation(
    tmp_path, monkeypatch
):
    error = RouterUnavailable("core_inspection_too_large", 409)
    service, client, bearer, seen = strict_service(tmp_path, monkeypatch, error)
    call, released = owned(service, cached=receipt_for("2" * 64), started=True)
    call.receipt["request_digest"] = call.digest
    snapshot = copy.deepcopy(call.receipt)
    for accept in (None, V2):
        response = strict_get(client, bearer, accept=accept)
        assert response.status_code == 409
        assert response.json()["code"] == "core_inspection_too_large"
        assert "core_receipt" not in response.json()
    assert len(seen) == 2 and all(entry[-1] is True for entry in seen)
    assert call.receipt == snapshot and released == []


def test_strict_owned_call_reports_exact_phase_and_only_confirmed_receipt(
    tmp_path, monkeypatch
):
    # Empty strict lookup: an admission_pending owned call keeps a null receipt.
    service, client, bearer, seen = strict_service(tmp_path, monkeypatch, None)
    call, released = owned(service)
    response = strict_get(client, bearer)
    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["source"], body["phase"]) == ("os_owned_call", "admission_pending")
    assert body["core_receipt"] is None
    assert body["request_digest"] == call.digest
    # A cached receipt is not reported when the strict lookup finds no row.
    call.started = True
    call.receipt = receipt_for(call.digest)
    response = strict_get(client, bearer)
    assert response.status_code == 202
    assert response.json()["phase"] == "call_active"
    assert response.json()["core_receipt"] is None
    assert released == []
    # A durable row with this call's digest is the only reported receipt.
    durable = receipt_for(call.digest, run_id=RUN)
    service, client, bearer, seen = strict_service(
        tmp_path / "confirmed", monkeypatch, durable
    )
    call, released = owned(service, started=True)
    response = strict_get(client, bearer, accept=V2)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["core_receipt"] == durable
    assert body["source"] == "os_owned_call" and body["holder_joined"] is False
    # Strict observation never updates, releases or refreshes the owned call.
    assert call.receipt is None and call.run_id is None and released == []
    # A same-ID durable row with another digest is not this call's receipt.
    service, client, bearer, seen = strict_service(
        tmp_path / "foreign", monkeypatch, receipt_for("3" * 64)
    )
    call, released = owned(service, started=True)
    response = strict_get(client, bearer)
    assert response.status_code == 202
    assert response.json()["core_receipt"] is None


def test_strict_owned_call_target_association(tmp_path, monkeypatch):
    service, client, bearer, seen = strict_service(tmp_path, monkeypatch, None)
    owned(service, run_id=RUN)
    for instance_id, run_id in ((OTHER, None), (INSTANCE, RUN2)):
        response = strict_get(client, bearer, instance_id=instance_id, run_id=run_id)
        assert response.status_code == 409
        assert response.json()["code"] == "observation_target_mismatch"
    assert strict_get(client, bearer, run_id=RUN).status_code == 202
    # The strict lookup ran first for every target, including refused ones.
    assert [entry[2:4] for entry in seen] == [
        (OTHER, None),
        (INSTANCE, RUN2),
        (INSTANCE, RUN),
    ]
    # A queued Start has not learned its run; only a Core row with its digest
    # can associate a supplied run, never a guess.
    service, client, bearer, seen = strict_service(
        tmp_path / "queued", monkeypatch, None
    )
    call, _ = owned(service, started=True)
    response = strict_get(client, bearer, run_id=RUN)
    assert response.status_code == 409
    assert response.json()["code"] == "observation_target_mismatch"
    queued = receipt_for(call.digest, status="queued", run_id=None)
    monkeypatch.setattr(
        service.router_client, "receipt", lambda *a, **k: copy.deepcopy(queued)
    )
    response = strict_get(client, bearer, run_id=RUN)
    assert response.status_code == 202
    assert response.json()["core_receipt"] == queued
    assert response.json()["run_id"] is None


def test_strict_response_is_bounded(tmp_path, monkeypatch):
    durable = receipt_for("4" * 64)
    durable["detail"] = "x" * api.RESPONSE_LIMIT
    service, client, bearer, seen = strict_service(tmp_path, monkeypatch, durable)
    response = strict_get(client, bearer)
    assert response.status_code == 409
    assert response.json()["code"] == "response_too_large"


def test_default_observation_never_selects_the_bounded_reader(tmp_path, monkeypatch):
    from millrace_os.router_workers import RouterWorkers

    service, client, bearer = setup(tmp_path)
    keywords = []

    def observe(*args, **kwargs):
        keywords.append(kwargs)
        raise RouterUnavailable("observation_unavailable", 404)

    monkeypatch.setattr(service.router_workers, "observe", observe)
    response = client.get(
        f"/api/v1/router/requests/{REQUEST}?home=default&instance_id={INSTANCE}",
        headers=bearer,
    )
    assert response.status_code == 404
    assert keywords and all("bounded" not in entry for entry in keywords)

    class LegacyClient:
        """A custom client whose receipt predates the bounded keyword."""

        homes = {"default": tmp_path / "core"}

        def state_home(self, home):
            return self.homes[home]

        def receipt(self, home, request_id, *, instance_id=None, run_id=None):
            return receipt_for("5" * 64)

    workers = RouterWorkers(LegacyClient())
    observation = workers.observe("default", REQUEST, instance_id=INSTANCE)
    assert observation.source == "core_receipt"
    with pytest.raises(TypeError):
        workers.observe("default", REQUEST, instance_id=INSTANCE, bounded=True)


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
    assert main(["router", *argv, *config]) == 0
    return captured[-1]


def test_cli_peers_select_exact_routes(tmp_path, monkeypatch):
    base = "http://127.0.0.1:8731/api/v1/router"
    run = f"{base}/views/instances/i/runs/r"
    for argv, url in (
        (("homes",), base + "/homes"),
        (
            ("homes", "--limit", "2", "--cursor", "h_2"),
            base + "/homes?limit=2&cursor=h_2",
        ),
        (("views", "--home", "h"), base + "/views/instances?home=h"),
        (
            ("views", "--home", "h", "--instance", "i", "--limit", "3"),
            base + "/views/instances/i?home=h&limit=3",
        ),
        (
            ("views", "--home", "h", "--instance", "i", "--run", "r"),
            run + "?home=h",
        ),
        (
            (
                "views", "--home", "h", "--instance", "i", "--run", "r",
                "--section", "controls", "--request-id", "q", "--limit", "1",
            ),
            run + "?home=h&section=controls&request_id=q&limit=1",
        ),
        (
            (
                "views", "--home", "h", "--instance", "i", "--run", "r",
                "--section", "invocations", "--invocation-id", "v",
            ),
            run + "?home=h&section=invocations&invocation_id=v",
        ),
        (
            (
                "views", "--home", "h", "--instance", "i", "--run", "r",
                "--revision", REVISION,
            ),
            f"{run}/revisions/{REVISION}?home=h",
        ),
        (
            (
                "request", "--home", "h", "--request-id", "q", "--bounded",
                "--instance", "i", "--run", "r",
            ),
            base + "/views/requests/q?home=h&instance_id=i&run_id=r",
        ),
        (
            ("request", "--home", "h", "--request-id", "q", "--instance", "i"),
            base + "/requests/q?home=h&instance_id=i",
        ),
    ):
        request = run_cli(monkeypatch, tmp_path, *argv)
        assert (request.full_url, request.get_method()) == (url, "GET"), argv
        assert request.data is None
    for argv, url, body in (
        (
            ("validate", "--home", "h", "--instance", "i", "--summary"),
            base + "/views/validate",
            {"home": "h", "instance_id": "i", "run_id": None},
        ),
        (
            ("validate", "--home", "h", "--instance", "i", "--run", "r"),
            base + "/validate",
            {"home": "h", "instance_id": "i", "run_id": "r", "draft_id": None},
        ),
    ):
        request = run_cli(monkeypatch, tmp_path, *argv)
        assert (request.full_url, request.get_method()) == (url, "POST")
        assert json.loads(request.data) == body
    saved = tmp_path / "request.json"
    saved.write_text("{}")
    for argv in (
        ("views", "--home", "h", "--run", "r"),
        ("views", "--home", "h", "--instance", "i", "--revision", REVISION),
        ("validate", "--home", "h", "--instance", "i", "--summary", "--draft", "d"),
        ("request", "--home", "h", "--file", str(saved), "--bounded"),
    ):
        with pytest.raises(SystemExit) as exit_:
            run_cli(monkeypatch, tmp_path, *argv)
        assert exit_.value.code == 2, argv
