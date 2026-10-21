"""Bounded, read-only stock-tmux provider.

This module is deliberately a small subprocess reader.  It observes one
configured tmux server and one shared session through stock format fields,
without creating a server/session or interpreting terminal output.  All
provider-private facts stay in this module; later OS packets may project them
through an explicitly qualified public adapter contract.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

DEFAULT_TMUX_EXECUTABLE: Final[str] = "/opt/homebrew/Cellar/tmux/3.7c/bin/tmux"
DEFAULT_TMUX_SHA256: Final[str] = (
    "1d101560ca3d4fb6f86bb53e6d9946e7b98444343d1fdab4a2a6e72980e232a5"
)
DEFAULT_SESSION_NAME: Final[str] = "millrace"
WORKSPACE_OPTION: Final[str] = "@millrace_workspace_id"
INSTANCE_OPTION: Final[str] = "@millrace_instance_id"
# tmux's format renderer replaces control characters in format output.  Keep
# the separator printable and reject it in fields that must be parsed rather
# than accepting an ambiguous inventory.
FIELD_SEPARATOR: Final[str] = "|"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_VERSION = re.compile(r"^tmux\s+(\d+)\.(\d+)(?:([A-Za-z0-9.-]+))?$")


class TmuxProviderError(RuntimeError):
    """Base for typed, fail-closed provider errors."""

    code = "tmux_provider_error"
    category = "availability"
    retryable = False

    def __init__(self, detail: str = "") -> None:
        self.detail = detail[:256]
        message = self.code if not self.detail else f"{self.code}: {self.detail}"
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class TmuxConfigurationError(TmuxProviderError):
    code = "tmux_invalid_configuration"
    category = "validation"


class TmuxExecutableMissing(TmuxProviderError):
    code = "tmux_executable_missing"
    retryable = False


class TmuxExecutableMismatch(TmuxProviderError):
    code = "tmux_executable_mismatch"
    category = "validation"


class TmuxVersionUnsupported(TmuxProviderError):
    code = "tmux_version_unsupported"
    category = "validation"


class TmuxServerUnavailable(TmuxProviderError):
    code = "tmux_server_unavailable"
    retryable = True


class TmuxSocketError(TmuxProviderError):
    code = "tmux_socket_error"
    retryable = True


class TmuxSessionUnavailable(TmuxProviderError):
    code = "tmux_session_unavailable"
    retryable = True


class TmuxCommandFailed(TmuxProviderError):
    code = "tmux_command_failed"
    retryable = True


class TmuxCommandTimeout(TmuxProviderError):
    code = "tmux_command_timeout"
    retryable = True


class TmuxOutputOverflow(TmuxProviderError):
    code = "tmux_output_overflow"
    category = "validation"


class TmuxMalformedOutput(TmuxProviderError):
    code = "tmux_malformed_output"
    category = "validation"


class TmuxTopologyError(TmuxProviderError):
    code = "tmux_invalid_topology"
    category = "validation"


class TmuxMappingError(TmuxProviderError):
    code = "tmux_invalid_mapping"
    category = "validation"


class TmuxDuplicateMapping(TmuxMappingError):
    code = "tmux_duplicate_mapping"


class TmuxTargetNotFound(TmuxMappingError):
    code = "tmux_target_not_found"
    retryable = True


class TmuxStaleTarget(TmuxMappingError):
    code = "tmux_stale_target"
    retryable = True


class TmuxServerRestarted(TmuxStaleTarget):
    code = "tmux_server_restarted"


@dataclass(frozen=True, slots=True)
class TmuxConfig:
    """Explicit stock-tmux connection configuration.

    ``socket_path`` is required so an omitted argument cannot silently use
    tmux's default server.  ``expected_sha256`` may be set to ``None`` only by
    a caller that is deliberately testing another executable; the production
    default is the pinned manager binary.
    """

    socket_path: str | Path
    executable: str | Path = DEFAULT_TMUX_EXECUTABLE
    session_name: str = DEFAULT_SESSION_NAME
    expected_sha256: str | None = DEFAULT_TMUX_SHA256
    minimum_version: tuple[int, int] = (3, 7)
    timeout_seconds: float = 2.0
    max_output_bytes: int = 256 * 1024
    cwd: str | Path | None = None
    environment: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        executable = Path(self.executable)
        socket_path = Path(self.socket_path)
        if not executable.is_absolute() or not str(executable):
            raise TmuxConfigurationError("executable must be an absolute path")
        if not socket_path.is_absolute() or not str(socket_path):
            raise TmuxConfigurationError("socket_path must be an absolute path")
        if "\x00" in str(executable) or "\x00" in str(socket_path):
            raise TmuxConfigurationError("paths may not contain NUL")
        if not self.session_name or len(self.session_name) > 96:
            raise TmuxConfigurationError("session_name is empty or too long")
        if any(char in self.session_name for char in "\x00\r\n"):
            raise TmuxConfigurationError("session_name contains a control character")
        if (
            len(self.minimum_version) != 2
            or any(
                not isinstance(part, int) or isinstance(part, bool)
                for part in self.minimum_version
            )
            or self.minimum_version[0] < 0
            or self.minimum_version[1] < 0
        ):
            raise TmuxConfigurationError("minimum_version must be a non-negative pair")
        if not 0 < self.timeout_seconds <= 30:
            raise TmuxConfigurationError("timeout_seconds must be between 0 and 30")
        if not 0 < self.max_output_bytes <= 4 * 1024 * 1024:
            raise TmuxConfigurationError("max_output_bytes is outside its bound")
        if self.expected_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.expected_sha256
        ):
            raise TmuxConfigurationError("expected_sha256 must be lowercase hex")
        if self.cwd is not None and not Path(self.cwd).is_absolute():
            raise TmuxConfigurationError("cwd must be an absolute path")


@dataclass(frozen=True, slots=True)
class TmuxVersion:
    text: str
    major: int
    minor: int
    suffix: str = ""

    @property
    def number(self) -> tuple[int, int]:
        return self.major, self.minor


@dataclass(frozen=True, slots=True)
class TmuxServerIdentity:
    socket_path: str
    pid: int
    version: str
    version_sha256: str
    session_created: int

    @property
    def generation(self) -> str:
        """Stable identity for one observed server incarnation."""

        return (
            f"{self.socket_path}|{self.pid}|{self.session_created}|{self.version}|"
            f"{self.version_sha256}"
        )

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["generation"] = self.generation
        return result


@dataclass(frozen=True, slots=True)
class TmuxSessionFact:
    session_id: str
    name: str
    created: int
    window_count: int
    server_generation: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TmuxWindowFact:
    window_id: str
    index: int
    name: str
    pane_count: int
    session_id: str
    workspace_id: str | None
    instance_id: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TmuxPaneFact:
    pane_id: str
    index: int
    pid: int
    dead: bool
    dead_status: int | None
    exit_status: int | None
    window_id: str
    session_id: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TmuxMapping:
    """Stable identity mapping plus independent tmux process facts."""

    workspace_id: str
    instance_id: str
    session_id: str
    session_name: str
    window_id: str
    window_index: int
    window_name: str
    pane_id: str
    pane_index: int
    pane_pid: int
    pane_dead: bool
    pane_dead_status: int | None
    pane_exit_status: int | None
    server_generation: str
    observed_at: str

    @property
    def process_fact(self) -> str:
        """The pane fact has no workflow meaning."""

        if self.pane_dead:
            return "dead"
        return "live"

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["process_fact"] = self.process_fact
        return result


@dataclass(frozen=True, slots=True)
class TmuxInventory:
    server: TmuxServerIdentity
    session: TmuxSessionFact
    windows: tuple[TmuxWindowFact, ...]
    panes: tuple[TmuxPaneFact, ...]
    mappings: tuple[TmuxMapping, ...]
    observed_at: str
    freshness: str = "current"
    source_provider: str = "tmux"

    def mapping_for(self, workspace_id: str, instance_id: str) -> TmuxMapping:
        _validate_id(workspace_id, "workspace_id")
        _validate_id(instance_id, "instance_id")
        matches = tuple(
            mapping
            for mapping in self.mappings
            if mapping.workspace_id == workspace_id
            and mapping.instance_id == instance_id
        )
        if len(matches) > 1:
            raise TmuxDuplicateMapping(
                f"workspace/instance pair {workspace_id}/{instance_id} is ambiguous"
            )
        if not matches:
            raise TmuxTargetNotFound(
                f"no fresh mapping for workspace/instance {workspace_id}/{instance_id}"
            )
        return matches[0]

    def as_dict(self) -> dict[str, object]:
        return {
            "server": self.server.as_dict(),
            "session": self.session.as_dict(),
            "windows": [item.as_dict() for item in self.windows],
            "panes": [item.as_dict() for item in self.panes],
            "mappings": [item.as_dict() for item in self.mappings],
            "observed_at": self.observed_at,
            "freshness": self.freshness,
            "source_provider": self.source_provider,
        }


@dataclass(frozen=True, slots=True)
class TmuxHealth:
    provider_id: str = "tmux"
    available: bool = False
    state: str = "unavailable"
    reason: str | None = None
    executable: str = ""
    executable_sha256: str | None = None
    version: str | None = None
    socket_path: str = ""
    session_name: str = ""
    server_generation: str | None = None
    observed_at: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AttachDescriptor:
    """A copyable native target; it has no execution method."""

    argv: tuple[str, ...]
    display: str
    socket_path: str
    server_generation: str
    session_id: str
    session_name: str
    window_id: str
    pane_id: str
    workspace_id: str
    instance_id: str
    observation_id: str
    observed_at: str

    @property
    def argv_list(self) -> list[str]:
        return list(self.argv)

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["argv"] = list(self.argv)
        result["argv_list"] = list(self.argv)
        return result


@dataclass(frozen=True, slots=True)
class ProviderHint:
    reason: str
    observed_at: str
    requires_reconcile: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise TmuxMappingError(f"{field} is not a bounded stable identifier")


def _int(value: str, field: str, *, allow_empty: bool = False) -> int | None:
    if not value and allow_empty:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TmuxMalformedOutput(f"{field} is not an integer") from exc
    if result < 0:
        raise TmuxMalformedOutput(f"{field} is negative")
    return result


def _bool(value: str, field: str) -> bool:
    if value == "0":
        return False
    if value == "1":
        return True
    raise TmuxMalformedOutput(f"{field} is not 0 or 1")


def _rows(raw: str, fields: int, command: str) -> list[list[str]]:
    if not raw:
        return []
    result: list[list[str]] = []
    for line in raw.splitlines():
        parts = line.split(FIELD_SEPARATOR)
        if len(parts) != fields:
            raise TmuxMalformedOutput(f"{command} returned an invalid field count")
        result.append(parts)
    return result


_SESSION_FIELDS = 7
_WINDOW_FIELDS = 10
_PANE_FIELDS = 9
_SESSION_FORMAT = FIELD_SEPARATOR.join(
    (
        "#{session_id}",
        "#{session_name}",
        "#{session_created}",
        "#{session_windows}",
        "#{socket_path}",
        "#{pid}",
        "#{version}",
    )
)
_WINDOW_FORMAT = FIELD_SEPARATOR.join(
    (
        "#{window_id}",
        "#{window_index}",
        "#{window_name}",
        "#{window_panes}",
        "#{session_id}",
        "#{session_name}",
        "#{socket_path}",
        "#{pid}",
        "#{@millrace_workspace_id}",
        "#{@millrace_instance_id}",
    )
)
_PANE_FORMAT = FIELD_SEPARATOR.join(
    (
        "#{pane_id}",
        "#{pane_index}",
        "#{pane_pid}",
        "#{pane_dead}",
        "#{pane_dead_status}",
        "#{pane_exit_status}",
        "#{window_id}",
        "#{session_id}",
        "#{pid}",
    )
)


class TmuxProvider:
    """Read one configured stock-tmux server and shared session."""

    provider_id = "tmux"

    def __init__(
        self,
        config: TmuxConfig | None = None,
        *,
        socket_path: str | Path | None = None,
        executable: str | Path = DEFAULT_TMUX_EXECUTABLE,
        session_name: str = DEFAULT_SESSION_NAME,
        expected_sha256: str | None = DEFAULT_TMUX_SHA256,
        timeout_seconds: float = 2.0,
        max_output_bytes: int = 256 * 1024,
        cwd: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if config is not None and any(
            value is not None
            for value in (socket_path, cwd, environment)
        ):
            raise TmuxConfigurationError(
                "config cannot be combined with convenience keyword arguments"
            )
        if config is None:
            if socket_path is None:
                raise TmuxConfigurationError(
                    "socket_path is required; the default tmux server is forbidden"
                )
            config = TmuxConfig(
                socket_path=socket_path,
                executable=executable,
                session_name=session_name,
                expected_sha256=expected_sha256,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                cwd=cwd,
                environment=environment,
            )
        self.config = config
        self._last_inventory: TmuxInventory | None = None
        self._last_server_generation: str | None = None
        self._server_was_unavailable = False
        self._hint: ProviderHint | None = None
        self._state = "unavailable"

    @property
    def state(self) -> str:
        """Current lifecycle state of the replaceable provider projection."""

        return self._state

    @property
    def last_inventory(self) -> TmuxInventory | None:
        """Latest replaceable projection; callers must reconcile for authority."""

        return self._last_inventory

    def hint(self, reason: str = "tmux-change") -> ProviderHint:
        """Record an event hint; it never makes cached data authoritative."""

        self._hint = ProviderHint(reason=reason, observed_at=_now())
        self._state = "reconciling"
        return self._hint

    mark_dirty = hint

    def reset(self) -> None:
        """Reset adapter state without touching tmux."""

        self._last_inventory = None
        self._last_server_generation = None
        self._server_was_unavailable = False
        self._hint = None
        self._state = "reconciling"

    def health(self) -> TmuxHealth:
        """Return typed availability without raising for expected degradation."""

        observed_at = _now()
        executable = str(Path(self.config.executable))
        socket_path = str(Path(self.config.socket_path))
        try:
            version, digest = self._verify_executable()
            sessions = self._read_sessions()
            server = self._server_identity(sessions[0], version, digest)
            server, session = self._select_session(sessions, server=server)
            self._state = "healthy"
            return TmuxHealth(
                available=True,
                state="healthy",
                executable=executable,
                executable_sha256=digest,
                version=version.text,
                socket_path=socket_path,
                session_name=self.config.session_name,
                server_generation=server.generation,
                observed_at=observed_at,
            )
        except TmuxProviderError as exc:
            state = (
                "unavailable" if exc.category == "availability" else "degraded"
            )
            self._state = state
            return TmuxHealth(
                available=False,
                state=state,
                reason=exc.code,
                executable=executable,
                socket_path=socket_path,
                session_name=self.config.session_name,
                observed_at=observed_at,
            )

    def reconcile(self, *, hint: str | None = None) -> TmuxInventory:
        """Read fresh server/session/window/pane facts from stock tmux."""

        del hint  # A hint only changes why the caller requested this fresh read.
        observed_at = _now()
        try:
            version, digest = self._verify_executable()
            server_rows = self._run(
                ("list-sessions", "-F", _SESSION_FORMAT), command="list-sessions"
            )
            rows = _rows(server_rows, _SESSION_FIELDS, "list-sessions")
            if not rows:
                raise TmuxSessionUnavailable("shared session is not present")
            identities = {
                self._server_identity(row, version, digest) for row in rows
            }
            if len(identities) != 1:
                raise TmuxMalformedOutput("server identity differs between rows")
            server = next(iter(identities))
            if self._last_server_generation and (
                self._last_server_generation != server.generation
            ):
                # The fresh inventory is still valid.  A descriptor from the
                # previous generation will fail validation below.
                self._server_was_unavailable = False
            self._last_server_generation = server.generation
            server, session = self._select_session(rows, server=server)
            windows = self._read_windows(server, session)
            panes = self._read_panes(server, session)
            inventory = self._build_inventory(
                server, session, windows, panes, observed_at
            )
            self._last_inventory = inventory
            self._server_was_unavailable = False
            self._hint = None
            self._state = "healthy"
            return inventory
        except TmuxServerUnavailable:
            self._server_was_unavailable = True
            self._state = "unavailable"
            raise
        except TmuxProviderError as exc:
            self._state = (
                "unavailable" if exc.category == "availability" else "degraded"
            )
            raise

    def attach_descriptor(
        self, workspace_id: str, instance_id: str
    ) -> AttachDescriptor:
        """Build a safe, copyable descriptor from a fresh inventory."""

        inventory = self.reconcile()
        mapping = inventory.mapping_for(workspace_id, instance_id)
        return self._descriptor(mapping, inventory)

    get_attach_descriptor = attach_descriptor
    build_attach_descriptor = attach_descriptor

    def validate_attach_descriptor(
        self, descriptor: AttachDescriptor
    ) -> AttachDescriptor:
        """Refuse a descriptor whose live target or server generation changed."""

        if not isinstance(descriptor, AttachDescriptor):
            raise TmuxStaleTarget("descriptor has an unsupported type")
        current = self.reconcile()
        if descriptor.socket_path != str(Path(self.config.socket_path)):
            raise TmuxStaleTarget("descriptor socket differs from configured socket")
        if descriptor.server_generation != current.server.generation:
            raise TmuxServerRestarted(
                "descriptor belongs to an older server generation"
            )
        try:
            mapping = current.mapping_for(
                descriptor.workspace_id, descriptor.instance_id
            )
        except TmuxTargetNotFound as exc:
            raise TmuxStaleTarget("descriptor mapping is no longer present") from exc
        expected = self._descriptor(mapping, current)
        if (
            descriptor.session_id,
            descriptor.window_id,
            descriptor.pane_id,
            descriptor.observation_id,
        ) != (
            expected.session_id,
            expected.window_id,
            expected.pane_id,
            expected.observation_id,
        ):
            raise TmuxStaleTarget("descriptor target identity no longer matches")
        return descriptor

    validate_attach = validate_attach_descriptor

    def _verify_executable(self) -> tuple[TmuxVersion, str]:
        path = Path(self.config.executable)
        try:
            if not path.is_file():
                raise TmuxExecutableMissing(str(path))
            digest = _sha256(path)
        except TmuxProviderError:
            raise
        except OSError as exc:
            raise TmuxExecutableMissing(str(path)) from exc
        if (
            self.config.expected_sha256 is not None
            and digest != self.config.expected_sha256
        ):
            raise TmuxExecutableMismatch(str(path))
        raw = self._run(("-V",), command="version")
        text = raw.strip()
        match = _VERSION.fullmatch(text)
        if match is None:
            raise TmuxMalformedOutput("version output is not stock tmux version text")
        major, minor = int(match.group(1)), int(match.group(2))
        version = TmuxVersion(
            text=text, major=major, minor=minor, suffix=match.group(3) or ""
        )
        if version.number < self.config.minimum_version:
            raise TmuxVersionUnsupported(version.text)
        return version, digest

    def _environment(self) -> dict[str, str]:
        # Start from no inherited process/session variables.  Caller-provided
        # values may add ordinary locale/runtime settings but never TMUX state.
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "TERM": "xterm-256color",
        }
        if self.config.environment:
            environment.update(
                {str(key): str(value) for key, value in self.config.environment.items()}
            )
        for key in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"):
            environment.pop(key, None)
        return environment

    def _run(self, args: Sequence[str], *, command: str) -> str:
        argv = [str(self.config.executable), "-S", str(self.config.socket_path), *args]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                env=self._environment(),
            )
        except FileNotFoundError as exc:
            raise TmuxExecutableMissing(str(self.config.executable)) from exc
        except PermissionError as exc:
            raise TmuxExecutableMissing(str(self.config.executable)) from exc
        except OSError as exc:
            raise TmuxSocketError(f"could not start {command}") from exc
        try:
            stdout, stderr = self._read_output(process, command)
        except (TmuxCommandTimeout, TmuxOutputOverflow):
            raise
        try:
            output = stdout.decode("utf-8")
            error = stderr.decode("utf-8", errors="replace").strip()
        except UnicodeDecodeError as exc:
            raise TmuxMalformedOutput(f"{command} output is not UTF-8") from exc
        if process.returncode:
            lower = error.lower()
            if any(
                marker in lower
                for marker in (
                    "no server running",
                    "error connecting",
                    "failed to connect",
                    "connection refused",
                )
            ):
                raise TmuxServerUnavailable(error)
            if "can't find session" in lower or "no such session" in lower:
                raise TmuxSessionUnavailable(error)
            if "can't find window" in lower or "can't find pane" in lower:
                raise TmuxTargetNotFound(error)
            raise TmuxCommandFailed(error or command)
        return output

    def _read_output(
        self, process: subprocess.Popen[bytes], command: str
    ) -> tuple[bytes, bytes]:
        """Read both pipes with a deadline and a combined byte bound."""

        stdout_pipe = getattr(process, "stdout", None)
        stderr_pipe = getattr(process, "stderr", None)
        # Keeping this small fallback makes the subprocess seam easy to test
        # with a process double that only implements communicate(). Real
        # Popen instances always expose both pipes because _run requested
        # PIPE for each stream.
        if stdout_pipe is None or stderr_pipe is None:
            try:
                stdout, stderr = process.communicate(
                    timeout=self.config.timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                raise TmuxCommandTimeout(command) from exc
            if len(stdout) + len(stderr) > self.config.max_output_bytes:
                raise TmuxOutputOverflow(command)
            return stdout, stderr

        selector = selectors.DefaultSelector()
        streams = ((stdout_pipe, "stdout"), (stderr_pipe, "stderr"))
        buffers: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        total = 0
        deadline = time.monotonic() + self.config.timeout_seconds
        for stream, label in streams:
            selector.register(stream, selectors.EVENT_READ, label)
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    raise TmuxCommandTimeout(command)
                events = selector.select(remaining)
                if not events:
                    self._terminate(process)
                    raise TmuxCommandTimeout(command)
                for key, _ in events:
                    try:
                        chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > self.config.max_output_bytes:
                        self._terminate(process)
                        raise TmuxOutputOverflow(command)
                    buffers[key.data].append(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate(process)
                raise TmuxCommandTimeout(command)
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                raise TmuxCommandTimeout(command) from exc
            return b"".join(buffers["stdout"]), b"".join(buffers["stderr"])
        finally:
            selector.close()

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """Stop a timed-out/overflowing child without draining unbounded data."""

        kill = getattr(process, "kill", None)
        if kill is not None:
            try:
                kill()
            except ProcessLookupError:
                pass
        for stream_name in ("stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        wait = getattr(process, "wait", None)
        if wait is not None:
            try:
                wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    kill()
                except ProcessLookupError:
                    pass

    def _read_sessions(self) -> list[list[str]]:
        raw = self._run(
            ("list-sessions", "-F", _SESSION_FORMAT), command="list-sessions"
        )
        rows = _rows(raw, _SESSION_FIELDS, "list-sessions")
        if not rows:
            raise TmuxSessionUnavailable("shared session is not present")
        return rows

    def _server_identity(
        self, row: list[str], version: TmuxVersion, digest: str
    ) -> TmuxServerIdentity:
        if row[4] != str(Path(self.config.socket_path)):
            raise TmuxSocketError("tmux reported a different socket path")
        pid = _int(row[5], "server pid")
        created = _int(row[2], "session creation time")
        windows = _int(row[3], "session window count")
        if pid is None or pid == 0 or created is None or windows is None:
            raise TmuxMalformedOutput("server identity contains an empty field")
        format_version = f"{version.major}.{version.minor}{version.suffix}"
        if row[6] != format_version:
            raise TmuxMalformedOutput("session version differs from version probe")
        return TmuxServerIdentity(
            socket_path=row[4],
            pid=pid,
            version=row[6],
            version_sha256=digest,
            session_created=created,
        )

    def _select_session(
        self,
        rows: list[list[str]],
        *,
        server: TmuxServerIdentity | None = None,
    ) -> tuple[TmuxServerIdentity, TmuxSessionFact]:
        if server is None:
            version_text = "tmux " + rows[0][6]
            match = _VERSION.fullmatch(version_text)
            if match is None:
                raise TmuxMalformedOutput("session version is malformed")
            version = TmuxVersion(
                text=version_text,
                major=int(match.group(1)),
                minor=int(match.group(2)),
                suffix=match.group(3) or "",
            )
            server = self._server_identity(rows[0], version, "")
        selected = [row for row in rows if row[1] == self.config.session_name]
        if len(selected) != 1:
            raise TmuxSessionUnavailable(
                f"expected one shared session named {self.config.session_name}"
            )
        row = selected[0]
        created = _int(row[2], "session creation time")
        windows = _int(row[3], "session window count")
        if created is None or windows is None:
            raise TmuxMalformedOutput("session fact contains an empty field")
        return server, TmuxSessionFact(
            session_id=row[0],
            name=row[1],
            created=created,
            window_count=windows,
            server_generation=server.generation,
        )

    def _read_windows(
        self, server: TmuxServerIdentity, session: TmuxSessionFact
    ) -> list[TmuxWindowFact]:
        raw = self._run(
            ("list-windows", "-t", session.session_id, "-F", _WINDOW_FORMAT),
            command="list-windows",
        )
        rows = _rows(raw, _WINDOW_FIELDS, "list-windows")
        windows: list[TmuxWindowFact] = []
        for row in rows:
            if row[4] != session.session_id or row[5] != session.name:
                raise TmuxStaleTarget("window belongs to another session")
            if (
                row[6] != server.socket_path
                or _int(row[7], "window server pid") != server.pid
            ):
                raise TmuxServerRestarted("window server identity changed during read")
            index = _int(row[1], "window index")
            pane_count = _int(row[3], "window pane count")
            if index is None or pane_count is None:
                raise TmuxMalformedOutput("window fact contains an empty field")
            workspace_id = row[8] or None
            instance_id = row[9] or None
            if (workspace_id is None) != (instance_id is None):
                raise TmuxMappingError("window has only one stable identity option")
            if workspace_id is not None:
                _validate_id(workspace_id, "workspace_id")
                _validate_id(instance_id or "", "instance_id")
            windows.append(
                TmuxWindowFact(
                    window_id=row[0],
                    index=index,
                    name=row[2],
                    pane_count=pane_count,
                    session_id=row[4],
                    workspace_id=workspace_id,
                    instance_id=instance_id,
                )
            )
        if len(windows) != session.window_count:
            raise TmuxTopologyError("session window count changed during read")
        return windows

    def _read_panes(
        self, server: TmuxServerIdentity, session: TmuxSessionFact
    ) -> list[TmuxPaneFact]:
        raw = self._run(
            ("list-panes", "-a", "-F", _PANE_FORMAT),
            command="list-panes",
        )
        rows = _rows(raw, _PANE_FIELDS, "list-panes")
        panes: list[TmuxPaneFact] = []
        for row in rows:
            if row[7] != session.session_id:
                continue
            if row[8] != str(server.pid):
                raise TmuxServerRestarted("pane server identity changed during read")
            index = _int(row[1], "pane index")
            pid = _int(row[2], "pane pid")
            if index is None or pid is None or pid == 0:
                raise TmuxMalformedOutput("pane fact contains an empty field")
            dead = _bool(row[3], "pane dead")
            dead_status = _int(row[4], "pane dead status", allow_empty=True)
            exit_status = _int(row[5], "pane exit status", allow_empty=True)
            panes.append(
                TmuxPaneFact(
                    pane_id=row[0],
                    index=index,
                    pid=pid,
                    dead=dead,
                    dead_status=dead_status,
                    exit_status=exit_status,
                    window_id=row[6],
                    session_id=row[7],
                )
            )
        return panes

    def _build_inventory(
        self,
        server: TmuxServerIdentity,
        session: TmuxSessionFact,
        windows: list[TmuxWindowFact],
        panes: list[TmuxPaneFact],
        observed_at: str,
    ) -> TmuxInventory:
        by_window: dict[str, list[TmuxPaneFact]] = {}
        for pane in panes:
            by_window.setdefault(pane.window_id, []).append(pane)
        mappings: list[TmuxMapping] = []
        workspace_seen: dict[str, TmuxWindowFact] = {}
        instance_seen: dict[str, TmuxWindowFact] = {}
        for window in windows:
            window_panes = by_window.get(window.window_id, [])
            if len(window_panes) != window.pane_count:
                raise TmuxTopologyError(
                    f"window {window.window_id} pane count changed during read"
                )
            if (window.workspace_id is None) != (window.instance_id is None):
                raise TmuxMappingError("window has only one stable identity option")
            if window.workspace_id is None:
                continue
            _validate_id(window.workspace_id, "workspace_id")
            _validate_id(window.instance_id or "", "instance_id")
            if len(window_panes) != 1:
                raise TmuxTopologyError(
                    f"managed window {window.window_id} must contain one pane"
                )
            if window.workspace_id in workspace_seen:
                raise TmuxDuplicateMapping(
                    f"workspace_id {window.workspace_id} occurs in multiple windows"
                )
            if window.instance_id in instance_seen:
                raise TmuxDuplicateMapping(
                    f"instance_id {window.instance_id} occurs in multiple windows"
                )
            workspace_seen[window.workspace_id] = window
            instance_seen[window.instance_id or ""] = window
            pane = window_panes[0]
            mappings.append(
                TmuxMapping(
                    workspace_id=window.workspace_id,
                    instance_id=window.instance_id or "",
                    session_id=session.session_id,
                    session_name=session.name,
                    window_id=window.window_id,
                    window_index=window.index,
                    window_name=window.name,
                    pane_id=pane.pane_id,
                    pane_index=pane.index,
                    pane_pid=pane.pid,
                    pane_dead=pane.dead,
                    pane_dead_status=pane.dead_status,
                    pane_exit_status=pane.exit_status,
                    server_generation=server.generation,
                    observed_at=observed_at,
                )
            )
        return TmuxInventory(
            server=server,
            session=session,
            windows=tuple(windows),
            panes=tuple(panes),
            mappings=tuple(mappings),
            observed_at=observed_at,
        )

    def _descriptor(
        self, mapping: TmuxMapping, inventory: TmuxInventory
    ) -> AttachDescriptor:
        target = f"{mapping.session_id}:{mapping.window_id}.{mapping.pane_id}"
        argv = (
            str(self.config.executable),
            "-S",
            str(self.config.socket_path),
            "attach-session",
            "-t",
            target,
        )
        observation_id = ":".join(
            (
                inventory.server.generation,
                mapping.session_id,
                mapping.window_id,
                mapping.pane_id,
                mapping.workspace_id,
                mapping.instance_id,
            )
        )
        return AttachDescriptor(
            argv=argv,
            display=shlex.join(argv),
            socket_path=str(self.config.socket_path),
            server_generation=inventory.server.generation,
            session_id=mapping.session_id,
            session_name=mapping.session_name,
            window_id=mapping.window_id,
            pane_id=mapping.pane_id,
            workspace_id=mapping.workspace_id,
            instance_id=mapping.instance_id,
            observation_id=observation_id,
            observed_at=inventory.observed_at,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TmuxExecutableMissing(str(path)) from exc
    return digest.hexdigest()


__all__ = [
    "AttachDescriptor",
    "DEFAULT_SESSION_NAME",
    "DEFAULT_TMUX_EXECUTABLE",
    "DEFAULT_TMUX_SHA256",
    "INSTANCE_OPTION",
    "ProviderHint",
    "TmuxCommandFailed",
    "TmuxCommandTimeout",
    "TmuxConfig",
    "TmuxConfigurationError",
    "TmuxDuplicateMapping",
    "TmuxExecutableMismatch",
    "TmuxExecutableMissing",
    "TmuxHealth",
    "TmuxInventory",
    "TmuxMalformedOutput",
    "TmuxMapping",
    "TmuxMappingError",
    "TmuxOutputOverflow",
    "TmuxPaneFact",
    "TmuxProvider",
    "TmuxProviderError",
    "TmuxServerIdentity",
    "TmuxServerRestarted",
    "TmuxServerUnavailable",
    "TmuxSessionFact",
    "TmuxSessionUnavailable",
    "TmuxStaleTarget",
    "TmuxTopologyError",
    "TmuxTargetNotFound",
    "TmuxVersion",
    "TmuxVersionUnsupported",
    "TmuxWindowFact",
    "WORKSPACE_OPTION",
]
