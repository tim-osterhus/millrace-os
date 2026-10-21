"""Small, fail-closed helpers for paths owned by the local service.

The service creates only paths below directories explicitly supplied in its
configuration.  Before creating anything, every existing ancestor is checked
with ``lstat`` so a symlink (including a dangling one) cannot redirect a write.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafePathError(PermissionError):
    """Raised when a configured path cannot be proved to be locally owned."""

    code = "unsafe_path"


def _absolute(path: Path | str) -> Path:
    """Return an absolute lexical path without resolving symlinks."""

    return Path(path).expanduser().absolute()


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _check_existing_ancestor(path: Path, info: os.stat_result) -> None:
    if info.st_uid not in {0, os.getuid()}:
        raise UnsafePathError(f"path ancestor is owned by another user: {path}")
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        raise UnsafePathError(f"path must not contain a symbolic link: {path}")
    if not stat.S_ISDIR(mode):
        raise UnsafePathError(f"path ancestor is not a directory: {path}")

    # A system-owned, read-only ancestor such as /tmp's parent is safe.  A
    # writable ancestor is accepted only when it is a sticky directory (the
    # normal /tmp shape); otherwise another user could redirect our files.
    writable_by_group_or_other = bool(mode & 0o022)
    if writable_by_group_or_other and not bool(mode & stat.S_ISVTX):
        raise UnsafePathError(f"path ancestor is writable by another user: {path}")


def validate_ancestors(path: Path | str) -> Path:
    """Validate all existing components of ``path`` without mutating them."""

    candidate = _absolute(path)
    current = candidate
    while True:
        info = _lstat(current)
        if info is not None:
            _check_existing_ancestor(current, info)
        if current.parent == current:
            break
        current = current.parent

    # A missing target is valid, but every existing lexical ancestor has still
    # been inspected above.  This explicit walk matters when a target already
    # exists below an older symlink or unsafe directory.
    return candidate


def ensure_directory(path: Path | str) -> Path:
    """Create an owner-only directory tree after validating its parents."""

    directory = validate_ancestors(path)
    missing: list[Path] = []
    current = directory
    while _lstat(current) is None:
        missing.append(current)
        if current.parent == current:
            raise UnsafePathError(f"cannot create directory: {directory}")
        current = current.parent

    for child in reversed(missing):
        try:
            os.mkdir(child, 0o700)
        except FileExistsError:
            pass
        info = _lstat(child)
        if info is None:
            raise UnsafePathError(f"directory disappeared during creation: {child}")
        _check_existing_ancestor(child, info)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise UnsafePathError(f"directory must be owner-only: {child}")

    info = _lstat(directory)
    if info is None:
        raise UnsafePathError(f"directory was not created: {directory}")
    _check_existing_ancestor(directory, info)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise UnsafePathError(f"directory must be owner-only: {directory}")
    return directory


def validate_file_target(
    path: Path | str, *, owner_only: bool = True
) -> tuple[Path, bool]:
    """Validate a file target and its parents, returning ``(path, exists)``."""

    candidate = _absolute(path)
    validate_ancestors(candidate.parent)
    info = _lstat(candidate)
    if info is None:
        return candidate, False
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"file must not be a symbolic link: {candidate}")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePathError(f"file target is not a regular file: {candidate}")
    if info.st_uid != os.getuid():
        raise UnsafePathError(f"file must be owned by the current user: {candidate}")
    if owner_only and info.st_mode & 0o077:
        raise UnsafePathError(f"file must be owner-only: {candidate}")
    return candidate, True


def open_new_file(path: Path | str, *, flags: int = os.O_WRONLY) -> tuple[int, Path]:
    """Open an absent owner-only file without following a symlink."""

    candidate, exists = validate_file_target(path)
    if exists:
        raise FileExistsError(candidate)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(
        candidate,
        flags | os.O_CREAT | os.O_EXCL | nofollow,
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise UnsafePathError(
                f"file target is not a safe regular file: {candidate}"
            )
        if info.st_mode & 0o077:
            raise UnsafePathError(f"file target is not owner-only: {candidate}")
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass
        raise
    return descriptor, candidate


def owned_file_matches(path: Path | str, *, owner_only: bool = True) -> bool:
    """Return whether an existing file remains the same safe target."""

    try:
        _, exists = validate_file_target(path, owner_only=owner_only)
    except (OSError, UnsafePathError):
        return False
    return exists
