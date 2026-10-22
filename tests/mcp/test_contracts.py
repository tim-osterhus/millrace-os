"""Raw admission must precede the SDK's permissive argument conversion."""

from __future__ import annotations

import copy
import json
import os
import select
import subprocess
import sys
from importlib.resources import files

import pytest
from pydantic import ValidationError

from millrace_os.mcp_contracts import (
    PROTOCOL,
    TOOL_MODELS,
    MCPConfig,
    admit_arguments,
    schema_bundle,
)


def config(**updates):
    value = {
        "format": "millrace-os-mcp-config/v1",
        "transport": "stdio",
        "os_origin": "http://127.0.0.1:8791",
        "os_credential_file": "/private/tmp/token",
        "homes": ["main"],
    }
    return MCPConfig.model_validate(value | updates)


def test_config_rejects_unknown_remote_and_alias_fields(http_config):
    assert config().homes == ["main"]
    assert (
        config(os_origin="http://127.0.0.2:8791").os_origin == "http://127.0.0.2:8791"
    )
    for changes in (
        {"unknown": True},
        {"homes": ["/tmp/main"]},
        {"homes": ["main", "main"]},
        {"os_origin": "http://localhost:8791"},
        {"os_origin": "http://127.0.0.1:8791/a"},
        {"resource_url": "https://mcp.example/mcp"},
        {"listen_port": True},
        {"os_origin": "http://127.0.0.1:8791?"},
        {"os_origin": "http://@127.0.0.1:8791"},
        {"os_origin": " http://127.0.0.1:8791"},
        {"os_origin": "http://127.0.0.1:0"},
    ):
        with pytest.raises(ValidationError):
            config(**changes)
    for name in ("resource_url", "issuer_url", "jwks_url"):
        for value in (
            "https://@example.test/mcp",
            "https://example.test/mcp?",
            " https://example.test/mcp",
            "https://example.test:invalid/mcp",
            "https://example.test:0/mcp",
        ):
            with pytest.raises(ValidationError):
                MCPConfig.model_validate(
                    http_config.model_dump(exclude_unset=True) | {name: value}
                )


def test_all_tool_args_are_strict_before_dispatch():
    samples = {
        "discover": {},
        "instances": {"home": "main"},
        "instance": {"home": "main", "instance_id": "a" * 32},
        "run": {"home": "main", "instance_id": "a" * 32, "run_id": "b" * 32},
        "request": {
            "home": "main",
            "request": {"operation": "start_run", "request_id": "c" * 32},
        },
        "receipt": {"home": "main", "instance_id": "a" * 32, "request_id": "c" * 32},
        "validate": {"home": "main", "instance_id": "a" * 32},
    }
    for suffix, args in samples.items():
        name = "millrace_v1_" + suffix
        assert admit_arguments(name, args, ["main"]) == args
        for bad in (args | {"extra": 1}, json.dumps(args), args | {"home": True}):
            with pytest.raises((ValidationError, ValueError)):
                admit_arguments(name, bad, ["main"])
    for bad in ("A" * 32, "a-b", True):
        with pytest.raises((ValidationError, ValueError)):
            admit_arguments(
                "millrace_v1_instance", {"home": "main", "instance_id": bad}, ["main"]
            )


def test_all_request_operations_reuse_public_validation():
    from millrace_os.mcp_contracts import OPERATIONS

    assert len(OPERATIONS) == 13
    for operation in OPERATIONS:
        raw = {"operation": operation, "request_id": "c" * 32, "untouched": {"n": 3}}
        args = {"home": "main", "request": copy.deepcopy(raw)}
        assert admit_arguments("millrace_v1_request", args, ["main"])["request"] == raw
    for operation in ("recover", "reconcile", "release_hold", "rotate", "draft_apply"):
        with pytest.raises(ValueError):
            admit_arguments(
                "millrace_v1_request",
                {
                    "home": "main",
                    "request": {"operation": operation, "request_id": "c" * 32},
                },
                ["main"],
            )


def test_schema_digest_matches_sdk_and_packaged_fixture():
    bundle = schema_bundle()
    assert len(bundle["tools"]) == len(TOOL_MODELS) == 7
    assert (
        json.loads(
            files("millrace_os").joinpath("schemas/mcp-tools-v1.json").read_text()
        )
        == bundle
    )
    assert (
        json.loads(
            files("millrace_os").joinpath("schemas/mcp-config-v1.json").read_text()
        )
        == MCPConfig.model_json_schema()
    )


def test_optional_extra_and_python_profile():
    from millrace_os.mcp_bridge import require_profile

    assert require_profile(sys.version_info[:2], "1.30.0") is None
    for version, sdk in (((3, 14), "1.30.0"), ((3, 12), "2.3.0")):
        with pytest.raises(ValueError):
            require_profile(version, sdk)


def test_selected_protocol_cannot_admit_tools(real_os, tmp_path, monkeypatch):
    from millrace_os.mcp_bridge import selected_protocol

    assert selected_protocol(PROTOCOL)
    assert not any(
        selected_protocol(x) for x in (None, "2026-01-01", "2024-11-05", "unknown")
    )
    service, configured = real_os
    backend_reads = []
    original = service.router_client.list_instances

    def observed(home):
        backend_reads.append(home)
        return original(home)

    monkeypatch.setattr(service.router_client, "list_instances", observed)
    path = tmp_path / "unsupported.json"
    path.write_text(configured.model_dump_json(exclude_unset=True))
    for version in ("unknown", "2024-11-05"):
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "millrace_v1_instances",
                    "arguments": {"home": "main"},
                },
            },
        ]
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "millrace_os.cli",
                "mcp",
                "serve",
                "--config",
                str(path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        replies = {}
        try:
            for message in messages:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
                if "id" in message:
                    assert select.select([process.stdout], [], [], 5)[0]
                    reply = json.loads(process.stdout.readline())
                    assert reply["id"] == message["id"]
                    replies[reply["id"]] = reply
            process.stdin.close()
            process.stdin = None
            _, err = process.communicate(timeout=5)
            assert process.returncode == 0, err
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        # The pinned SDK can answer fallback negotiation; original parameters
        # still prevent either operation from reaching the backend.
        assert replies[1]["result"]["protocolVersion"] in {PROTOCOL, version}
        assert "error" in replies[2]
        call = replies[3]["result"]
        assert call.get("isError") or (
            call["structuredContent"]["access_error"]["code"] == "unsupported_protocol"
        )
        assert service.router_workers.snapshot() == []
        assert not backend_reads


def test_discovery_and_supported_schema_matrix(bridge_config):
    import asyncio

    from millrace_os.mcp_bridge import Bridge

    class NoBackend:
        async def call(self, name, arguments):
            raise AssertionError("discovery cannot read OS state")

    async def check():
        bridge = Bridge(bridge_config, NoBackend())
        bridge._admitted = lambda: True
        value = await bridge.call_tool("millrace_v1_discover", {})
        tools = await bridge.list_tools()
        assert value["protocol"] == PROTOCOL
        assert value["tool_schema_sha256"] == schema_bundle()["sha256"]
        assert value["homes"] == ["main"]
        assert [
            {"name": t.name, "inputSchema": t.inputSchema} for t in tools
        ] == schema_bundle()["tools"]

    asyncio.run(check())
