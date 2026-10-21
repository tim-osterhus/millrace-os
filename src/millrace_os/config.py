"""Configuration and local path ownership for the Millrace OS service."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from .paths import ensure_directory


class ConfigurationError(ValueError):
    """Raised when configuration would violate the local-only boundary."""

    code = "configuration_error"


def _xdg_path(name: str, fallback: str, environ: dict[str, str]) -> Path:
    value = environ.get(name)
    return Path(value).expanduser() if value else Path(fallback).expanduser()


def _parse_port(value: str | None) -> int:
    if value is None or value == "":
        return 8731
    try:
        port = int(value)
    except ValueError as exc:
        raise ConfigurationError(
            "port must be a nonzero integer from 1 through 65535"
        ) from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("port must be a nonzero integer from 1 through 65535")
    return port


def _validate_loopback(host: str) -> str:
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigurationError("host must be a loopback address") from exc
    if not address.is_loopback:
        raise ConfigurationError("host must be a loopback address")
    return host


def _endpoint_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    """Resolved paths and bind settings for one local service instance."""

    config_dir: Path
    state_dir: Path
    credential_path: Path
    host: str = "127.0.0.1"
    port: int = 8731
    socket_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_dir", Path(self.config_dir).expanduser())
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser())
        object.__setattr__(
            self, "credential_path", Path(self.credential_path).expanduser()
        )
        object.__setattr__(self, "host", _validate_loopback(self.host))
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ConfigurationError(
                "port must be a nonzero integer from 1 through 65535"
            )
        if not 1 <= self.port <= 65535:
            raise ConfigurationError(
                "port must be a nonzero integer from 1 through 65535"
            )
        if self.socket_path is not None:
            object.__setattr__(self, "socket_path", Path(self.socket_path).expanduser())

    @classmethod
    def from_environment(
        cls, environ: dict[str, str] | None = None
    ) -> ApplicationConfig:
        values = dict(os.environ if environ is None else environ)
        config_home = _xdg_path("XDG_CONFIG_HOME", "~/.config", values)
        state_home = _xdg_path("XDG_STATE_HOME", "~/.local/state", values)
        config_dir = Path(
            values.get("MILLRACE_OS_CONFIG_DIR", config_home / "millrace-os")
        )
        state_dir = Path(
            values.get("MILLRACE_OS_STATE_DIR", state_home / "millrace-os")
        )
        credential_path = Path(
            values.get("MILLRACE_OS_CREDENTIAL_PATH", config_dir / "credentials.json")
        )
        socket_value = values.get("MILLRACE_OS_SOCKET")
        if socket_value:
            raise ConfigurationError(
                "Unix socket binding is not enabled by the pinned Release 1 contract"
            )
        return cls(
            config_dir=config_dir,
            state_dir=state_dir,
            credential_path=credential_path,
            host=_validate_loopback(values.get("MILLRACE_OS_HOST", "127.0.0.1")),
            port=_parse_port(values.get("MILLRACE_OS_PORT")),
            socket_path=None,
        )

    @property
    def database_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def pid_path(self) -> Path:
        return self.state_dir / "millrace-os.pid"

    @property
    def browser_endpoint(self) -> str:
        return f"http://{_endpoint_host(self.host)}:{self.port}/"

    def ensure_directories(self) -> None:
        for path in (self.config_dir, self.state_dir):
            ensure_directory(path)
