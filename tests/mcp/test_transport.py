"""Bounds, access errors, credential identity and owned transport lifetimes."""

from __future__ import annotations

import asyncio
import io
import json

import httpx

from millrace_os.mcp_auth import HTTPGuard
from millrace_os.mcp_bridge import Bridge, _Input, _Output
from millrace_os.mcp_client import BackendClient
from millrace_os.mcp_contracts import MAX_INPUT, MAX_OUTPUT, PROTOCOL


def test_fixed_backend_paths_and_no_redirect_or_proxy(bridge_config, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.invalid:9999")
    calls = []

    def peer(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example"})

    async def check():
        backend = BackendClient(bridge_config, transport=httpx.MockTransport(peer))
        try:
            result = await backend.call("millrace_v1_instances", {"home": "main"})
            assert result["access_error"]["status"] == 302
            assert len(calls) == 1
            assert (
                str(calls[0].url)
                == bridge_config.os_origin + "/api/v1/router/instances?home=main"
            )
            assert set(calls[0].headers) >= {"authorization"}
            assert "cookie" not in calls[0].headers
        finally:
            await backend.close()

    asyncio.run(check())


def test_backend_credential_rotation(bridge_config, credential):
    calls = []

    def peer(request):
        calls.append(request)
        return httpx.Response(401 if len(calls) == 1 else 200, json={"data": []})

    async def check():
        backend = BackendClient(bridge_config, transport=httpx.MockTransport(peer))
        try:
            first = await backend.call("millrace_v1_instances", {"home": "main"})
            assert first["access_error"]["status"] == 401
            credential.write_text(json.dumps({"token": "B" * 48}))
            assert await backend.call("millrace_v1_instances", {"home": "main"}) == {
                "data": []
            }
            assert [r.headers["authorization"] for r in calls] == [
                "Bearer " + "A" * 48,
                "Bearer " + "B" * 48,
            ]
            credential.chmod(0o644)
            assert (await backend.call("millrace_v1_instances", {"home": "main"}))[
                "access_error"
            ]["code"] == "local_credential_unavailable"
            assert len(calls) == 2
            credential.unlink()
            assert "access_error" in await backend.call(
                "millrace_v1_instances", {"home": "main"}
            )
            assert not credential.exists()
        finally:
            await backend.close()

    asyncio.run(check())


def test_stdio_ingress_and_response_bounds():
    stream = io.BytesIO()
    output = _Output(stream)
    source = io.BytesIO(b"x" * (MAX_INPUT + 1) + b"\n" + b"{}\n")
    bounded = _Input(source, output)
    data = bytearray(64)
    assert bounded.readinto(data) == 3
    assert bytes(data[:3]) == b"{}\n"
    assert json.loads(stream.getvalue())["error"]["message"] == "request_too_large"
    stream.seek(0)
    stream.truncate()
    output.write(b"x" * (MAX_OUTPUT + 1) + b"\n")
    assert json.loads(stream.getvalue())["error"]["message"] == "response_too_large"
    exact = _Input(io.BytesIO(b"x" * (MAX_INPUT - 1) + b"\n"), output)
    read = bytearray(MAX_INPUT)
    assert exact.readinto(read) == MAX_INPUT


def test_http_ingress_and_response_bounds(http_config):
    calls = []

    async def app(scope, receive, send):
        calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * (MAX_OUTPUT + 1)})

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=HTTPGuard(app, http_config)),
            base_url="https://mcp.example",
        ) as client:
            response = await client.post("/mcp", content=b"x" * (MAX_INPUT + 1))
            assert response.status_code == 413 and not calls
            response = await client.post(
                "/mcp",
                json={"method": "tools/list"},
                headers={"mcp-protocol-version": PROTOCOL},
            )
            assert response.status_code == 502 and len(calls) == 1
            response = await client.post(
                "/mcp",
                json={"method": "initialize", "params": {"protocolVersion": "unknown"}},
            )
            assert response.status_code == 400 and len(calls) == 1

    asyncio.run(check())


def test_tool_concurrency_busy_is_retryable(bridge_config):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        class Backend:
            async def call(self, name, args):
                entered.set()
                await release.wait()
                return {"data": []}

        bridge = Bridge(bridge_config, Backend())
        bridge._admitted = lambda: True
        tasks = [
            asyncio.create_task(
                bridge.call_tool("millrace_v1_instances", {"home": "main"})
            )
            for _ in range(8)
        ]
        try:
            await entered.wait()
            await asyncio.sleep(0)
            ninth = await bridge.call_tool("millrace_v1_instances", {"home": "main"})
            assert ninth["access_error"]["code"] == "busy"
            assert ninth["access_error"]["retryable"]
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            assert bridge.active == 7
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert bridge.active == 0
            assert "os" in await bridge.call_tool(
                "millrace_v1_instances", {"home": "main"}
            )
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(check())


def test_backend_413_and_429_remain_access_errors(bridge_config, real_os):
    async def check():
        for status in (413, 429):
            backend = BackendClient(
                bridge_config,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        status, json={"code": "not-copied", "secret": "never-echo"}
                    )
                ),
            )
            try:
                result = await backend.call("millrace_v1_instances", {"home": "main"})
                assert result["access_error"]["status"] == status
                assert result["access_error"]["retryable"] == (status == 429)
                assert "core_receipt" not in result and "never-echo" not in json.dumps(
                    result
                )
            finally:
                await backend.close()
        service, config = real_os
        backend = BackendClient(config)
        try:
            await backend.startup()
            request = {
                "operation": "create_instance",
                "request_id": "a" * 32,
                "payload": {"oversized": "x" * 5000},
            }
            result = await backend.call(
                "millrace_v1_request", {"home": "main", "request": request}
            )
            assert result["access_error"]["status"] == 413
            assert result["access_error"]["code"] == "request_too_large"
            assert not service.router_workers.snapshot()
        finally:
            await backend.close()

    asyncio.run(check())


def test_disconnect_and_bridge_stop_leave_os_worker_owned(real_os):
    service, config = real_os

    async def check():
        backend = BackendClient(config)
        await backend.startup()
        await backend.close()
        # Pool closure has no daemon/worker shutdown hook.
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(config.os_origin + "/health")).status_code == 200
        assert not service.router_workers.snapshot()

    asyncio.run(check())


def test_backend_timeout_preserves_unknown_correlation(bridge_config):
    calls = []

    def peer(request):
        calls.append(request)
        raise httpx.ReadTimeout("do-not-echo")

    async def check():
        backend = BackendClient(bridge_config, transport=httpx.MockTransport(peer))
        request = {
            "operation": "start_run",
            "request_id": "a" * 32,
            "instance_id": "b" * 32,
        }
        try:
            result = await backend.call(
                "millrace_v1_request", {"home": "main", "request": request}
            )
            assert result["access_error"]["code"] == "backend_timeout"
            assert result["access_error"]["observation"] == "unknown"
            assert (
                result["access_error"]["correlation"]["request_id"]
                == request["request_id"]
            )
            assert json.loads(calls[0].content)["request"] == request
            assert len(calls) == 1 and "do-not-echo" not in json.dumps(result)
        finally:
            await backend.close()

    asyncio.run(check())
