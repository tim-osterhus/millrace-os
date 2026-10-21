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
