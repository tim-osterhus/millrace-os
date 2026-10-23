"""Resource authentication and hostile HTTP inputs refuse without OS effects."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

from millrace_os.mcp_auth import HTTPGuard, ResourceVerifier
from millrace_os.mcp_bridge import Bridge
from millrace_os.mcp_client import BackendClient
from millrace_os.mcp_contracts import PROTOCOL


@pytest.fixture
def auth_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        "millrace_os.mcp_auth.time",
        SimpleNamespace(monotonic=lambda: now[0], time=time.time),
    )
    return now


def test_rs256_claims_and_key_rotation(http_config, signing, auth_clock):
    from cryptography.hazmat.primitives.asymmetric import rsa

    key, jwk, token = signing
    calls = []
    published_keys = [jwk]

    def keys(request):
        calls.append(request)
        return httpx.Response(200, json={"keys": published_keys})

    async def check():
        verifier = ResourceVerifier(http_config, transport=httpx.MockTransport(keys))
        try:
            assert await verifier.verify_token(token())
            for change in (
                {"iss": "https://evil.example"},
                {"aud": "elsewhere"},
                {"aud": [http_config.resource_url]},
                {"sub": "intruder"},
                {"scope": "read"},
                {"exp": time.time() - 1},
                {"iat": time.time() + 20},
                {"nbf": time.time() + 20},
                {"exp": str(time.time() + 30)},
                {"iat": True},
                {"exp": float("inf")},
            ):
                assert await verifier.verify_token(token(**change)) is None
            assert await verifier.verify_token("x" * 16385) is None
            assert (
                await verifier.verify_token(
                    jwt.encode(
                        {"sub": "operator"},
                        "fixture-not-a-real-secret-xxxxxxxxxxxx",
                        algorithm="HS256",
                    )
                )
                is None
            )
            assert len(calls) == 1
            auth_clock[0] += 300
            assert await verifier.verify_token(token())
            assert len(calls) == 2
            rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            published_keys[:] = [
                json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rotated.public_key()))
                | {"kid": "rotated"}
            ]
            claims = {
                "iss": http_config.issuer_url,
                "aud": http_config.resource_url,
                "sub": "operator",
                "scope": "millrace:operator",
                "iat": time.time() - 1,
                "exp": time.time() + 60,
            }
            replacement = jwt.encode(
                claims, rotated, algorithm="RS256", headers={"kid": "rotated"}
            )
            assert await verifier.verify_token(replacement) is None
            assert len(calls) == 2
            auth_clock[0] += 30
            assert await verifier.verify_token(replacement)
            assert len(calls) == 3
            bad_signature = jwt.encode(
                claims, key, algorithm="RS256", headers={"kid": "rotated"}
            )
            assert await verifier.verify_token(bad_signature) is None
            assert len(calls) == 3
        finally:
            await verifier.close()

    asyncio.run(check())


def test_distinct_unknown_keys_share_cold_and_warm_cooldown(
    http_config, signing, auth_clock
):
    key, jwk, token = signing

    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        all_started = asyncio.Event()
        calls = []
        started = 0

        async def fetch(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"keys": [jwk]})

        def unknown(number):
            return jwt.encode(
                {}, key, algorithm="RS256", headers={"kid": f"missing-{number}"}
            )

        verifier = ResourceVerifier(http_config, transport=httpx.MockTransport(fetch))
        pending = asyncio.create_task(verifier.verify_token(unknown(0)))
        burst = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)

            async def verify_unknown(number):
                nonlocal started
                started += 1
                if started == 20:
                    all_started.set()
                return await verifier.verify_token(unknown(number))

            burst = asyncio.gather(*(verify_unknown(i) for i in range(1, 21)))
            await asyncio.wait_for(all_started.wait(), timeout=1)
            assert not burst.done() and len(calls) == 1
            release.set()
            assert await pending is None
            assert await burst == [None] * 20
            assert await verifier.verify_token(token())
            auth_clock[0] += 29.999
            assert await verifier.verify_token(unknown(21)) is None
            assert len(calls) == 1
            auth_clock[0] = 1030.0
            assert await verifier.verify_token(unknown(22)) is None
            assert len(calls) == 2
            assert await verifier.verify_token("") is None
        finally:
            release.set()
            await pending
            if burst is not None:
                await burst
            await verifier.close()

    asyncio.run(check())


@pytest.mark.parametrize("expired", [False, True])
def test_concurrent_valid_callers_share_cold_or_expired_refresh(
    http_config, signing, auth_clock, expired
):
    _, jwk, token = signing

    async def check():
        entered, release, second_started = (
            asyncio.Event(), asyncio.Event(), asyncio.Event()
        )
        calls = []

        async def fetch(request):
            calls.append(request)
            if len(calls) == 1 + int(expired):
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"keys": [jwk]})

        verifier = ResourceVerifier(http_config, transport=httpx.MockTransport(fetch))
        pending = []
        try:
            if expired:
                assert await verifier.verify_token(token())
                auth_clock[0] += 300
            pending.append(asyncio.create_task(verifier.verify_token(token())))
            await asyncio.wait_for(entered.wait(), timeout=1)

            async def second():
                second_started.set()
                return await verifier.verify_token(token())

            pending.append(asyncio.create_task(second()))
            await asyncio.wait_for(second_started.wait(), timeout=1)
            assert verifier.lock.locked() and not pending[1].done()
            assert len(calls) == 1 + int(expired)
            release.set()
            principals = await asyncio.gather(*pending)
            assert all(principal is not None for principal in principals)
            assert [principal.client_id for principal in principals] == [
                "operator", "operator"
            ]
            assert all(
                "millrace:operator" in principal.scopes for principal in principals
            )
            assert len(calls) == 1 + int(expired)
        finally:
            release.set()
            await asyncio.gather(*pending)
            await verifier.close()

    asyncio.run(check())


def test_fresh_known_key_does_not_wait_for_unknown_refresh(
    http_config, signing, auth_clock
):
    key, jwk, token = signing

    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(request):
            calls.append(request)
            if len(calls) == 2:
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"keys": [jwk]})

        verifier = ResourceVerifier(http_config, transport=httpx.MockTransport(fetch))
        pending = None
        try:
            assert await verifier.verify_token(token())
            auth_clock[0] += 30
            unknown = jwt.encode(
                {}, key, algorithm="RS256", headers={"kid": "missing"}
            )
            pending = asyncio.create_task(verifier.verify_token(unknown))
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert verifier.lock.locked() and not release.is_set()
            assert await asyncio.wait_for(verifier.verify_token(token()), timeout=1)
            assert not pending.done() and len(calls) == 2
        finally:
            release.set()
            if pending is not None:
                assert await pending is None
            await verifier.close()

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["http", "network", "parse", "timeout", "cancel"])
def test_failed_attempt_cooldown_and_recovery(
    http_config, signing, auth_clock, failure
):
    key, jwk, token = signing

    async def check():
        calls = []

        def fetch(request):
            calls.append(request)
            if len(calls) > 1:
                return httpx.Response(200, json={"keys": [jwk]})
            if failure == "network":
                raise httpx.ConnectError("fixture", request=request)
            if failure == "timeout":
                raise TimeoutError("fixture")
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "parse":
                return httpx.Response(200, content=b"not-json")
            return httpx.Response(503)

        verifier = ResourceVerifier(http_config, transport=httpx.MockTransport(fetch))
        try:
            if failure == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await verifier.verify_token(token())
            else:
                assert await verifier.verify_token(token()) is None
            assert verifier.loaded == 0 and verifier.keys == {}
            assert await verifier.verify_token(token()) is None
            distinct = [
                jwt.encode(
                    {}, key, algorithm="RS256", headers={"kid": f"missing-{i}"}
                )
                for i in range(20)
            ]
            assert await asyncio.gather(
                *(verifier.verify_token(value) for value in distinct)
            ) == [None] * 20
            auth_clock[0] = 1029.999
            assert await verifier.verify_token(token()) is None
            assert len(calls) == 1 and verifier.loaded == 0
            auth_clock[0] = 1030.0
            assert await verifier.verify_token(token())
            assert len(calls) == 2 and verifier.loaded == 1030.0
        finally:
            await verifier.close()

    asyncio.run(check())


def test_cache_expiry_never_uses_stale_key_during_failure_cooldown(
    http_config, signing, auth_clock
):
    key, jwk, token = signing

    async def check():
        calls = []

        def fetch(request):
            calls.append(request)
            if len(calls) in {2, 4}:
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": [jwk]})

        verifier = ResourceVerifier(http_config, transport=httpx.MockTransport(fetch))
        try:
            assert await verifier.verify_token(token())
            auth_clock[0] = 1290.0
            unknown = jwt.encode(
                {}, key, algorithm="RS256", headers={"kid": "missing"}
            )
            assert await verifier.verify_token(unknown) is None
            assert verifier.loaded == 1000.0 and len(calls) == 2
            auth_clock[0] = 1299.999
            assert await verifier.verify_token(token())
            auth_clock[0] = 1300.0
            assert await verifier.verify_token(token()) is None
            assert len(calls) == 2 and verifier.loaded == 1000.0
            auth_clock[0] = 1320.0
            assert await verifier.verify_token(token())
            assert len(calls) == 3 and verifier.loaded == 1320.0
            auth_clock[0] = 1620.0
            assert await verifier.verify_token(token()) is None
            assert len(calls) == 4 and verifier.loaded == 1320.0
            auth_clock[0] = 1650.0
            assert await verifier.verify_token(token())
            assert len(calls) == 5 and verifier.loaded == 1650.0
        finally:
            await verifier.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "irrelevant",
    [
        {"kty": "EC"},
        {"kty": "EC", "kid": "fixture"},
        {"kty": "RSA", "use": "enc"},
        {"kty": "RSA", "use": "enc", "kid": "fixture"},
        {"kty": "RSA", "alg": "RS512"},
        {"kty": "RSA", "alg": "RS512", "kid": "fixture"},
    ],
)
@pytest.mark.parametrize("reversed_keys", [False, True])
def test_irrelevant_keys_do_not_poison_selected_set(
    http_config, signing, irrelevant, reversed_keys
):
    _, jwk, token = signing
    keys = [jwk, irrelevant]
    if reversed_keys:
        keys.reverse()

    async def check():
        verifier = ResourceVerifier(
            http_config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": keys})
            ),
        )
        try:
            assert await verifier.verify_token(token())
            assert set(verifier.keys) == {"fixture"}
        finally:
            await verifier.close()

    asyncio.run(check())


@pytest.mark.parametrize("invalid_identity", ["duplicate", "missing"])
def test_selected_key_identity_still_fails_closed(
    http_config, signing, invalid_identity
):
    _, jwk, token = signing
    invalid = dict(jwk)
    if invalid_identity == "missing":
        invalid.pop("kid")

    async def check():
        verifier = ResourceVerifier(
            http_config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": [jwk, invalid]})
            ),
        )
        try:
            assert await verifier.verify_token(token()) is None
            assert verifier.keys == {} and verifier.loaded == 0
        finally:
            await verifier.close()

    asyncio.run(check())


def test_jwks_bounds_and_single_refresh(http_config, signing):
    key, jwk, token = signing

    async def check():
        for response in (
            httpx.Response(302, headers={"Location": "https://evil.example"}),
            httpx.Response(200, content=b"x" * 262145),
            httpx.Response(200, json={"keys": [jwk, jwk]}),
        ):
            calls = []

            def fetch(request):
                calls.append(request)
                assert str(request.url) == http_config.jwks_url
                return response

            verifier = ResourceVerifier(
                http_config, transport=httpx.MockTransport(fetch)
            )
            try:
                assert await verifier.verify_token(token()) is None
                assert len(calls) == 1
            finally:
                await verifier.close()
        calls = []

        def fetch(request):
            calls.append(request)
            return httpx.Response(200, json={"keys": [jwk]})

        verifier = ResourceVerifier(http_config, transport=httpx.MockTransport(fetch))
        try:
            unknown = jwt.encode(
                {"exp": time.time() + 30},
                key,
                algorithm="RS256",
                headers={"kid": "missing"},
            )
            assert await verifier.verify_token(unknown) is None
            assert len(calls) == 1
            hostile = jwt.encode(
                {},
                key,
                algorithm="RS256",
                headers={"kid": "fixture", "jku": "https://evil.example"},
            )
            assert await verifier.verify_token(hostile) is None
            assert len(calls) == 1
        finally:
            await verifier.close()

    asyncio.run(check())


def test_origin_host_ssrf_and_token_separation(http_config):
    calls = []

    async def app(scope, receive, send):
        calls.append(scope)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"access-control-allow-origin", b"*")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=HTTPGuard(app, http_config)),
            base_url="https://mcp.example",
        ) as client:
            for headers in (
                {"host": "evil.example"},
                {"origin": "null"},
                {"origin": ""},
                {"origin": "https://evil.example"},
            ):
                assert (
                    await client.get(
                        "/.well-known/oauth-protected-resource/mcp", headers=headers
                    )
                ).status_code == 403
            duplicate = await client.get(
                "/guard-test",
                headers=[("Host", "mcp.example"), ("Host", "evil.example")],
            )
            assert duplicate.status_code == 403
            assert not calls
            response = await client.get(
                "/guard-test", headers={"origin": "https://client.example"}
            )
            assert (
                response.headers["access-control-allow-origin"]
                == "https://client.example"
            )
            assert len(calls) == 1
            response = await client.get("/guard-test")
            assert "access-control-allow-origin" not in response.headers
            assert len(calls) == 2
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=HTTPGuard(app, http_config), client=("192.0.2.1", 123)
            ),
            base_url="https://mcp.example",
        ) as client:
            response = await client.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"x-forwarded-host": "mcp.example"},
            )
            assert response.status_code == 403 and len(calls) == 2

    asyncio.run(check())


def test_resource_metadata_is_public_config_only_and_guarded(http_config, signing):
    _, jwk, token = signing
    verifier = ResourceVerifier(
        http_config,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"keys": [jwk]})
        ),
    )
    backend = BackendClient(http_config)
    bridge = Bridge(http_config, backend, verifier)
    with TestClient(
        HTTPGuard(bridge.streamable_http_app(), http_config),
        base_url="https://mcp.example",
    ) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        value = metadata.json()
        assert value["resource"] == http_config.resource_url
        assert value["authorization_servers"] == [http_config.issuer_url]
        assert value["scopes_supported"] == ["millrace:operator"]
        assert not any(
            x in metadata.text for x in ("main", "credential", "0.23", "os_origin")
        )
        assert (
            client.get(
                "/.well-known/oauth-protected-resource/mcp", headers={"origin": "null"}
            ).status_code
            == 403
        )
        headers = {
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": PROTOCOL,
        }
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert client.post("/mcp", json=message, headers=headers).status_code == 401
        headers["authorization"] = "Bearer " + token()
        response = client.post("/mcp", json=message, headers=headers)
        assert response.status_code == 200, response.text
        assert len(response.json()["result"]["tools"]) == 7
        for bearer in (token(exp=time.time() - 1), token(aud="wrong-resource")):
            headers["authorization"] = "Bearer " + bearer
            assert client.post("/mcp", json=message, headers=headers).status_code == 401
    asyncio.run(backend.close())
    asyncio.run(verifier.close())


def test_auth_and_version_fail_before_dispatch(bridge_config):
    calls = []

    async def check():
        def wrong(request):
            calls.append(request)
            return httpx.Response(
                200, json={"data": {"api": "v2", "package": "millrace-os"}}
            )

        backend = BackendClient(bridge_config, transport=httpx.MockTransport(wrong))
        try:
            with pytest.raises(ValueError):
                await backend.startup()
            assert {r.url.path for r in calls} == {
                "/api/v1/version",
                "/api/v1/contracts",
            }
        finally:
            await backend.close()

    asyncio.run(check())

    async def corrupt_registry():
        def peer(request):
            if request.url.path.endswith("/version"):
                return httpx.Response(
                    200, json={"data": {"api": "v1", "package": "millrace-os"}}
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "registry": "os.registry.v1",
                        "schemas": [
                            {"contract_id": "RouterRead-JsonValue", "version": True},
                            {"contract_id": "RouterCallObservation", "version": 1},
                        ],
                    }
                },
            )

        backend = BackendClient(bridge_config, transport=httpx.MockTransport(peer))
        try:
            with pytest.raises(ValueError):
                await backend.startup()
        finally:
            await backend.close()

    asyncio.run(corrupt_registry())
