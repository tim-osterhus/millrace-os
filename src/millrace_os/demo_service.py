"""Owned foreground demo service; no daemon adoption or PID signaling."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import select
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .config import ApplicationConfig
from .contracts import DemoContext, strict_json
from .credentials import CredentialStore
from .paths import ensure_directory, open_new_file, validate_file_target
from .service import DemoContextPending, Service, ServiceDependencies

__all__ = ["DemoServiceHandle", "DemoServiceError", "start_demo_service"]

_RECORD_LIMIT = 4096
_START_TIMEOUT = 10.0
_DRAIN_TIMEOUT = 15.0
_CORE_ID = re.compile(r"[0-9a-f]{32}")


class DemoServiceError(RuntimeError):
    """The owned service could not be established or observed safely."""


def _record(value: object) -> bytes:
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(raw) > _RECORD_LIMIT:
        raise DemoServiceError("private demo record exceeds 4 KiB")
    return raw


def _read_record(stream: BinaryIO) -> dict[str, object]:
    raw = stream.readline(_RECORD_LIMIT + 1)
    if not raw:
        raise EOFError
    if len(raw) > _RECORD_LIMIT or not raw.endswith(b"\n"):
        raise DemoServiceError("invalid private demo record")
    value = strict_json(raw)
    if not isinstance(value, dict):
        raise DemoServiceError("invalid private demo record")
    return {key: item for key, item in value.items()}


def _write_record(descriptor: int, value: object) -> None:
    raw = _record(value)
    if os.write(descriptor, raw) != len(raw):
        raise DemoServiceError("incomplete private demo record")


def _private_root(root: Path) -> Path:
    root = Path(root).absolute()
    if root.resolve() != root:
        raise DemoServiceError("demo root must be canonical")
    ensure_directory(root)
    if root.stat().st_mode & 0o077:
        raise DemoServiceError("demo root must be private")
    marker, exists = validate_file_target(root / "owner.json")
    if not exists:
        raise DemoServiceError("demo owner marker is missing")
    with marker.open("rb") as stream:
        before = os.fstat(stream.fileno())
        owner = strict_json(stream.read(_RECORD_LIMIT + 1))
    after = marker.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise DemoServiceError("demo owner marker changed")
    if not isinstance(owner, dict) or owner.get("format") != "millrace-demo-owner/v1":
        raise DemoServiceError("invalid demo owner marker")
    identity = root.stat()
    if (
        owner.get("root") != str(root)
        or owner.get("root_device") != identity.st_dev
        or owner.get("root_inode") != identity.st_ino
    ):
        raise DemoServiceError("demo owner root does not match")
    for name in ("os-config", "os-state", "router-state", "logs"):
        ensure_directory(root / name)
    return root


def _http(
    endpoint: str,
    path: str,
    headers: dict[str, str],
    body: object = None,
) -> object:
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        endpoint + path,
        data=data,
        headers={
            **headers,
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    # An environment proxy must never receive the private bearer.
    with build_opener(ProxyHandler({})).open(request, timeout=2.0) as response:
        raw = response.read(_RECORD_LIMIT + 1)
        if len(raw) > _RECORD_LIMIT:
            raise DemoServiceError("demo readiness response exceeds 4 KiB")
        return strict_json(raw)


class DemoServiceHandle:
    """Own one exact Popen child and its private channels, never a saved PID."""

    def __init__(
        self,
        root: Path,
        process: subprocess.Popen[bytes],
        incoming: int,
        outgoing: int,
        token: bytes,
    ) -> None:
        self.root = root
        self._process = process
        self._incoming = os.fdopen(incoming, "rb", buffering=0)
        self._outgoing = outgoing
        self._token = token.hex()
        self._condition = threading.Condition()
        self._send_lock = threading.Lock()
        self._ready: dict[str, object] | None = None
        self._acks: dict[str, dict[str, object]] = {}
        self._failure = False
        self._finish_requested = False
        self._finish_cancel: bool | None = None
        self._target: tuple[str, str] | None = None
        self._result: dict[str, object] = {}
        self.endpoint = ""
        self.auth_headers: dict[str, str] = {}
        self._reader = threading.Thread(
            target=self._receive, name="millrace-demo-owner", daemon=True
        )
        self._reader.start()

    def _receive(self) -> None:
        try:
            while True:
                message = _read_record(self._incoming)
                with self._condition:
                    kind = message.get("kind")
                    request_id = message.get("id")
                    if kind == "ready":
                        self._ready = message
                    elif kind == "ack" and isinstance(request_id, str):
                        self._acks[request_id] = message
                    elif kind == "finish_requested":
                        self._finish_requested = True
                    elif kind == "shutdown":
                        self._result = message
                    else:
                        self._failure = True
                    self._condition.notify_all()
        except EOFError:
            pass
        except (OSError, ValueError, DemoServiceError):
            self._failure = True
        finally:
            with self._condition:
                self._failure = True
                self._condition.notify_all()
            self._incoming.close()

    def _command(self, kind: str, **values: object) -> None:
        with self._send_lock:
            identifier = secrets.token_hex(8)
            try:
                _write_record(
                    self._outgoing, {"kind": kind, "id": identifier, **values}
                )
            except OSError as exc:
                raise DemoServiceError("demo owner channel is closed") from exc
            deadline = time.monotonic() + _START_TIMEOUT
            with self._condition:
                while identifier not in self._acks and not self._failure:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DemoServiceError(
                            "demo owner acknowledgement not observed"
                        )
                    self._condition.wait(remaining)
                answer = self._acks.pop(identifier, None)
            if answer is None or answer.get("accepted") is not True:
                raise DemoServiceError("demo owner intent refused")

    @property
    def finish_requested(self) -> bool:
        with self._condition:
            return self._finish_requested

    @property
    def shutdown_result(self) -> dict[str, object]:
        with self._condition:
            result = copy.deepcopy(self._result)
        result.update(
            {
                "child_pid": self._process.pid,
                "child_exited": self._process.poll() is not None,
                "returncode": self._process.returncode,
                "status": "exited"
                if self._process.returncode is not None
                else "draining"
                if self._finish_cancel is not None or self._outgoing < 0
                else "running",
            }
        )
        # Child exit/worker drain never establishes Core or native writer closure.
        result["graph_writers_stopped"] = None
        return result

    def bootstrap_url(self, *, instance_id: str, run_id: str) -> str:
        if not _CORE_ID.fullmatch(instance_id) or not _CORE_ID.fullmatch(run_id):
            raise DemoServiceError("invalid demo target")
        target = (instance_id, run_id)
        if self._target is not None and self._target != target:
            raise DemoServiceError("demo target cannot be replaced")
        if self._target is None:
            self._command("bind", instance_id=instance_id, run_id=run_id)
            self._target = target
        value = _http(
            self.endpoint,
            "/api/v1/auth/bootstrap",
            self.auth_headers,
            {"recovery": False},
        )
        nonce = value.get("nonce") if isinstance(value, dict) else None
        if not isinstance(nonce, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43,64}", nonce
        ):
            raise DemoServiceError("invalid demo bootstrap response")
        query = urlencode({"home": "demo", "instance": instance_id, "run": run_id})
        return f"{self.endpoint}/ui/stock/?{query}#{nonce}"

    def update_outputs(
        self,
        accepted_output_refs: list[dict[str, object]],
        *,
        output_error: str | None = None,
    ) -> None:
        if self._target is None:
            raise DemoServiceError("demo target is not bound")
        self._command(
            "outputs",
            accepted_output_refs=accepted_output_refs,
            output_error=output_error,
        )

    def finish(self, cancel: bool) -> None:
        if type(cancel) is not bool:
            raise TypeError("cancel must be a boolean")
        if self._finish_cancel is True or self._finish_cancel == cancel:
            return
        self._command("finish", cancel=cancel)
        self._finish_cancel = cancel

    def wait(self, timeout: float | None = None) -> bool:
        try:
            self._process.wait(timeout=_DRAIN_TIMEOUT if timeout is None else timeout)
        except subprocess.TimeoutExpired:
            return False
        self._reader.join(timeout=1)
        return True

    def close(self) -> None:
        if self._outgoing >= 0:
            os.close(self._outgoing)
            self._outgoing = -1


def start_demo_service(root: Path, *, owner_token: bytes) -> DemoServiceHandle:
    """Start an isolated service and authenticate its private readiness in 10s."""
    if not isinstance(owner_token, bytes) or len(owner_token) != 32:
        raise ValueError("owner token must contain 256 bits")
    root = _private_root(root)
    child_read, parent_write = os.pipe()
    parent_read, child_write = os.pipe()
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "millrace_os.demo_service",
                str(child_read),
                str(child_write),
            ],
            pass_fds=(child_read, child_write),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except BaseException:
        for descriptor in (child_read, parent_write, parent_read, child_write):
            os.close(descriptor)
        raise
    os.close(child_read)
    os.close(child_write)
    handle = DemoServiceHandle(root, process, parent_read, parent_write, owner_token)
    deadline = time.monotonic() + _START_TIMEOUT
    try:
        _write_record(
            parent_write,
            {"kind": "launch", "root": str(root), "owner_token": owner_token.hex()},
        )
        with handle._condition:
            while handle._ready is None and not handle._failure:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DemoServiceError(
                        "demo readiness was not observed within 10 seconds"
                    )
                handle._condition.wait(remaining)
        ready = handle._ready or {}
        endpoint = ready.get("endpoint")
        ready_token = ready.get("owner_token")
        if not isinstance(endpoint, str) or not isinstance(ready_token, str):
            raise DemoServiceError("demo readiness identity did not match")
        address = urlsplit(endpoint)
        if (
            set(ready) != {"kind", "owner_token", "root", "pid", "endpoint"}
            or not secrets.compare_digest(ready_token, owner_token.hex())
            or ready.get("pid") != process.pid
            or ready.get("root") != str(root)
            or address.scheme != "http"
            or address.hostname != "127.0.0.1"
            or address.username is not None
            or address.password is not None
            or address.path
            or address.query
            or address.fragment
            or address.port is None
            or not 1 <= address.port <= 65535
        ):
            raise DemoServiceError("demo readiness identity did not match")
        handle.endpoint = endpoint
        handle.auth_headers = {
            "Authorization": "Bearer "
            + CredentialStore(root / "os-config" / "credentials.json").ensure()
        }
        identity = _http(endpoint, "/api/v1/identity", handle.auth_headers)
        if (
            not isinstance(identity, dict)
            or identity.get("contract") != "os.identity.v1"
            or identity.get("data")
            != {
                "identity": "local-operator",
                "authentication": "bearer",
                "recovery_only": False,
            }
            or time.monotonic() > deadline
        ):
            raise DemoServiceError("demo authenticated readiness did not match")
        return handle
    except BaseException:
        # EOF requests pause/drain; never force-kill an uncertain writer.
        handle.close()
        raise


class _ChildOwner:
    def __init__(self, root: Path, outgoing: int) -> None:
        self.root = root
        self.outgoing = outgoing
        self.lock = threading.Lock()
        self.context: DemoContext | None = None
        self.requested = False
        self.shutdown_cancel: bool | None = None

    def emit(self, value: dict[str, object]) -> None:
        with self.lock:
            _write_record(self.outgoing, value)

    def read_context(self) -> dict[str, object]:
        with self.lock:
            if self.context is None:
                raise DemoContextPending
            return {key: value for key, value in self.context.model_dump().items()}

    def request_finish(self) -> None:
        with self.lock:
            if self.context is None:
                raise DemoServiceError("demo target is not bound")
            if self.requested:
                return
            _write_record(self.outgoing, {"kind": "finish_requested"})
            self.requested = True

    def command(self, message: dict[str, object], service: Service) -> None:
        kind = message.get("kind")
        cancel = message.get("cancel")
        if kind == "bind" and set(message) == {"kind", "id", "instance_id", "run_id"}:
            context = DemoContext.model_validate(
                {
                    "format": "millrace-os-demo-context/v1",
                    "home": "demo",
                    "instance_id": message["instance_id"],
                    "run_id": message["run_id"],
                    "finish_available": True,
                    "accepted_output_refs": [],
                    "output_error": None,
                }
            )
            with self.lock:
                if self.context is not None:
                    if any(
                        value != previous
                        for value, previous in (
                            (self.context.home, context.home),
                            (self.context.instance_id, context.instance_id),
                            (self.context.run_id, context.run_id),
                        )
                    ):
                        raise DemoServiceError("demo target cannot be replaced")
                else:
                    self.context = context
        elif kind == "outputs" and set(message) == {
            "kind",
            "id",
            "accepted_output_refs",
            "output_error",
        }:
            with self.lock:
                if self.context is None:
                    raise DemoServiceError("demo target is not bound")
                context = DemoContext.model_validate(
                    {
                        **self.context.model_dump(),
                        "accepted_output_refs": message["accepted_output_refs"],
                        "output_error": message["output_error"],
                    }
                )
                _record(context.model_dump())
                if any(
                    item.reference.instance_id != context.instance_id
                    or item.reference.run_id != context.run_id
                    for item in context.accepted_output_refs
                ):
                    raise DemoServiceError("demo output target does not match")
                old = {
                    item.reference.output_id: item
                    for item in self.context.accepted_output_refs
                }
                if len(
                    {item.reference.output_id for item in context.accepted_output_refs}
                ) != len(context.accepted_output_refs):
                    raise DemoServiceError("duplicate accepted demo output")
                for item in context.accepted_output_refs:
                    if (
                        item.reference.output_id in old
                        and old[item.reference.output_id] != item
                    ):
                        raise DemoServiceError(
                            "accepted demo output cannot be replaced"
                        )
                if not set(old) <= {
                    item.reference.output_id for item in context.accepted_output_refs
                }:
                    raise DemoServiceError("accepted demo output cannot be removed")
                self.context = context
        elif (
            kind == "finish"
            and set(message) == {"kind", "id", "cancel"}
            and isinstance(cancel, bool)
        ):
            # Repeated EOF/Finish cannot turn Service's second-shutdown rule
            # into an accidental escalation. Explicit pause->cancel may escalate.
            if self.shutdown_cancel is None or (cancel and not self.shutdown_cancel):
                service.request_shutdown(cancel=cancel)
                self.shutdown_cancel = cancel
        else:
            raise DemoServiceError("invalid private demo intent")


def _child(incoming: int, outgoing: int) -> int:
    os.umask(0o077)
    stream = os.fdopen(incoming, "rb", buffering=0)
    launch = _read_record(stream)
    launch_root = launch.get("root")
    launch_token = launch.get("owner_token")
    if (
        set(launch) != {"kind", "root", "owner_token"}
        or launch["kind"] != "launch"
        or not isinstance(launch_root, str)
        or not isinstance(launch_token, str)
        or re.fullmatch(r"[0-9a-f]{64}", launch_token) is None
    ):
        raise DemoServiceError("invalid private launch")
    root = _private_root(Path(launch_root))
    handler = RotatingFileHandler(
        root / "logs" / "os-service.log", maxBytes=1048576, backupCount=1
    )
    logging.getLogger().addHandler(handler)
    owner = _ChildOwner(root, outgoing)
    service = Service(
        ApplicationConfig(
            config_dir=root / "os-config",
            state_dir=root / "os-state",
            credential_path=root / "os-config" / "credentials.json",
            port=0,
            allow_ephemeral_port=True,
            router_state_homes={"demo": root / "router-state"},
        ),
        dependencies=ServiceDependencies(
            demo_context_reader=owner.read_context,
            demo_finish_request=owner.request_finish,
        ),
    )
    service.start()
    owner.emit(
        {
            "kind": "ready",
            "owner_token": launch_token,
            "root": str(root),
            "pid": os.getpid(),
            "endpoint": service.browser_endpoint.rstrip("/"),
        }
    )
    connected = True
    while not service.wait_for_stop(timeout=0):
        if not connected:
            service.wait_for_stop(timeout=0.2)
            continue
        readable, _, _ = select.select([incoming], [], [], 0.2)
        if not readable:
            continue
        try:
            message = _read_record(stream)
        except (EOFError, ValueError, DemoServiceError):
            connected = False
            if owner.shutdown_cancel is None:
                service.request_shutdown()
                owner.shutdown_cancel = False
            continue
        accepted = True
        try:
            owner.command(message, service)
        except (ValueError, DemoServiceError):
            accepted = False
        try:
            owner.emit({"kind": "ack", "id": message.get("id"), "accepted": accepted})
        except BrokenPipeError:
            connected = False
            if owner.shutdown_cancel is None:
                service.request_shutdown()
                owner.shutdown_cancel = False
    service.stop()
    result: dict[str, object] = {
        "kind": "shutdown",
        "service_stopped": True,
        "workers_drained": True,
        "cancel_requested": owner.shutdown_cancel,
        "graph_writers_stopped": None,
    }
    descriptor, _ = open_new_file(root / "os-state" / "demo-shutdown.json")
    with os.fdopen(descriptor, "wb") as output:
        output.write(_record(result))
        output.flush()
        os.fsync(output.fileno())
    # Lifecycle EOF closes only parent->child intake. The independent result
    # channel may still have its owner waiting for this exact child's facts.
    try:
        owner.emit(result)
    except BrokenPipeError:
        pass  # A departed parent cannot receive the durable shutdown record.
    stream.close()
    os.close(outgoing)
    handler.close()
    return 0


if __name__ == "__main__":
    try:
        # The terminal coordinator owns first Ctrl-C cancellation and export.
        # A terminal group signal must not interrupt the child's pipe reader.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if len(sys.argv) != 3:
            raise DemoServiceError("private child descriptors required")
        raise SystemExit(_child(int(sys.argv[1]), int(sys.argv[2])))
    except (OSError, ValueError, DemoServiceError):
        raise SystemExit(1) from None
