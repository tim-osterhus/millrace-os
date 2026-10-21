from __future__ import annotations

import os
from dataclasses import replace

import pytest

from millrace_os import process


def test_native_birth_is_stable_and_microsecond_precision():
    first = process.ProcessIdentity.current()
    assert first == process.ProcessIdentity.current()
    assert first.pid == os.getpid()
    seconds, micros = first.start_time.split(":")
    assert int(seconds) > 0
    assert len(micros) == 6


@pytest.mark.parametrize("kind", ["reused", "unowned", "external"])
def test_refusal_never_signals(monkeypatch, kind):
    identity = process.ProcessIdentity.current()
    identity = replace(identity, arguments="/venv/bin/millrace-os serve")
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


def test_observation_failure_at_deadline_is_not_exit(monkeypatch):
    identity = process.ProcessIdentity.current()

    def fail(_):
        raise process.ProcessIdentityError("inspection failed")

    monkeypatch.setattr(process, "observe_identity", fail)
    with pytest.raises(process.ProcessIdentityError):
        process.wait_for_exit(identity, timeout=0)


def test_stop_timeout_is_not_success(monkeypatch):
    identity = replace(
        process.ProcessIdentity.current(), arguments="/venv/bin/millrace-os serve"
    )
    monkeypatch.setattr(process, "observe_identity", lambda _: identity)
    monkeypatch.setattr(process.os, "kill", lambda *args: None)
    with pytest.raises(process.ProcessStopError):
        process.stop_verified(identity, timeout=0)


def test_unsupported_platform_fails_closed(monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "unsupported")
    with pytest.raises(process.ProcessIdentityError, match="unsupported"):
        process.ProcessIdentity.current()
