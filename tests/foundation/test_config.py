from __future__ import annotations

import os
import stat

import pytest

from millrace_os.config import ApplicationConfig, ConfigurationError
from millrace_os.credentials import CredentialStore


def test_config_uses_xdg_defaults_and_fixed_loopback_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MILLRACE_OS_HOST", raising=False)
    monkeypatch.delenv("MILLRACE_OS_PORT", raising=False)

    config = ApplicationConfig.from_environment()

    assert config.host == "127.0.0.1"
    assert config.port == 8731
    assert config.browser_endpoint == "http://127.0.0.1:8731/"
    assert config.config_dir == tmp_path / "config" / "millrace-os"
    assert config.state_dir == tmp_path / "state" / "millrace-os"
    assert config.database_path == config.state_dir / "state.sqlite3"


def test_config_rejects_non_loopback_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MILLRACE_OS_HOST", "0.0.0.0")

    with pytest.raises(ConfigurationError, match="loopback") as error:
        ApplicationConfig.from_environment()

    assert error.value.code == "configuration_error"


def test_config_refuses_an_unix_socket_setting_until_its_contract_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MILLRACE_OS_SOCKET", "/tmp/millrace-os.sock")

    with pytest.raises(ConfigurationError, match="Unix socket"):
        ApplicationConfig.from_environment()


def test_config_rejects_port_zero_without_silent_ephemeral_binding() -> None:
    with pytest.raises(ConfigurationError, match="nonzero"):
        ApplicationConfig(
            config_dir="/tmp/millrace-config",
            state_dir="/tmp/millrace-state",
            credential_path="/tmp/millrace-config/credentials.json",
            port=0,
        )


def test_config_accepts_ipv6_loopback_as_an_explicit_local_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MILLRACE_OS_HOST", "::1")

    config = ApplicationConfig.from_environment()

    assert config.host == "::1"


def test_credential_store_is_owner_only(tmp_path) -> None:
    credentials = CredentialStore(tmp_path / "config" / "credentials.json")

    token = credentials.ensure()

    assert len(token) >= 32
    assert credentials.path.stat().st_mode & 0o777 == 0o600
    assert credentials.path.parent.stat().st_mode & 0o777 == 0o700
    assert credentials.ensure() == token


def test_credential_store_refuses_group_or_world_readable_file(tmp_path) -> None:
    credentials = CredentialStore(tmp_path / "config" / "credentials.json")
    credentials.ensure()
    os.chmod(credentials.path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)

    with pytest.raises(PermissionError, match="owner-only"):
        credentials.ensure()


def test_credential_store_refuses_symlink_and_dangling_parent(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PermissionError, match="symbolic link"):
        CredentialStore(link / "credentials.json").ensure()

    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(PermissionError, match="symbolic link"):
        CredentialStore(dangling / "credentials.json").ensure()


def test_router_homes_are_explicit_canonical_and_reject_aliases(tmp_path):
    values = {
        "MILLRACE_OS_CONFIG_DIR": str(tmp_path / "config"),
        "MILLRACE_OS_STATE_DIR": str(tmp_path / "state"),
        "MILLRACE_OS_ROUTER_STATE_HOMES": '{"primary":"'
        + str(tmp_path / "core")
        + '"}',
    }
    config = ApplicationConfig.from_environment(values)
    assert config.router_state_homes == {"primary": (tmp_path / "core").resolve()}
    assert not (tmp_path / "core").exists()
    for invalid in ('{"a":"one","a":"two"}', "[]", '{"a":1}'):
        with pytest.raises(ConfigurationError):
            ApplicationConfig.from_environment(
                {**values, "MILLRACE_OS_ROUTER_STATE_HOMES": invalid}
            )
    with pytest.raises(ConfigurationError, match="aliases"):
        ApplicationConfig(
            config_dir=tmp_path / "c",
            state_dir=tmp_path / "s",
            credential_path=tmp_path / "c/token",
            router_state_homes={"a": tmp_path / "core", "b": tmp_path / "core/../core"},
        )
    with pytest.raises(ConfigurationError, match="name"):
        ApplicationConfig(
            config_dir=tmp_path / "c",
            state_dir=tmp_path / "s",
            credential_path=tmp_path / "c/token",
            router_state_homes={"../arbitrary": tmp_path / "core"},
        )
