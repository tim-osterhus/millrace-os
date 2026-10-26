from test_auth import setup


def test_typed_bounds_and_refusals(tmp_path):
    service, client, bearer = setup(tmp_path)
    for query in (
        "limit=0",
        "limit=101",
        "limit=1.2",
        "limit=1.0",
        "limit=1e0",
        "limit=+1",
        "limit=%201",
        "extra=x",
        "cursor=",
        "cursor=!",
    ):
        response = client.get("/api/v1/providers?" + query, headers=bearer)
        assert response.status_code == 422, response.text
        assert response.json()["contract"] == "os.error.v1"
    assert (
        client.get("/api/v1/providers?cursor=valid", headers=bearer).status_code == 409
    )
    assert client.get("/api/v1/providers?limit=1", headers=bearer).json()["data"] == {
        "items": [],
        "next_cursor": None,
    }
    for path in ("unknown", "actions/shell:invoke"):
        response = client.post("/api/v1/" + path, headers=bearer, json={})
        assert response.status_code in (404, 422)
    assert (
        client.post(
            "/api/v1/actions/a:invoke", json={"target_id": "bad/id"}, headers=bearer
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/actions/a:invoke",
            json={"target_id": "ok", "extra": 1},
            headers=bearer,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/actions/a:invoke", json={"target_id": "ok"}, headers=bearer
        ).status_code
        == 422
    )
    result = client.post(
        "/api/v1/actions/run.pause:invoke",
        json={"target_id": "ok"},
        headers={**bearer, "Idempotency-Key": "abc", "X-Correlation-Id": "corr"},
    )
    assert result.status_code == 409 and result.json()["code"] == "action_unavailable"


def test_events_are_authenticated_non_durable_bounded_and_explicit(tmp_path):
    service, client, bearer = setup(tmp_path)
    assert client.get("/api/v1/events").status_code == 401
    result = client.get("/api/v1/events", headers=bearer)
    assert result.status_code == 200
    assert result.text.startswith("event: availability\ndata: ")
    assert result.text.endswith("\n\n")
    assert "\\n" not in result.text
    assert '"durable":false' in result.text and "id:" not in result.text
    assert service.event_connections == 0
    for cursor, status in [("valid", 409), ("!", 422), ("", 422), ("x" * 129, 422)]:
        assert (
            client.get(
                "/api/v1/events", headers={**bearer, "Last-Event-ID": cursor}
            ).status_code
            == status
        )
    service.event_connections = 8
    assert client.get("/api/v1/events", headers=bearer).status_code == 429


def test_router_query_and_exact_envelope_boundaries(tmp_path):
    service, client, bearer = setup(tmp_path)
    for query in (
        "home=a&home=b",
        "home=default&extra=x",
        "home=",
        "state_home=/tmp/arbitrary",
    ):
        result = client.get("/api/v1/router/instances?" + query, headers=bearer)
        assert result.status_code == 422, result.text
    for body in (
        {"home": "default", "request": {}, "extra": True},
        {"home": "default", "request": []},
        {"home": "default", "request": {"format": "millrace-router-request/v1"}},
    ):
        result = client.post("/api/v1/router/requests", json=body, headers=bearer)
        assert result.status_code == 422, result.text
    result = client.post(
        "/api/v1/router/requests",
        content=b'{"private":"secret"}',
        headers={**bearer, "Content-Type": "text/plain"},
    )
    assert result.status_code == 415 and "secret" not in result.text


def test_router_absence_is_sanitized_without_breaking_foundation(tmp_path, monkeypatch):
    from millrace_os.router_client import RouterUnavailable

    service, client, bearer = setup(tmp_path)

    def unavailable(*args, **kwargs):
        raise RouterUnavailable("router_unavailable", 503)

    monkeypatch.setattr(service.router_client, "list_instances", unavailable)
    response = client.get("/api/v1/router/instances?home=default", headers=bearer)
    assert (
        response.status_code == 503 and response.json()["code"] == "router_unavailable"
    )
    assert response.json()["provider"] == "os"
    assert "core_receipt" not in response.json()
    assert client.get("/api/v1/version", headers=bearer).status_code == 200
    assert client.get("/api/v1/identity", headers=bearer).status_code == 200
    assert client.get("/api/v1/health").status_code == 200


def test_options_validates_home_without_core_effects(tmp_path, monkeypatch):
    service, client, bearer = setup(tmp_path)
    home = tmp_path / "untouched-core"
    service.router_client.homes = {"default": home}

    def forbidden(*args, **kwargs):
        raise AssertionError("capability discovery must not read or mutate Core")

    monkeypatch.setattr(service.router_client, "_control", forbidden)
    monkeypatch.setattr(service.router_workers, "submit", forbidden)
    route = "/api/v1/router/requests"
    result = client.options(route + "?home=default", headers=bearer)
    assert result.status_code == 200, result.text
    assert result.json() == {
        "contract": "millrace-os-router-observation-capabilities/v1",
        "home": "default",
        "observation_contracts": [
            "millrace-os-router-call/v1", "millrace-os-router-call/v2"
        ],
        "holder_joined_supported": True,
    }
    assert not home.exists()
    for query in (
        "", "home=", "home=unknown", "home=bad/name", "home=default&home=default",
        "home=default&extra=x", "state_home=/tmp/core",
    ):
        response = client.options(
            route + ("?" + query if query else ""), headers=bearer
        )
        assert response.status_code == 422, response.text
    response = client.options(
        "/api/v1/router/validate?home=default", headers=bearer
    )
    assert response.status_code == 422
    assert service.router_workers.snapshot() == []
    assert not home.exists()


def test_call_v2_negotiation_precedes_dispatch(tmp_path, monkeypatch):
    service, client, bearer = setup(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported negotiation reached Core")

    monkeypatch.setattr(service.router_workers, "submit", forbidden)
    monkeypatch.setattr(service.router_workers, "observe", forbidden)
    for accept in (
        "application/vnd.millrace.router-call.v3+json",
        "application/vnd.millrace.router-call.v2",
        "application/vnd.millrace.router-call+json",
        "application/vnd.millrace.router-call.v2+json;q=0",
        "application/json, application/vnd.millrace.router-call.v2+json",
    ):
        headers = {**bearer, "Accept": accept}
        post = client.post(
            "/api/v1/router/requests", headers=headers,
            json={"home": "default", "request": {}},
        )
        get = client.get("/api/v1/router/requests/call?home=default", headers=headers)
        for response in (post, get):
            assert response.status_code == 406, response.text
            assert response.json()["code"] == "unsupported_observation_version"
    assert service.router_workers.snapshot() == []


def test_repeated_accept_vendor_refuses_before_dispatch(tmp_path, monkeypatch):
    service, client, bearer = setup(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("repeated unsupported Accept reached Core")

    monkeypatch.setattr(service.router_workers, "submit", forbidden)
    monkeypatch.setattr(service.router_workers, "observe", forbidden)
    unsupported = "application/vnd.millrace.router-call.v3+json"
    for first in (
        "application/json", "*/*",
        "application/vnd.millrace.router-call.v1+json",
        "application/vnd.millrace.router-call.v2+json",
    ):
        headers = [*bearer.items(), ("Accept", first), ("Accept", unsupported)]
        post = client.post(
            "/api/v1/router/requests", headers=headers,
            json={"home": "default", "request": {}},
        )
        get = client.get("/api/v1/router/requests/call?home=default", headers=headers)
        for response in (post, get):
            assert response.status_code == 406, response.text
            assert response.json()["code"] == "unsupported_observation_version"
    assert service.router_workers.snapshot() == []


def test_v1_observation_payload_unchanged(tmp_path, monkeypatch):
    from millrace_os.contracts import RouterCallObservation, RouterCallObservationV2

    service, client, bearer = setup(tmp_path)
    payload = {
        "home": "default", "request_id": "call", "request_digest": None,
        "instance_id": None, "run_id": None, "source": "core_receipt",
        "phase": "receipt_observed", "core_receipt": None,
        "observed_at": "2026-10-25T00:00:00+00:00",
    }

    def observe(*args, version=1, **kwargs):
        if version == 2:
            return RouterCallObservationV2(**payload, holder_joined=None)
        return RouterCallObservation(**payload)

    monkeypatch.setattr(service.router_workers, "observe", observe)
    route = "/api/v1/router/requests/call?home=default"
    for accept in (
        "*/*", "application/json", "application/json;q=1",
        "application/json, */*;q=0.8", "text/plain",
        "application/vnd.millrace.router-call.v1+json",
    ):
        response = client.get(route, headers={**bearer, "Accept": accept})
        assert response.json() == {"contract": "millrace-os-router-call/v1", **payload}
    media = "application/vnd.millrace.router-call.v2+json"
    response = client.get(route, headers={**bearer, "Accept": "  " + media + " "})
    assert response.json() == {
        "contract": "millrace-os-router-call/v2", **payload, "holder_joined": None
    }
    assert response.headers["content-type"] == media


def test_repeated_ordinary_accept_preserves_v1_on_post_and_get(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    from millrace_os.contracts import RouterCallObservation

    service, client, bearer = setup(tmp_path)
    payload = RouterCallObservation(
        home="default", request_id="call", request_digest=None,
        instance_id=None, run_id=None, source="core_receipt",
        phase="receipt_observed", core_receipt=None,
        observed_at="2026-10-25T00:00:00+00:00",
    )
    done = threading.Event()
    done.set()
    observed_versions = []

    def observe(*args, version=1, **kwargs):
        observed_versions.append(version)
        return payload

    monkeypatch.setattr(
        service.router_workers, "submit",
        lambda *args: SimpleNamespace(done=done, error=None),
    )
    monkeypatch.setattr(service.router_workers, "call_observation", observe)
    monkeypatch.setattr(service.router_workers, "observe", observe)
    headers = [
        *bearer.items(), ("Accept", "application/json;q=1"),
        ("Accept", "text/plain, */*;q=0.8"),
    ]
    post = client.post(
        "/api/v1/router/requests", headers=headers,
        json={"home": "default", "request": {}},
    )
    get = client.get("/api/v1/router/requests/call?home=default", headers=headers)
    for response in (post, get):
        assert response.status_code == 200, response.text
        assert response.json() == payload.model_dump()
        assert "holder_joined" not in response.json()
    assert observed_versions == [1, 1]
