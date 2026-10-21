"""Deterministic SQLite WAL state with non-mutating migration preflight."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..paths import ensure_directory, open_new_file, validate_file_target
from .errors import CorruptStateError, IncompatibleStateError

CURRENT_SCHEMA_VERSION = 1
COMPATIBILITY_ID = "millrace-os-release-1"
MIGRATIONS_DIR = files("millrace_os.storage").joinpath("migrations")


def _migration_paths() -> tuple[Any, ...]:
    try:
        candidates = MIGRATIONS_DIR.iterdir()
    except OSError as exc:
        raise IncompatibleStateError("OS migration resources are unavailable") from exc
    paths = tuple(
        sorted(
            (
                path
                for path in candidates
                if path.is_file()
                and len(path.name) >= 8
                and path.name[:3].isdigit()
                and path.name[3] == "_"
                and path.name.endswith(".sql")
            ),
            key=lambda path: path.name,
        )
    )
    if not paths:
        raise IncompatibleStateError("OS migration resources are unavailable")
    return paths


def _migration_name(path: Any) -> str:
    return str(path.name)


def _migration_bytes(path: Any) -> bytes:
    try:
        return path.read_bytes()
    except (OSError, AttributeError) as exc:
        raise IncompatibleStateError(
            f"migration cannot be read: {_migration_name(path)}"
        ) from exc


def _migration_sql(path: Any) -> str:
    try:
        return _migration_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IncompatibleStateError(
            f"migration is not UTF-8: {_migration_name(path)}"
        ) from exc


def _split_statements(sql: str) -> Iterable[str]:
    """Split ordinary SQL statements without executescript's implicit commit."""

    buffer: list[str] = []
    for line in sql.splitlines(keepends=True):
        buffer.append(line)
        statement = "".join(buffer)
        if sqlite3.complete_statement(statement):
            if statement.strip().strip(";").strip():
                yield statement
            buffer.clear()
    remainder = "".join(buffer).strip()
    if remainder:
        raise sqlite3.DatabaseError("migration has an incomplete SQL statement")


def _execute_migration(
    connection: sqlite3.Connection, path: Any, checksum: str
) -> None:
    filename = _migration_name(path)
    for statement in _split_statements(_migration_sql(path)):
        # Transaction control belongs to SQLiteState.open, so a migration
        # cannot commit a partial schema before a later statement fails.
        connection.execute(statement)
    connection.execute(
        "INSERT INTO millrace_os_migrations(filename, checksum) VALUES (?, ?)",
        (filename, checksum),
    )


def _database_uri(path: Path) -> str:
    return f"file:{quote(os.fspath(path), safe='/')}?mode=ro&immutable=1"


class SQLiteState:
    """One opened and migrated OS-owned SQLite database."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self.connection = connection
        self.path = path

    @classmethod
    def open(cls, path: Path) -> SQLiteState:
        database_path = Path(path).expanduser().absolute()
        ensure_directory(database_path.parent)
        database_path, existed = validate_file_target(database_path, owner_only=False)
        for suffix in ("-wal", "-shm", "-journal"):
            _, present = validate_file_target(
                Path(f"{database_path}{suffix}"), owner_only=False
            )
            if present:
                raise IncompatibleStateError(
                    "state has SQLite sidecars; offline recovery is required"
                )
        created_by_us = False

        if not existed:
            try:
                descriptor, database_path = open_new_file(
                    database_path, flags=os.O_RDWR
                )
                os.close(descriptor)
                created_by_us = True
            except FileExistsError:
                database_path, existed = validate_file_target(
                    database_path, owner_only=False
                )
                if not existed:
                    raise IncompatibleStateError(
                        "state database disappeared during creation"
                    )

        migration_paths = _migration_paths()
        migration_checksums = {
            _migration_name(path): hashlib.sha256(_migration_bytes(path)).hexdigest()
            for path in migration_paths
        }

        # Validate and run the migration plan against a read-only snapshot
        # first.  This keeps malformed/future/migration-failed input and its
        # WAL/journal sidecars untouched.
        try:
            if existed:
                readonly = sqlite3.connect(_database_uri(database_path), uri=True)
                try:
                    cls._configure_connection(readonly)
                    pending = cls._validate_and_plan(
                        readonly, migration_checksums, database_path
                    )
                    snapshot = sqlite3.connect(":memory:")
                    try:
                        readonly.backup(snapshot)
                        cls._configure_connection(snapshot)
                        cls._apply_plan(
                            snapshot, pending, migration_paths, migration_checksums
                        )
                        cls._validate_final(
                            snapshot, migration_checksums, database_path
                        )
                    finally:
                        snapshot.close()
                finally:
                    readonly.close()
            else:
                snapshot = sqlite3.connect(":memory:")
                try:
                    cls._configure_connection(snapshot)
                    pending = tuple(migration_checksums)
                    cls._apply_plan(
                        snapshot, pending, migration_paths, migration_checksums
                    )
                    cls._validate_final(snapshot, migration_checksums, database_path)
                finally:
                    snapshot.close()
        except (CorruptStateError, IncompatibleStateError):
            if created_by_us:
                cls._remove_created_database(database_path)
            raise
        except sqlite3.DatabaseError as exc:
            if created_by_us:
                cls._remove_created_database(database_path)
            raise IncompatibleStateError("state migration preflight failed") from exc
        except BaseException:
            if created_by_us:
                cls._remove_created_database(database_path)
            raise

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database_path, check_same_thread=False)
            cls._configure_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            pending = (
                tuple(migration_checksums)
                if created_by_us
                else cls._validate_and_plan(
                    connection, migration_checksums, database_path
                )
            )
            cls._apply_plan(connection, pending, migration_paths, migration_checksums)
            cls._validate_final(connection, migration_checksums, database_path)
            connection.commit()
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise IncompatibleStateError("SQLite WAL mode could not be enabled")
            return cls(connection, database_path)
        except (CorruptStateError, IncompatibleStateError, PermissionError):
            if connection is not None:
                connection.rollback()
                connection.close()
            if created_by_us:
                cls._remove_created_database(database_path)
            raise
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.rollback()
                connection.close()
            if created_by_us:
                cls._remove_created_database(database_path)
            raise IncompatibleStateError("state migration failed") from exc
        except BaseException:
            if connection is not None:
                connection.rollback()
                connection.close()
            if created_by_us:
                cls._remove_created_database(database_path)
            raise

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def _validate_and_plan(
        cls,
        connection: sqlite3.Connection,
        migration_checksums: dict[str, str],
        database_path: Path,
    ) -> tuple[str, ...]:
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise CorruptStateError(
                f"state database is corrupt: {database_path}"
            ) from exc
        if integrity != ("ok",):
            raise CorruptStateError(f"state database is corrupt: {database_path}")

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not tables or "millrace_os_schema" not in tables:
            raise IncompatibleStateError("existing state has no Millrace OS schema")
        rows = connection.execute(
            "SELECT schema_version, typeof(schema_version), compatibility_id "
            "FROM millrace_os_schema"
        ).fetchall()
        if len(rows) != 1:
            raise IncompatibleStateError(
                "state schema must contain exactly one version"
            )
        version_value, version_type, compatibility_id = rows[0]
        if version_type != "integer":
            raise IncompatibleStateError("state schema version is malformed")
        version = version_value
        if version > CURRENT_SCHEMA_VERSION:
            raise IncompatibleStateError("state schema is newer than this release")
        if compatibility_id != COMPATIBILITY_ID:
            raise IncompatibleStateError(
                "state schema belongs to another compatibility line"
            )

        if "millrace_os_migrations" not in tables:
            raise IncompatibleStateError("state schema has no migration history")

        applied = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT filename, checksum FROM millrace_os_migrations "
                "ORDER BY filename"
            )
        }
        if set(applied) - set(migration_checksums):
            raise IncompatibleStateError("state has an unknown migration")
        for filename, checksum in applied.items():
            if migration_checksums[filename] != checksum:
                raise IncompatibleStateError(f"migration checksum changed: {filename}")

        names = tuple(migration_checksums)
        pending = tuple(name for name in names if name not in applied)
        return pending

    @classmethod
    def _apply_plan(
        cls,
        connection: sqlite3.Connection,
        pending: Iterable[str],
        migration_paths: Iterable[Any],
        migration_checksums: dict[str, str],
    ) -> None:
        by_name = {_migration_name(path): path for path in migration_paths}
        for filename in pending:
            path = by_name[filename]
            _execute_migration(connection, path, migration_checksums[filename])

    @classmethod
    def _validate_final(
        cls,
        connection: sqlite3.Connection,
        migration_checksums: dict[str, str],
        database_path: Path,
    ) -> None:
        cls._validate_and_plan(connection, migration_checksums, database_path)
        row = connection.execute(
            "SELECT schema_version, typeof(schema_version) "
            "FROM millrace_os_schema"
        ).fetchone()
        if row is None or row[1] != "integer" or row[0] != CURRENT_SCHEMA_VERSION:
            raise IncompatibleStateError(
                "state schema migration did not reach this release"
            )
        pending = cls._validate_and_plan(connection, migration_checksums, database_path)
        if pending:
            raise IncompatibleStateError("state schema has unapplied migrations")

    @staticmethod
    def _remove_created_database(path: Path) -> None:
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                path.unlink()
        except FileNotFoundError:
            return
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{path}{suffix}")
            try:
                sidecar_info = os.lstat(sidecar)
            except FileNotFoundError:
                continue
            if (
                stat.S_ISREG(sidecar_info.st_mode)
                and sidecar_info.st_uid == os.getuid()
            ):
                sidecar.unlink()

    @property
    def journal_mode(self) -> str:
        return str(self.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @property
    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT schema_version, typeof(schema_version) "
            "FROM millrace_os_schema"
        ).fetchone()
        if row is None:
            raise IncompatibleStateError("state schema has no version")
        if row[1] != "integer":
            raise IncompatibleStateError("state schema version is malformed")
        return row[0]

    @property
    def applied_migrations(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT filename FROM millrace_os_migrations ORDER BY filename"
        ).fetchall()
        return tuple(row[0] for row in rows)

    @property
    def migration_run_count(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM millrace_os_migrations"
            ).fetchone()[0]
        )

    def close(self) -> None:
        self.connection.close()
