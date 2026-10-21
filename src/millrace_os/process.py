"""Fail-closed identity and stop helpers for the OS service process."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import UnsafePathError, open_new_file, validate_file_target


class ProcessIdentityError(RuntimeError):
    """Raised when a PID cannot be tied to the recorded OS process."""

    code = "process_identity_error"


class ProcessNotFoundError(ProcessIdentityError):
    """Raised when the recorded PID is proven to be absent."""

    code = "process_not_found"


class ProcessStopError(ProcessIdentityError):
    """Raised when a verified process does not stop within the deadline."""

    code = "process_stop_error"


def _require_supported_platform() -> None:
    # The identity contract is intentionally narrow.  A future platform must
    # add and qualify its own identity primitive before stop can signal a PID.
    if sys.platform != "darwin":
        raise ProcessIdentityError("process identity is unsupported on this platform")


class _BSDInfo(ctypes.Structure):
    # Darwin sys/proc_info.h PROC_PIDTBSDINFO, including microsecond birth time.
    _fields_ = [
        ("prefix", ctypes.c_uint32 * 12),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("suffix", ctypes.c_uint32 * 6),
        ("seconds", ctypes.c_uint64),
        ("microseconds", ctypes.c_uint64),
    ]


def _birth(pid: int) -> str:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    function.restype = ctypes.c_int
    info = _BSDInfo()
    size = function(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        if size == 0 and ctypes.get_errno() == errno.ESRCH:
            raise ProcessNotFoundError(f"process {pid} is absent")
        raise ProcessIdentityError(f"native identity unavailable for process {pid}")
    if info.prefix[3] != pid or not info.seconds:
        raise ProcessIdentityError("native process identity is malformed")
    if info.prefix[1] == 5:  # SZOMB: exited, waiting for its parent to reap.
        raise ProcessNotFoundError(f"process {pid} has exited")
    return f"{info.seconds}:{info.microseconds:06d}"


def _ps_identity(pid: int) -> tuple[int, int, int, str, str, str]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "pid=,uid=,ppid=,lstart=,args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcessIdentityError(f"timed out inspecting process {pid}") from exc
    if result.returncode != 0:
        raise ProcessIdentityError(f"could not inspect process {pid}")
    fields = result.stdout.split(maxsplit=8)
    if len(fields) != 9:
        raise ProcessIdentityError(f"process {pid} identity is malformed")
    try:
        observed_pid = int(fields[0])
        uid = int(fields[1])
        parent_pid = int(fields[2])
    except ValueError as exc:
        raise ProcessIdentityError(f"process {pid} identity is malformed") from exc
    start_time = " ".join(fields[3:8])
    arguments = fields[8].strip()
    if not start_time or not arguments:
        raise ProcessIdentityError(f"process {pid} identity is malformed")
    return (
        observed_pid,
        uid,
        parent_pid,
        arguments.split(maxsplit=1)[0],
        start_time,
        arguments,
    )


def _process_identity(pid: int) -> ProcessIdentity:
    _require_supported_platform()
    if pid <= 0:
        raise ProcessIdentityError("process id must be positive")
    birth = _birth(pid)
    observed_pid, uid, parent_pid, executable, _start_time, arguments = _ps_identity(
        pid
    )
    if _birth(pid) != birth:
        raise ProcessIdentityError("process changed during observation")
    if observed_pid != pid:
        raise ProcessIdentityError(f"process {pid} identity changed")
    return ProcessIdentity(
        pid=pid,
        uid=uid,
        parent_pid=parent_pid,
        executable=executable,
        arguments=arguments,
        start_time=birth,
    )


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A PID plus macOS identity fields that fence PID reuse."""

    pid: int
    uid: int
    parent_pid: int
    executable: str
    arguments: str
    start_time: str

    @classmethod
    def current(cls, pid: int | None = None) -> ProcessIdentity:
        return _process_identity(os.getpid() if pid is None else pid)

    @classmethod
    def from_mapping(cls, value: object) -> ProcessIdentity:
        if not isinstance(value, dict):
            raise ProcessIdentityError("process identity record is malformed")
        try:
            identity = cls(
                pid=int(value["pid"]),
                uid=int(value["uid"]),
                parent_pid=int(value["parent_pid"]),
                executable=str(value["executable"]),
                arguments=str(value["arguments"]),
                start_time=str(value["start_time"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProcessIdentityError("process identity record is malformed") from exc
        if identity.pid <= 0 or identity.uid < 0 or identity.parent_pid < 0:
            raise ProcessIdentityError("process identity record is malformed")
        if not all(
            isinstance(item, str) and item
            for item in (
                identity.executable,
                identity.arguments,
                identity.start_time,
            )
        ):
            raise ProcessIdentityError("process identity record is malformed")
        return identity

    def as_mapping(self) -> dict[str, object]:
        return asdict(self)

    def matches(self, observed: ProcessIdentity) -> bool:
        return self == observed


def read_identity(path: Path | str) -> ProcessIdentity:
    """Read a safe JSON identity record; legacy PID-only files are refused."""

    try:
        _, exists = validate_file_target(path)
    except UnsafePathError as exc:
        raise ProcessIdentityError(str(exc)) from exc
    if not exists:
        raise ProcessIdentityError("daemon identity record is missing")
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ProcessIdentityError("daemon identity record is malformed") from exc
    return ProcessIdentity.from_mapping(value)


def write_identity(path: Path | str, identity: ProcessIdentity) -> Path:
    """Atomically create an owner-only identity record."""

    descriptor, target = open_new_file(path)
    try:
        payload = json.dumps(identity.as_mapping(), sort_keys=True).encode() + b"\n"
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return target


def observe_identity(identity: ProcessIdentity) -> ProcessIdentity:
    """Observe the process and return a fresh identity snapshot."""

    return _process_identity(identity.pid)


def verify_identity(identity: ProcessIdentity) -> None:
    """Refuse unless every recorded field still matches the live process."""

    observed = observe_identity(identity)
    if not identity.matches(observed):
        raise ProcessIdentityError("recorded daemon identity does not match process")
    words = shlex.split(observed.arguments)
    is_os_serve = any(
        (Path(word).name == "millrace-os" or word == "millrace_os.cli")
        and index + 1 < len(words)
        and words[index + 1] == "serve"
        for index, word in enumerate(words)
    )
    if not is_os_serve:
        raise ProcessIdentityError("recorded process is not an OS serve command")
    if observed.uid != os.getuid():
        raise ProcessIdentityError("recorded daemon is not owned by the current user")


def wait_for_exit(identity: ProcessIdentity, timeout: float = 5.0) -> bool:
    """Wait for the exact recorded process to disappear."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            observed = observe_identity(identity)
        except ProcessNotFoundError:
            return True
        if not identity.matches(observed):
            raise ProcessIdentityError("process identity changed while stopping")
        time.sleep(0.05)
    try:
        observed = observe_identity(identity)
    except ProcessNotFoundError:
        return True
    if identity.matches(observed):
        return False
    raise ProcessIdentityError("process identity changed while stopping")


def stop_verified(identity: ProcessIdentity, timeout: float = 5.0) -> None:
    """Verify, signal, and verify exit for one exact OS-owned process."""

    verify_identity(identity)
    try:
        os.kill(identity.pid, signal.SIGTERM)
    except OSError as exc:
        raise ProcessStopError(f"could not signal daemon {identity.pid}") from exc
    if not wait_for_exit(identity, timeout):
        raise ProcessStopError(f"daemon {identity.pid} did not stop in time")
