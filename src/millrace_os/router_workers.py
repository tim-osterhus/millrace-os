"""Bounded ownership of every public call that could acquire Core's controller."""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .contracts import Error, RouterCallObservation
from .router_client import RouterUnavailable, request_digest

__all__ = ["RouterWorkers"]

STOPS = {"pause_run", "cancel_run", "pause_plane", "cancel_plane"}
FOREGROUND = {
    "start_run",
    "recover_run",
    "resume_run",
    "accept_revision",
    "retry_invocation",
    "resume_plane",
    "redirect_plane",
}


def now():
    return datetime.now(UTC).isoformat()


@dataclass
class OwnedCall:
    home: str
    request: dict
    digest: str
    group: tuple
    slot: str
    thread: threading.Thread | None = None
    watcher: threading.Thread | None = None
    done: threading.Event = field(default_factory=threading.Event)
    receipt: dict | None = None
    error: Error | None = None
    error_status: int = 503
    release_admission: object = None
    started: bool = False
    joined: bool = False
    result: object = None
    finished_at: float | None = None
    instance_id: str | None = None
    run_id: str | None = None


class RouterWorkers:
    """Own bounded non-daemon calls through receipt observation and final join.

    Core decides actual controller ownership. A short ingress call is therefore
    tracked exactly like a foreground call: either may become the holder.
    """

    def __init__(self, client, *, capacity=8, observation_timeout=0.15):
        self.client = client
        self.capacity = capacity
        self.observation_timeout = observation_timeout
        self.observation_ttl = 300.0
        self._lock = threading.RLock()
        self._calls = {}
        self._closing = False
        self._sealed = False
        self._closing_calls = {}
        self._cancel = threading.Event()
        self.admission_guard = None

    def _key(self, home, request_id):
        return str(self.client.state_home(home)), request_id

    def _retire(self):
        for key, call in list(self._calls.items()):
            if call.done.is_set() and all(
                t is None or not t.is_alive() for t in (call.thread, call.watcher)
            ):
                for thread in (call.thread, call.watcher):
                    if thread is not None and thread is not threading.current_thread():
                        thread.join()
                call.joined = True
                if (
                    call.finished_at is not None
                    and time.monotonic() - call.finished_at > self.observation_ttl
                ):
                    del self._calls[key]
        # Only joined observations may be evicted. Active slots never expire.
        completed = [k for k, c in self._calls.items() if c.joined]
        for key in completed[:-128]:
            del self._calls[key]

    def submit(self, home, request, *, operation=None, bypass_guard=False):
        request = copy.deepcopy(request)
        digest = request_digest(request)
        request_id = request.get("request_id")
        if (
            not isinstance(request_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", request_id) is None
        ):
            raise RouterUnavailable("invalid_request", 422)
        for name in ("instance_id", "run_id"):
            value = request.get(name)
            if value is not None and (
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value) is None
            ):
                raise RouterUnavailable("invalid_request", 422)
        if not isinstance(request.get("operation"), str):
            raise RouterUnavailable("invalid_request", 422)
        key = self._key(home, request_id)
        op = request.get("operation")
        slot = (
            "stop" if op in STOPS else "foreground" if op in FOREGROUND else "control"
        )
        instance_id = request.get("instance_id")
        group = (
            str(self.client.state_home(home)),
            instance_id or ("request:" + request_id),
        )
        with self._lock:
            self._retire()
            old = self._calls.get(key)
            if old is not None:
                if old.digest != digest:
                    raise RouterUnavailable("request_id_reused", 409)
                return old
            if self._sealed or (self._closing and slot != "stop"):
                raise RouterUnavailable("router_closing", 503)
            active = [c for c in self._calls.values() if not c.joined]
            groups = {c.group for c in active}
            if (group not in groups and len(groups) >= self.capacity) or any(
                c.group == group and c.slot == slot for c in active
            ):
                raise RouterUnavailable("router_busy", 429)
            release = None
            if (
                self.admission_guard is not None
                and not bypass_guard
                and slot != "stop"
                and op != "reconcile_invocation"
            ):
                release = self.admission_guard(home, instance_id)
            call = OwnedCall(
                home,
                request,
                digest,
                group,
                slot,
                release_admission=release,
                instance_id=instance_id,
                run_id=request.get("run_id"),
            )
            self._calls[key] = call
            call.thread = threading.Thread(
                target=self._run,
                args=(call, operation),
                name="millrace-os-call-" + request_id,
                daemon=False,
            )
            call.watcher = threading.Thread(
                target=self._watch,
                args=(call,),
                name="millrace-os-observe-" + request_id,
                daemon=False,
            )
            call.thread.start()
            call.watcher.start()
            return call

    def _release(self, call):
        with self._lock:
            release = call.release_admission
            call.release_admission = None
        if release is not None:
            release()

    def _accept_receipt(self, call, receipt):
        if (
            not isinstance(receipt, dict)
            or receipt.get("request_id") != call.request["request_id"]
        ):
            return
        if receipt.get("request_digest") != call.digest:
            # A previous same-ID body is never trusted as this call's receipt.
            return
        with self._lock:
            call.receipt = copy.deepcopy(receipt)
            call.instance_id = receipt.get("instance_id") or call.instance_id
            call.run_id = receipt.get("run_id") or call.run_id
        self._release(call)

    def _run(self, call, operation):
        try:
            call.started = True

            def started(receipt):
                with self._lock:
                    call.instance_id = receipt.get("instance_id") or call.instance_id
                    call.run_id = receipt.get("run_id") or call.run_id
                self._accept_receipt(call, receipt)

            result = (
                operation()
                if operation is not None
                else self.client.execute_request(
                    call.home, call.request, on_started=started
                )
            )
            call.result = result
            self._accept_receipt(call, result)
        except Exception as exc:
            code = (
                exc.code if isinstance(exc, RouterUnavailable) else "router_call_failed"
            )
            call.error_status = (
                exc.status if isinstance(exc, RouterUnavailable) else 503
            )
            call.error = Error(
                code=code,
                category="availability",
                summary=code.replace("_", " "),
                retryable=False,
                actionable=True,
                correlation=call.request["request_id"],
            )
        finally:
            self._release(call)
            call.finished_at = time.monotonic()
            call.done.set()

    def _watch(self, call):
        if call.request.get("format") != "millrace-router-request/v2":
            return
        while not call.done.is_set() or (
            call.request["operation"] == "start_run"
            and call.receipt is not None
            and call.receipt.get("status") == "queued"
        ):
            try:
                receipt = self.client.receipt(
                    call.home,
                    call.request["request_id"],
                    instance_id=call.instance_id,
                    run_id=call.run_id,
                )
                if receipt is not None:
                    self._accept_receipt(call, receipt)
            except RouterUnavailable:
                pass
            if call.done.is_set():
                time.sleep(0.02)
            else:
                call.done.wait(0.02)

    def _observation(self, call):
        phase = (
            ("call_failed" if call.error else "call_returned")
            if call.done.is_set()
            else ("call_active" if call.started else "admission_pending")
        )
        return RouterCallObservation(
            home=call.home,
            request_id=call.request["request_id"],
            request_digest=call.digest,
            instance_id=call.instance_id,
            run_id=call.run_id,
            source="os_owned_call",
            phase=phase,
            core_receipt=copy.deepcopy(call.receipt),
            observed_at=now(),
        )

    def observe(self, home, request_id, *, instance_id=None, run_id=None):
        with self._lock:
            self._retire()
            call = self._calls.get(self._key(home, request_id))
            if call is not None:
                if (instance_id is not None and instance_id != call.instance_id) or (
                    run_id is not None and run_id != call.run_id
                ):
                    raise RouterUnavailable("observation_target_mismatch", 409)
        if call is not None:
            # A short ingress call can return a queued receipt while another
            # controller later applies it. Observation is a fresh public read,
            # never another execute/replay or a continued ownership claim.
            if call.receipt is not None and call.receipt.get("status") == "queued":
                receipt = self.client.receipt(
                    home, request_id, instance_id=call.instance_id, run_id=call.run_id
                )
                if receipt is not None:
                    self._accept_receipt(call, receipt)
            return self._observation(call)
        receipt = self.client.receipt(
            home, request_id, instance_id=instance_id, run_id=run_id
        )
        if receipt is None:
            raise RouterUnavailable("observation_unavailable", 404)
        return RouterCallObservation(
            home=home,
            request_id=request_id,
            request_digest=receipt.get("request_digest"),
            instance_id=receipt.get("instance_id"),
            run_id=receipt.get("run_id"),
            source="core_receipt",
            phase="receipt_observed",
            core_receipt=receipt,
            observed_at=now(),
        )

    def call_observation(self, call):
        """Return the owned-call observation without starting another Core read."""
        with self._lock:
            return self._observation(call)

    def wait_observation(self, call):
        call.done.wait(self.observation_timeout)
        return self.observe(call.home, call.request["request_id"])

    def snapshot(self):
        with self._lock:
            self._retire()
            return [
                {
                    "observation": self._observation(c).model_dump(),
                    "joined": c.joined,
                    "slot": c.slot,
                    "worker_error": c.error.model_dump() if c.error else None,
                }
                for c in self._calls.values()
            ]

    def reopen(self):
        """Reopen only after the previous service lifetime has drained and joined."""
        with self._lock:
            self._retire()
            if any(not call.joined for call in self._calls.values()):
                raise RouterUnavailable("router_busy", 429)
            self._closing = self._sealed = False
            self._closing_calls.clear()
            self._cancel.clear()

    def request_shutdown(self, *, cancel=False):
        with self._lock:
            self._retire()
            self._closing = True
            # Keep references beyond physical return/join. A queued start can
            # acquire its generated run identity between two coordinator scans.
            self._closing_calls.update(
                {
                    key: call
                    for key, call in self._calls.items()
                    if not call.joined and call.slot != "stop"
                }
            )
            if cancel:
                self._cancel.set()

    def drain(self):
        """Resolve queued starts, stop their runs, and seal final ingress atomically."""
        requested = {}
        read_failures = set()

        def read_unsettled(exc, target):
            code = (
                exc.code if isinstance(exc, RouterUnavailable) else "core_read_failed"
            )
            if code in {"core_unknown_run", "core_invalid_id"}:
                # No such target exists; this does not bypass physical-call joins.
                return False
            failure = (target, code)
            if failure not in read_failures:
                read_failures.add(failure)
                logging.getLogger(__name__).warning(
                    "Core run read unavailable during shutdown (%s); "
                    "awaiting readable state",
                    code,
                )
            return True

        while True:
            with self._lock:
                self._retire()
                active = [call for call in self._calls.values() if not call.joined]
                tracked = list(self._closing_calls.values())
            unsettled = False
            checked_targets = set()
            for call in tracked:
                if call.slot == "stop" or not call.instance_id or not call.run_id:
                    continue
                op = "cancel_run" if self._cancel.is_set() else "pause_run"
                target = (call.home, call.instance_id, call.run_id, op)
                checked_targets.add(target)
                try:
                    run = self.client.inspect_run(
                        call.home, call.instance_id, call.run_id
                    )
                except Exception as exc:
                    unsettled |= read_unsettled(exc, target)
                    continue
                if run["status"] in {"succeeded", "failed", "cancelled"} and run.get(
                    "quiescent"
                ):
                    continue
                stop = requested.get(target)
                if stop is None:
                    request = {
                        "format": "millrace-router-request/v2",
                        "operation": op,
                        "instance_id": call.instance_id,
                        "run_id": call.run_id,
                        "plane_id": None,
                        "request_id": uuid.uuid4().hex,
                        "expected_state_version": None,
                        "payload": {},
                    }
                    try:
                        stop = self.submit(call.home, request, bypass_guard=True)
                        requested[target] = stop
                    except RouterUnavailable as exc:
                        if exc.code != "router_busy":
                            raise
                    unsettled = True
                    continue
                if stop.receipt is not None and stop.receipt.get("status") == "queued":
                    # A short known-target stop returned and freed its physical
                    # ingress slot. Keep its exact identity while observing the
                    # actual receipt; a later cancel can still enter that slot.
                    try:
                        observed = self.client.receipt(
                            call.home,
                            stop.request["request_id"],
                            instance_id=call.instance_id,
                            run_id=call.run_id,
                        )
                    except Exception as exc:
                        unsettled |= read_unsettled(exc, target)
                        continue
                    if observed is not None:
                        self._accept_receipt(stop, observed)
                if stop.receipt is None or stop.receipt.get("status") == "queued":
                    unsettled = True
                elif run.get("inflight_invocations"):
                    unsettled = True
                elif not run.get("quiescent"):
                    # Retained uncertain writers are Core truth, not live OS
                    # workers to invent or kill. They remain visible on restart.
                    unsettled |= any(
                        i.get("writer_state") == "running"
                        for i in run.get("invocations", [])
                    )
            with self._lock:
                self._retire()
                current_targets = {
                    (
                        c.home,
                        c.instance_id,
                        c.run_id,
                        "cancel_run" if self._cancel.is_set() else "pause_run",
                    )
                    for c in self._closing_calls.values()
                    if c.instance_id and c.run_id
                }
                if (
                    not unsettled
                    and current_targets <= checked_targets
                    and not any(not c.joined for c in self._calls.values())
                ):
                    # Receipt ID updates, target coverage, final empty-check,
                    # and ingress sealing share this same lock. A run learned
                    # after the scan must receive another coordinator pass.
                    # A later stop either entered before this seal and is joined,
                    # or refuses before creating any potential holder.
                    self._sealed = True
                    return
            if active:
                if active[0].done.is_set():
                    for thread in (active[0].thread, active[0].watcher):
                        if thread is not None:
                            thread.join(timeout=0.02)
                else:
                    active[0].done.wait(0.02)
            else:
                time.sleep(0.02)

    def wait_group(self, home, instance_id, *, exclude=None, exclude_thread=None):
        """Wait for final public-call return after Core's quiescence assertion."""
        while True:
            with self._lock:
                calls = [
                    c
                    for c in self._calls.values()
                    if c.home == home
                    and c.instance_id == instance_id
                    and c is not exclude
                    and c.thread is not exclude_thread
                    and not c.joined
                ]
            if not calls:
                return
            for call in calls:
                call.done.wait()
                for thread in (call.thread, call.watcher):
                    if thread is not None and thread is not threading.current_thread():
                        thread.join()
                with self._lock:
                    call.joined = True
