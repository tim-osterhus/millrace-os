from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

from millrace_os import process


@pytest.fixture
def identity():
    """A synthetic OS serve identity for the stop policy.

    These cases patch `observe_identity`, the policy's only route to native
    observation, so they need no native primitive and run on every platform.
    """
    return process.ProcessIdentity(
        pid=os.getpid(),
        uid=os.getuid(),
        parent_pid=os.getppid(),
        executable="/venv/bin/millrace-os",
        arguments="/venv/bin/millrace-os serve",
        start_time="1:000001",
    )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="native daemon identity is qualified on macOS only; "
    "other platforms fail closed (see test_unsupported_platform_fails_closed)",
)
def test_native_birth_is_stable_and_microsecond_precision():
    first = process.ProcessIdentity.current()
    assert first == process.ProcessIdentity.current()
    assert first.pid == os.getpid()
    seconds, micros = first.start_time.split(":")
    assert int(seconds) > 0
    assert len(micros) == 6


@pytest.mark.parametrize("kind", ["reused", "unowned", "external"])
def test_refusal_never_signals(monkeypatch, identity, kind):
    observed = identity
    if kind == "reused":
        observed = replace(identity, start_time="1:000002")
    elif kind == "unowned":
        observed = replace(identity, uid=os.getuid() + 1)
        identity = observed
    else:
        identity = observed = replace(identity, arguments="python provider.py")
    monkeypatch.setattr(process, "observe_identity", lambda _: observed)
    signals = []
    monkeypatch.setattr(process.os, "kill", lambda *args: signals.append(args))
    with pytest.raises(process.ProcessIdentityError):
        process.stop_verified(identity)
    assert signals == []


def test_observation_failure_at_deadline_is_not_exit(monkeypatch, identity):
    def fail(_):
        raise process.ProcessIdentityError("inspection failed")

    monkeypatch.setattr(process, "observe_identity", fail)
    with pytest.raises(process.ProcessIdentityError):
        process.wait_for_exit(identity, timeout=0)


def test_stop_timeout_is_not_success(monkeypatch, identity):
    monkeypatch.setattr(process, "observe_identity", lambda _: identity)
    monkeypatch.setattr(process.os, "kill", lambda *args: None)
    with pytest.raises(process.ProcessStopError):
        process.stop_verified(identity, timeout=0)


def test_unsupported_platform_fails_closed(monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "unsupported")
    with pytest.raises(process.ProcessIdentityError, match="unsupported"):
        process.ProcessIdentity.current()
