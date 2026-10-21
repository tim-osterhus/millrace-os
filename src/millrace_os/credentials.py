"""Owner-only local credential storage."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from .paths import (
    UnsafePathError,
    ensure_directory,
    open_new_file,
    validate_file_target,
)


class CredentialStoreError(RuntimeError):
    """Raised when credentials cannot be kept in an owner-only store."""

    code = "credential_store_error"


class CredentialStore:
    """Create and read one local bearer credential without logging its value."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def ensure(self) -> str:
        parent = self.path.parent
        ensure_directory(parent)

        _, exists = validate_file_target(self.path)
        if exists:
            try:
                data = json.loads(self.path.read_text())
                token = data["token"]
            except (OSError, KeyError, TypeError, ValueError, UnsafePathError) as exc:
                raise CredentialStoreError("credential store is invalid") from exc
            if not isinstance(token, str) or len(token) < 32:
                raise CredentialStoreError("credential store is invalid")
            return token

        token = secrets.token_urlsafe(48)
        descriptor, path = open_new_file(self.path)
        try:
            os.write(
                descriptor,
                json.dumps({"token": token}, sort_keys=True).encode() + b"\n",
            )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        return token

    def rotate(self) -> str:
        """Atomically replace the owner-only credential; never return it over HTTP."""
        ensure_directory(self.path.parent)
        target, _ = validate_file_target(self.path)
        temporary = target.with_name(target.name + "." + secrets.token_hex(12))
        descriptor, temporary = open_new_file(temporary)
        token = secrets.token_urlsafe(48)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"token": token}, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            validate_file_target(target)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return token
