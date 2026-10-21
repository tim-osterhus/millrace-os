"""OS-owned durable state primitives."""

from .database import CURRENT_SCHEMA_VERSION, SQLiteState
from .errors import CorruptStateError, IncompatibleStateError, StateError

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "CorruptStateError",
    "IncompatibleStateError",
    "SQLiteState",
    "StateError",
]
