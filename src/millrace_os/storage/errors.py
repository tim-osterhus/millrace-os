"""Typed errors for safe local state handling."""

from __future__ import annotations


class StateError(RuntimeError):
    """Base class for a state failure that can be shown by recovery runtime."""

    code = "state_error"


class CorruptStateError(StateError):
    code = "corrupt_state"


class IncompatibleStateError(StateError):
    code = "incompatible_state"
