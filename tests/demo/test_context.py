"""Fixed Finish route trust/input contracts and immutable owner context."""

import json

import pytest
from fastapi.testclient import TestClient

from millrace_os.config import ApplicationConfig
from millrace_os.demo_service import DemoServiceError, _ChildOwner
from millrace_os.service import Service, ServiceDependencies

INSTANCE, RUN = "a" * 32, "b" * 32


def service_client(tmp_path, *, finish=None, reader=None):
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credential",
    )
    service = Service(
        config,
        dependencies=ServiceDependencies(
            demo_context_reader=reader, demo_finish_request=finish
        ),
    )
    service.trust.reset()
    service._state = object()
    service._lifecycle = "running"
    client = TestClient(service.app, base_url="http://127.0.0.1:8731")
    return service, client, {"Authorization": "Bearer " + service.trust.store.ensure()}


def test_ordinary_finish_route_refuses_after_normal_input_validation(tmp_path):
    _, client, bearer = service_client(tmp_path)
    assert client.post("/api/v1/demo/finish", json={}).status_code == 401
    null = client.post(
        "/api/v1/demo/finish",
        content="null",
        headers={**bearer, "Content-Type": "application/json"},
    )
    assert null.status_code == 422
    for body in ([], {"cancel": True}):
        response = client.post("/api/v1/demo/finish", json=body, headers=bearer)
        assert response.status_code == 422
    assert (
        client.post("/api/v1/demo/finish?extra=1", json={}, headers=bearer).status_code
        == 422
    )
    response = client.post("/api/v1/demo/finish", json={}, headers=bearer)
    assert response.status_code == 404
    assert response.json()["code"] == "demo_service_unavailable"


def test_owned_finish_cookie_csrf_origin_and_sanitized_failure(tmp_path):
    intents = []
    _, client, bearer = service_client(tmp_path, finish=lambda: intents.append(True))
    nonce = client.post(
        "/api/v1/auth/bootstrap", headers=bearer, json={"recovery": False}
    ).json()["nonce"]
    csrf = client.post(
        "/api/v1/auth/exchange",
        headers={"Origin": str(client.base_url).rstrip("/")},
        json={"nonce": nonce, "recovery": False},
    ).json()["csrf"]
    assert client.post("/api/v1/demo/finish", json={}).status_code == 403
    assert not intents
    response = client.post(
        "/api/v1/demo/finish",
        json={},
        headers={"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200 and response.json()["requested"] is True
    assert intents == [True]


def test_finish_callback_failure_does_not_claim_success(tmp_path):
    def failure():
        raise RuntimeError("private credential and file")

    _, client, bearer = service_client(tmp_path, finish=failure)
    response = client.post("/api/v1/demo/finish", json={}, headers=bearer)
    assert response.status_code == 503
    assert response.json()["code"] == "demo_service_failed"
    assert "credential" not in response.text


def test_output_context_preserves_accepted_pins_and_target(tmp_path):
    owner = _ChildOwner(tmp_path, -1)
    owner.command(
        {"kind": "bind", "id": "one", "instance_id": INSTANCE, "run_id": RUN}, None
    )
    pin = {
        "reference": {
            "instance_id": INSTANCE,
            "run_id": RUN,
            "output_id": "baseline",
            "record_sha256": "c" * 64,
        },
        "report_sha256": "d" * 64,
        "json_sha256": "e" * 64,
    }
    message = {
        "kind": "outputs",
        "id": "two",
        "accepted_output_refs": [pin],
        "output_error": None,
    }
    owner.command(message, None)
    assert owner.read_context()["accepted_output_refs"] == [pin]
    for changed in ([{**pin, "report_sha256": "f" * 64}], [], [pin, pin]):
        with pytest.raises(DemoServiceError):
            owner.command({**message, "accepted_output_refs": changed}, None)
    assert owner.read_context()["accepted_output_refs"] == [pin]
    with pytest.raises(DemoServiceError):
        owner.command(
            {"kind": "bind", "id": "three", "instance_id": "f" * 32, "run_id": RUN},
            None,
        )
    assert json.dumps(owner.read_context()).find("owner_token") == -1
