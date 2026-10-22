"""Local fixture principals and a real loopback OS; no remote credentials."""

from __future__ import annotations

import importlib.util
import json
import socket
import time
from pathlib import Path

import pytest

from millrace_os.mcp_contracts import MCPConfig

pytest.importorskip("mcp")
jwt = pytest.importorskip("jwt")


@pytest.fixture
def credential(tmp_path):
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"token": "A" * 48}))
    path.chmod(0o600)
    return path


@pytest.fixture
def bridge_config(credential):
    return MCPConfig.model_validate(
        {
            "format": "millrace-os-mcp-config/v1",
            "transport": "stdio",
            "os_origin": "http://127.0.0.1:8791",
            "os_credential_file": str(credential),
            "homes": ["main"],
        }
    )


@pytest.fixture
def http_config(bridge_config):
    return MCPConfig.model_validate(
        bridge_config.model_dump(exclude_unset=True)
        | {
            "transport": "streamable-http",
            "resource_url": "https://mcp.example/mcp",
            "issuer_url": "https://issuer.example",
            "jwks_url": "https://issuer.example/jwks",
            "trusted_subjects": ["operator"],
            "allowed_hosts": ["mcp.example"],
            "allowed_origins": ["https://client.example"],
        }
    )


@pytest.fixture
def signing():
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {
        "kid": "fixture",
        "alg": "RS256",
        "use": "sig",
    }

    def token(**changes):
        claims = {
            "iss": "https://issuer.example",
            "aud": "https://mcp.example/mcp",
            "sub": "operator",
            "scope": "millrace:operator",
            "iat": time.time() - 1,
            "exp": time.time() + 60,
        } | changes
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture"})

    return key, jwk, token


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def mcp_port():
    return free_port()


@pytest.fixture
def real_os(tmp_path):
    from millrace_os.config import ApplicationConfig
    from millrace_os.service import Service

    root = tmp_path / "os"
    service = Service(
        ApplicationConfig(
            config_dir=root / "config",
            state_dir=root / "state",
            credential_path=root / "config/credentials.json",
            port=free_port(),
            router_state_homes={"main": root / "core"},
        )
    )
    service.start()
    config = MCPConfig.model_validate(
        {
            "format": "millrace-os-mcp-config/v1",
            "transport": "stdio",
            "os_origin": service.browser_endpoint.rstrip("/"),
            "os_credential_file": str(service.config.credential_path),
            "homes": ["main"],
        }
    )
    try:
        yield service, config
    finally:
        service.stop()


@pytest.fixture
def mcp_harness(tmp_path):
    """Reuse the existing command-only OS integration fixture and cleanup."""
    path = Path(__file__).parents[1] / "integration/conftest.py"
    spec = importlib.util.spec_from_file_location("mcp_router_support", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    harness = module.RouterHarness(tmp_path / "harness")
    try:
        yield harness
    finally:
        harness.stop()
