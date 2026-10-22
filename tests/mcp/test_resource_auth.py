"""Resource authentication and hostile HTTP inputs refuse without OS effects."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

from millrace_os.mcp_auth import HTTPGuard, ResourceVerifier
from millrace_os.mcp_bridge import Bridge
from millrace_os.mcp_client import BackendClient
from millrace_os.mcp_contracts import PROTOCOL


def test_rs256_claims_and_key_rotation(http_config, signing):
    from cryptography.hazmat.primitives.asymmetric import rsa

    _, jwk, token = signing
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
            verifier.loaded -= 301
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
            assert await verifier.verify_token(replacement)
            assert len(calls) == 3
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
