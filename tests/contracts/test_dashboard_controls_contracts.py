"""UI3 control boundary: a Plus catalog refusal keeps its public machine code.

Plus guards catalog ownership before it delegates a request to Core. Its public
`CatalogError` code reaches HTTP in the established `catalog_<code>` form (409),
with no exception text or path and no fabricated Core receipt. Other failures
keep their existing mapping. The two public exception modules are stand-ins,
so no installed Core or Plus is needed.
"""

from __future__ import annotations

import sys
import types

import pytest
from test_auth import setup

INSTANCE, RUN, REQUEST = "a" * 32, "b" * 32, "c" * 32


class RouterError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("private Core message")
        self.code = code


class CatalogError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def control_service(tmp_path, monkeypatch, failure):
    """A service whose Plus control facade fails `execute_request` with `failure`."""
    execution = types.ModuleType("millrace.router.execution")
    execution.RouterError = RouterError
    catalog = types.ModuleType("millrace_plus.catalog")
    catalog.CatalogError = CatalogError
    monkeypatch.setitem(sys.modules, "millrace.router.execution", execution)
    monkeypatch.setitem(sys.modules, "millrace_plus.catalog", catalog)
    executed = []

    def execute_request(request, *, state_home, on_started=None):
        executed.append(request["request_id"])
        raise failure

    module = types.SimpleNamespace(
        execute_request=execute_request,
        # The legacy receipt watcher finds no recorded control row.
        inspect_run=lambda *args, **kwargs: {"controls": []},
        inspect_instance=lambda *args, **kwargs: {"controls": []},
    )
    service, client, bearer = setup(tmp_path)
    service.router_client.homes = {"default": tmp_path / "core"}
    monkeypatch.setattr(service.router_client, "_control", lambda: module)
    return service, client, bearer, executed


def pause_request() -> dict:
    return {
        "format": "millrace-router-request/v2",
        "operation": "pause_run",
        "instance_id": INSTANCE,
        "run_id": RUN,
        "plane_id": None,
        "request_id": REQUEST,
        "expected_state_version": None,
        "payload": {},
    }


@pytest.mark.parametrize(
    ("code", "public"),
    [
        ("catalog_ownership", "catalog_catalog_ownership"),
        ("io_error", "catalog_io_error"),
        ("invalid_catalog_settings", "catalog_invalid_catalog_settings"),
    ],
)
def test_plus_catalog_refusal_keeps_its_code_without_text_or_receipt(
    tmp_path, monkeypatch, code, public
):
    private = f"refused at {tmp_path}/private-project secret"
    service, client, bearer, executed = control_service(
        tmp_path, monkeypatch, CatalogError(code, private)
    )
    response = client.post(
        "/api/v1/router/requests",
        json={"home": "default", "request": pause_request()},
        headers=bearer,
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["contract"] == "os.error.v1"
    assert body["code"] == public  # the established spelling, unchanged
    assert body["correlation"] == REQUEST
    assert "secret" not in response.text and str(tmp_path) not in response.text
    assert executed == [REQUEST]  # the facade refused once; nothing else ran

    # The owned call reports its failure; no Core receipt is invented.
    observed = client.get(
        f"/api/v1/router/requests/{REQUEST}",
        params={"home": "default", "instance_id": INSTANCE, "run_id": RUN},
        headers=bearer,
    )
    assert observed.status_code == 200, observed.text
    observation = observed.json()
    assert observation["phase"] == "call_failed"
    assert observation["core_receipt"] is None
    assert observation["source"] == "os_owned_call"


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (RouterError("stale_state_version"), 409, "core_stale_state_version"),
        (RuntimeError("private secret"), 503, "router_call_failed"),
    ],
)
def test_other_failures_keep_their_existing_mapping(
    tmp_path, monkeypatch, failure, status, code
):
    service, client, bearer, executed = control_service(tmp_path, monkeypatch, failure)
    response = client.post(
        "/api/v1/router/requests",
        json={"home": "default", "request": pause_request()},
        headers=bearer,
    )
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert "secret" not in response.text and "private" not in response.text
    assert executed == [REQUEST]
