"""Immutable health/recovery state shown when OS-local state is unsafe."""

from __future__ import annotations

from dataclasses import dataclass

from millrace_os.storage.errors import StateError


@dataclass(frozen=True, slots=True)
class RecoveryRuntime:
    """A read-only description of a safe startup failure."""

    code: str
    message: str

    @classmethod
    def from_error(cls, error: StateError) -> RecoveryRuntime:
        return cls(
            code=error.code,
            message="OS storage is unavailable; original state preserved.",
        )

    def payload(self) -> dict[str, object]:
        return {
            "status": "recovery",
            "error": {"code": self.code, "message": self.message},
            "actions": [],
            "mutable_ui": False,
        }
