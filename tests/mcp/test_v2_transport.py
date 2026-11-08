"""MCP v2 framing over the pinned SDK, stdio and Streamable HTTP (N11-amended).

The input bound is I2 = 2B + A: the pinned SDK spells a dense `1e-05` body as
`0.00001`, so a valid near-B API body costs more than B on the wire. Results
stay within 3R + 1,024 by the joint text/structured token bound, and every
response reserves the entire possible incoming message. Requests below are the
exact SDK encodings (`JSONRPCRequest.model_dump_json`); responses are measured
as the bridge wrote them.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import select
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from mcp.types import JSONRPCRequest
from test_shared_requests import clients

from millrace_os import mcp_contracts as mc
from millrace_os import presentation as p
from millrace_os.credentials import CredentialStore
from millrace_os.mcp_auth import HTTPGuard, ResourceVerifier
from millrace_os.mcp_bridge import Bridge, _Input, _Output
from millrace_os.mcp_client import BackendClient
from millrace_os.mcp_contracts import PROTOCOL, MCPConfig

B, A = 1_048_576, 65_536
I2, R, O2 = mc.V2_MAX_INPUT, mc.V2_MAX_API_RESPONSE, mc.V2_MAX_OUTPUT
OLD_I2 = B + A  # the pre-N11 G0 value
CEILING = 3 * R + 1_024
BOARD = {
    "frontend_id": "example.board",
    "presentation_format": "example.board-layout/v1",
}
# N11 vectors: quotes, backslashes and C0 controls, direct multibyte Unicode
# and one nested mixed object/array, carried as a valid custom payload.
ESCAPES = '"\\\x01\n\té€\U0001f600'
NESTED: object = "leaf"
for depth in range(32):
    NESTED = {"d": depth, "list": [NESTED, 1e-05, None, True]}


def compact(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def sdk_request(name, arguments, request_id=1) -> bytes:
    """Exactly what the pinned SDK client writes for this call (without LF)."""
    message = JSONRPCRequest(
        jsonrpc="2.0",
        id=request_id,
        method="tools/call",
        params={"name": name, "arguments": arguments},
    )
    return message.model_dump_json(by_alias=True, exclude_none=True).encode()


def store(payload, name="Dense"):
    config = {"id": "a" * 32, "name": name, "payload": payload}
    namespace = {**BOARD, "default_ref": None, "configs": [config]}
    return {"format": p.STORE_FORMAT, "namespaces": [namespace]}


def write(payload):
    return {
        "format": p.WRITE_FORMAT,
        "mode": "compare",
        "expected_version": None,
        "store": store(payload),
    }


def dense_write(size):
    """A valid custom write whose compact API body is exactly `size` bytes."""

    def payload(count, text=""):
        return {"v": [1e-05] * count, "t": text, "n": NESTED, "e": ESCAPES}

    base = len(compact(write(payload(1))))
    count = 1 + (size - base) // 6  # each further element adds `,1e-05`
    body = write(payload(count))
    body = write(payload(count, "x" * (size - len(compact(body)))))
    assert len(compact(body)) == size
    return body


def test_v2_stdio_and_http_guards_count_exact_wire_bytes(http_config):
    # stdio: a line of exactly I2 bytes including its LF passes; I2 + 1 refuses.
    stream = io.BytesIO()
    output = _Output(stream, O2)
    exact = b"x" * (I2 - 1) + b"\n"
    ingress = _Input(io.BytesIO(b"x" * I2 + b"\n" + exact), output, I2)
    data = bytearray(I2)
    assert ingress.readinto(data) == I2 and bytes(data) == exact
    assert json.loads(stream.getvalue())["error"]["message"] == "request_too_large"
    stream.seek(0)
    stream.truncate()
    shell = compact({"jsonrpc": "2.0", "id": 1, "result": {"x": ""}})
    line = shell[:-3] + b"x" * (O2 - len(shell) - 1) + shell[-3:] + b"\n"
    assert len(line) == O2
    output.write(line)
    assert stream.getvalue() == line
    stream.seek(0)
    stream.truncate()
    output.write(line[:-1] + b" \n")
    assert json.loads(stream.getvalue())["error"]["message"] == "response_too_large"

    # HTTP: the guard admits exactly I2 and egresses up to exactly O2.
    calls = []
    sizes = iter((O2, O2 + 1))

    async def app(scope, receive, send):
        calls.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * next(sizes)})

    def padded(size):
        message = compact({"method": "tools/list"})
        return message[:1] + b" " * (size - len(message)) + message[1:]

    async def check():
        guard = HTTPGuard(app, http_config, profile=mc.V2)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=guard), base_url="https://mcp.example"
        ) as client:
            headers = {"mcp-protocol-version": PROTOCOL}
            over = padded(I2 + 1)
            response = await client.post("/mcp", content=over, headers=headers)
            assert response.status_code == 413 and not calls
            response = await client.post("/mcp", content=padded(I2), headers=headers)
            assert response.status_code == 200 and len(response.content) == O2
            assert len(calls[0]) == I2
            response = await client.post("/mcp", content=padded(I2), headers=headers)
            assert response.status_code == 502

    asyncio.run(check())


@contextmanager
def v2_http_bridge(config, signing, port):
    """The in-process HTTP bridge of `test_shared_requests`, with the v2 profile."""
    _, jwk, token = signing
    http_config = MCPConfig.model_validate(
        config.model_dump(exclude_unset=True)
        | {
            "transport": "streamable-http",
            "resource_url": "https://mcp.example/mcp",
            "issuer_url": "https://issuer.example",
            "jwks_url": "https://issuer.example/jwks",
            "trusted_subjects": ["operator"],
            "allowed_hosts": ["mcp.example"],
            "allowed_origins": ["https://client.example"],
            "listen_port": port,
        }
    )
    server, failures, ready = None, [], threading.Event()

    async def run():
        nonlocal server
        backend = BackendClient(config, profile=mc.V2)
        verifier = ResourceVerifier(
            http_config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": [jwk]})
            ),
        )
        try:
            await backend.startup()
            bridge = Bridge(http_config, backend, verifier, profile=mc.V2)
            server = uvicorn.Server(
                uvicorn.Config(
                    HTTPGuard(bridge.streamable_http_app(), http_config, profile=mc.V2),
                    host="127.0.0.1",
                    port=port,
                    access_log=False,
                    log_level="critical",
                    proxy_headers=False,
                )
            )
            ready.set()
            await server.serve()
        finally:
            await backend.close()
            await verifier.close()

    def own():
        try:
            asyncio.run(run())
        except BaseException as error:
            failures.append(error)
            ready.set()

    thread = threading.Thread(target=own, name="mcp-v2-fixture-http")
    thread.start()
    try:
        assert ready.wait(5)
        deadline = time.monotonic() + 5
        while server and not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server and server.started and not failures
        yield {
            "transport": "streamable-http",
            "url": f"http://127.0.0.1:{port}/mcp",
            "token": token(),
        }
    finally:
        if server:
            server.should_exit = True
        thread.join(8)
        assert not thread.is_alive() and not failures


@contextmanager
def peer_base(transport, config, tmp_path, signing, port):
    if transport == "stdio":
        path = tmp_path / "bridge.json"
        path.write_text(config.model_dump_json(exclude_unset=True))
        yield {"transport": "stdio", "config": str(path), "serve": ["--tools", "v2"]}
    else:
        with v2_http_bridge(config, signing, port) as base:
            yield base


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_sdk_peer_carries_a_near_limit_dense_float_write(
    real_os, tmp_path, signing, mcp_port, transport
):
    service, config = real_os
    # The largest write whose resulting GET state still fits the 1 MiB policy.
    body = dense_write(B - 4096)
    arguments = {"operation": "put", "write": body}
    request = sdk_request("millrace_v2_presentation", arguments)
    assert OLD_I2 < len(request) + 1 <= I2  # beyond the old bound, LF included
    with peer_base(transport, config, tmp_path, signing, mcp_port) as base:
        (result,) = clients(
            [base | {"calls": [{"name": "millrace_v2_presentation",
                                "arguments": arguments}]}]
        )
    names = {tool["name"] for tool in result["tools"]}
    assert "millrace_v2_presentation" in names
    assert not any("frontend" in n or "demo" in n for n in names)
    (call,) = result["results"]
    assert not call.get("isError"), call
    structured = call["structuredContent"]
    assert json.loads(call["content"][0]["text"]) == structured
    assert structured["http_status"] == 200 and structured["os"]["status"] == "ready"
    # The forwarded JSON meaning arrived intact: floats, escapes, nesting.
    state = p.read_state(service.config.config_dir)
    assert state["store"]["namespaces"] == body["store"]["namespaces"]


def stdio_exchange(process, raw: bytes, *, reply=True):
    process.stdin.write(raw + b"\n")
    process.stdin.flush()
    if not reply:
        return None
    assert select.select([process.stdout], [], [], 30)[0]
    return process.stdout.readline()


@contextmanager
def stdio_bridge(config, tmp_path):
    path = tmp_path / "raw-bridge.json"
    path.write_text(config.model_dump_json(exclude_unset=True))
    process = subprocess.Popen(
        [sys.executable, "-m", "millrace_os.cli", "mcp", "serve", "--config",
         str(path), "--tools", "v2"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
    )
    try:
        hello = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "1"},
            },
        }
        assert json.loads(stdio_exchange(process, compact(hello)))["id"] == 0
        notice = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        stdio_exchange(process, compact(notice), reply=False)
        yield lambda raw: stdio_exchange(process, raw)
    finally:
        # `communicate` closes stdin itself; that end of input lets it exit.
        try:
            _, err = process.communicate(timeout=10)
            assert process.returncode == 0, err
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@contextmanager
def http_raw(config, signing, port):
    with v2_http_bridge(config, signing, port) as base:
        headers = {
            "Host": "mcp.example",
            "Authorization": "Bearer " + base["token"],
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-protocol-version": PROTOCOL,
        }
        with httpx.Client(timeout=30, trust_env=False) as client:

            def send(raw):
                response = client.post(base["url"], content=raw, headers=headers)
                return response.status_code, response.content

            yield send


def plant_near_limit_state(config_dir):
    """A ready state whose GET envelope is just under the 1 MiB write policy."""

    def envelope(count):
        full = store({"v": [1e-05] * count, "e": ESCAPES * 200, "n": NESTED})
        state = {
            "contract": p.STATE_CONTRACT,
            "status": "ready",
            "version": "0" * 32,
            "file_sha256": "0" * 64,
            "store": {**full, "version": "0" * 32},
            "bundled_configs": p.bundled_configs(),
            "observed_at": "2026-11-07T00:00:00.000000+00:00",
        }
        return full, len(compact(state))

    base = envelope(1)[1]
    full, size = envelope(1 + (p.STATE_LIMIT - 64 - base) // 6)
    assert p.STATE_LIMIT - 70 <= size <= p.STATE_LIMIT - 64
    p.write_state(
        config_dir,
        {"format": p.WRITE_FORMAT, "mode": "compare", "expected_version": None,
         "store": full},
    )
    return full


def frame(raw: bytes, request_id) -> tuple[dict, int]:
    """Check the exact echoed ID; return the response and its result's bytes."""
    prefix = b'{"jsonrpc":"2.0","id":' + compact(request_id) + b',"result":'
    assert raw.startswith(prefix), raw[:200]
    body = raw.rstrip(b"\n")
    assert body.endswith(b"}")
    return json.loads(body), len(body) - len(prefix) - 1


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_raw_wire_at_the_input_bound_with_a_large_echoed_id(
    real_os, tmp_path, signing, mcp_port, transport
):
    service, config = real_os
    full = plant_near_limit_state(service.config.config_dir)
    lf = 1 if transport == "stdio" else 0
    opened = (
        stdio_bridge(config, tmp_path)
        if transport == "stdio"
        else http_raw(config, signing, mcp_port)
    )
    with opened as send:

        def call(raw):
            if transport == "stdio":
                return 200, send(raw)
            return send(raw)

        # Exactly I2 wire bytes (stdio counting its LF) are admitted.
        bridge = sdk_request("millrace_v2_discover", {"kind": "bridge"}, 7)
        exact = bridge[:1] + b" " * (I2 - lf - len(bridge)) + bridge[1:]
        status, raw = call(exact)
        response, _ = frame(raw, 7)
        limits = response["result"]["structuredContent"]["limits"]
        assert status == 200 and (limits["max_input"], limits["max_output"]) == (I2, O2)
        # One more byte is refused before parsing.
        status, raw = call(exact[:1] + b" " + exact[1:])
        if transport == "stdio":
            assert json.loads(raw)["error"]["message"] == "request_too_large"
        else:
            assert status == 413
        # A large escaped JSON-RPC ID with a near-R response: the exact ID is
        # echoed and the whole response, including it, fits O2.
        identity = '"\\é€' * 6_600  # about 59 KB once escaped, within A
        get = {"operation": "get"}
        request = sdk_request("millrace_v2_presentation", get, identity)
        assert len(request) + lf <= I2
        status, raw = call(request)
        response, result_bytes = frame(raw, identity)
        result = response["result"]
        assert status == 200 and not result.get("isError"), result
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        state = result["structuredContent"]["os"]
        assert state["store"]["namespaces"] == full["namespaces"]
        assert result_bytes <= CEILING
        assert len(raw) <= O2


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_sdk_peer_carries_maximal_draft_and_output_bodies(
    real_os, tmp_path, signing, mcp_port, transport
):
    # Both tools map to operations of the optional router component.
    pytest.importorskip("millrace_plus")
    service, config = real_os
    token = CredentialStore(service.config.credential_path).ensure()
    with httpx.Client(
        base_url=config.os_origin,
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=10,
    ) as api:
        created = api.post("/api/v1/router/drafts", json={"home": "main", "files": []})
    assert created.status_code == 200, created.text
    draft_id = created.json()["draft_id"]
    shell = {"home": "main", "path": "blob.txt", "content_base64": ""}
    content = "A" * ((B - len(compact(shell))) // 4 * 4)
    assert B - 4 < len(compact(shell | {"content_base64": content})) <= B
    put = {
        "operation": "put_file",
        "home": "main",
        "draft_id": draft_id,
        "path": "blob.txt",
        "content_base64": content,
    }
    record = {"format": "example.blob/v1", "blob": ""}
    record["blob"] = "x" * (B - len(compact({"home": "main", "record": record})))
    assert len(compact({"home": "main", "record": record})) == B
    publish = {"operation": "publish", "home": "main", "record": record}
    calls = [
        {"name": "millrace_v2_draft", "arguments": put},
        {"name": "millrace_v2_outputs", "arguments": publish},
    ]
    with peer_base(transport, config, tmp_path, signing, mcp_port) as base:
        (result,) = clients([base | {"calls": calls}])
    names = {tool["name"] for tool in result["tools"]}
    assert {"millrace_v2_draft", "millrace_v2_outputs"} <= names
    stored, published = result["results"]
    # The maximal upload reached OS and its R-sized DraftRead came back.
    assert not stored.get("isError"), stored
    files = stored["structuredContent"]["os"]["files"]
    assert [item["content_base64"] for item in files] == [content]
    # The maximal record was carried: OS answered with its own typed refusal
    # of the record, not a framing or size refusal.
    body = published["structuredContent"]
    assert "access_error" not in body, body
    assert published["isError"] and body["http_status"] not in {413, 502}
    assert body["os"]["code"] != "request_too_large"
