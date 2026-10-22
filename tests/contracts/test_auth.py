import json

import pytest
from fastapi.testclient import TestClient

from millrace_os.auth import COOKIE, MAX_NONCES, MAX_SESSIONS
from millrace_os.config import ApplicationConfig
from millrace_os.credentials import CredentialStore
from millrace_os.service import Service


def setup(tmp_path, damaged=False):
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credentials.json",
    )
    config.ensure_directories()
    if damaged:
        config.database_path.write_bytes(b"private-file-path-and-corrupt-sqlite")
    service = Service(config)
    service.trust.reset()
    if damaged:
        from millrace_os.recovery.runtime import RecoveryRuntime

        service._recovery = RecoveryRuntime("corrupt_state", "PRIVATE secret path")
    else:
        service._state = object()
    service._lifecycle = "running"
    client = TestClient(service.app, base_url="http://127.0.0.1:8731")
    bearer = {
        "Authorization": "Bearer " + CredentialStore(config.credential_path).ensure()
    }
    return service, client, bearer


def bootstrap(client, bearer, recovery=False):
    nonce = client.post(
        "/api/v1/auth/bootstrap", headers=bearer, json={"recovery": recovery}
    ).json()["nonce"]
    response = client.post(
        "/api/v1/auth/exchange",
        headers={"Origin": str(client.base_url).rstrip("/")},
        json={"nonce": nonce, "recovery": recovery},
    )
    assert response.status_code == 200, response.text
    return nonce, response.json()["csrf"]


def test_all_reads_require_auth(tmp_path):
    service, client, bearer = setup(tmp_path)
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    for route in (
        "version",
        "diagnostics",
        "contracts",
        "actions",
        "providers",
        "events",
        "identity",
        "recovery/diagnostics",
    ):
        assert client.get("/api/v1/" + route).status_code == 401
        assert client.get("/api/v1/" + route, headers=bearer).status_code == 200
    assert (
        client.get(
            "/api/v1/version", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_browser_nonce_scope_replay_origin_cookie_and_rotation(tmp_path):
    service, client, bearer = setup(tmp_path)
    nonce, csrf = bootstrap(client, bearer)
    cookie = client.cookies[COOKIE]
    assert client.get("/api/v1/identity").json()["data"]["authentication"] == "session"
    exchange = {"nonce": nonce, "recovery": False}
    assert client.post("/api/v1/auth/exchange", json=exchange).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/exchange",
            json=exchange,
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/auth/exchange",
            json=exchange,
            headers={"Origin": "http://127.0.0.1:8731"},
        ).status_code
        == 401
    )
    route = "/api/v1/actions/run.pause:invoke"
    body = {"target_id": "run-1"}
    assert client.post(route, json=body).json()["code"] == "wrong_origin"
    assert (
        client.post(
            route, json=body, headers={"Origin": "http://127.0.0.1:8731"}
        ).json()["code"]
        == "invalid_csrf"
    )
    headers = {
        "Origin": "http://127.0.0.1:8731",
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "id-1",
        "X-Correlation-Id": "corr-1",
    }
    assert (
        client.post(route, json=body, headers=headers).json()["code"]
        == "action_unavailable"
    )
    pending = client.post("/api/v1/auth/bootstrap", json={}, headers=bearer).json()[
        "nonce"
    ]
    assert (
        client.post("/api/v1/auth/rotate", json={}, headers=headers).status_code == 403
    )
    assert (
        client.post("/api/v1/auth/rotate", json={}, headers=bearer).status_code == 200
    )
    assert client.get("/api/v1/version", headers=bearer).status_code == 401
    assert client.get("/api/v1/identity").status_code == 401
    assert cookie not in service.trust.sessions
    assert (
        client.post(
            "/api/v1/auth/exchange",
            json={"nonce": pending},
            headers={"Origin": "http://127.0.0.1:8731"},
        ).status_code
        == 401
    )


def test_trust_expiry_capacity_and_scope(tmp_path):
    service, client, bearer = setup(tmp_path)
    now = [1.0]
    service.trust.clock = lambda: now[0]
    nonce = service.trust.mint(True)
    with pytest.raises(Exception):
        service.trust.exchange(nonce, False)
    assert nonce in service.trust.nonces
    now[0] = 62
    with pytest.raises(Exception):
        service.trust.exchange(nonce, True)
    nonce = service.trust.mint(False)
    sid, _ = service.trust.exchange(nonce, False)
    now[0] += 901
    with pytest.raises(Exception):
        service.trust.session(sid)
    for _ in range(MAX_NONCES):
        service.trust.mint(False)
    with pytest.raises(Exception):
        service.trust.mint(False)
    service.trust.reset()
    for _ in range(MAX_SESSIONS):
        service.trust.exchange(service.trust.mint(False), False)
    with pytest.raises(Exception):
        service.trust.exchange(service.trust.mint(False), False)
    service.trust.reset()
    assert not service.trust.sessions and not service.trust.nonces


def test_refusals_never_echo_credentials_and_host_cannot_change_origin(tmp_path):
    service, client, bearer = setup(tmp_path)
    token = service.trust.token
    for headers in (
        {**bearer, "Host": "evil.example"},
        {**bearer, "Origin": "null"},
        {**bearer, "Origin": "http://localhost:8731"},
    ):
        response = client.get("/api/v1/diagnostics", headers=headers)
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers
    response = client.post(
        "/api/v1/auth/bootstrap", headers=bearer, json={"unknown": token}
    )
    assert response.status_code == 422 and token not in response.text
    for route in ("/", "/recovery", "/api/v1/diagnostics"):
        assert token not in client.get(route, headers=bearer).text
    response = client.post(
        "/api/v1/auth/exchange",
        headers={"Origin": "http://127.0.0.1:8731"},
        json={"nonce": "!" * 43},
    )
    assert response.status_code == 422 and "!" not in response.text
    response = client.post(
        "/api/v1/actions/a:invoke",
        content=json.dumps({"x": "s" * 5000}),
        headers={**bearer, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_router_routes_preserve_host_origin_csrf_and_recovery_scope(tmp_path):
    service, client, bearer = setup(tmp_path)
    read = "/api/v1/router/instances?home=default"
    mutate = "/api/v1/router/drafts"
    body = {"home": "default", "files": []}
    assert client.get(read).status_code == 401
    assert client.post(mutate, json=body).status_code == 401
    for headers in ({**bearer, "Host": "evil.example"}, {**bearer, "Origin": "null"}):
        response = client.get(read, headers=headers)
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers
    _, csrf = bootstrap(client, bearer)
    assert client.post(mutate, json=body).json()["code"] == "wrong_origin"
    assert (
        client.post(
            mutate, json=body, headers={"Origin": "http://127.0.0.1:8731"}
        ).json()["code"]
        == "invalid_csrf"
    )
    # A valid session crosses trust, then reaches the configured-home boundary.
    response = client.post(
        mutate,
        json=body,
        headers={"Origin": "http://127.0.0.1:8731", "X-CSRF-Token": csrf},
    )
    assert (
        response.status_code == 422 and response.json()["code"] == "unknown_router_home"
    )
    damaged, recovery, recovery_bearer = setup(tmp_path / "recovery", damaged=True)
    bootstrap(recovery, recovery_bearer, recovery=True)
    blocked = recovery.get(read)
    assert (
        blocked.status_code == 403 and blocked.json()["code"] == "recovery_session_only"
    )
    assert recovery.get("/api/v1/recovery/diagnostics").status_code == 200
