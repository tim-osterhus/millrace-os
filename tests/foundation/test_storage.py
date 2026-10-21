from __future__ import annotations

import hashlib
import socket
import sqlite3

import pytest
from fastapi.testclient import TestClient

from millrace_os.config import ApplicationConfig
from millrace_os.service import Service
from millrace_os.storage.database import MIGRATIONS_DIR, SQLiteState
from millrace_os.storage.errors import CorruptStateError, IncompatibleStateError


def test_sqlite_state_uses_wal_and_repeatable_ordered_migrations(tmp_path) -> None:
    database_path = tmp_path / "state.sqlite3"

    first = SQLiteState.open(database_path)
    assert first.journal_mode == "wal"
    assert first.schema_version == 1
    assert first.applied_migrations == ("001_initial.sql",)
    first.close()

    second = SQLiteState.open(database_path)
    assert second.schema_version == 1
    assert second.applied_migrations == ("001_initial.sql",)
    assert second.migration_run_count == 1
    second.close()


def test_corrupt_state_is_typed_and_does_not_get_replaced(tmp_path) -> None:
    database_path = tmp_path / "state.sqlite3"
    database_path.write_bytes(b"not a sqlite database")

    with pytest.raises(CorruptStateError, match="corrupt"):
        SQLiteState.open(database_path)

    assert database_path.read_bytes() == b"not a sqlite database"


def test_existing_schema_less_database_is_incompatible_and_unchanged(tmp_path) -> None:
    database_path = tmp_path / "state.sqlite3"
    database_path.touch()
    before = database_path.read_bytes()

    with pytest.raises(IncompatibleStateError, match="Millrace OS schema"):
        SQLiteState.open(database_path)

    assert database_path.read_bytes() == before


def test_newer_schema_is_refused_without_downgrade(tmp_path) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE millrace_os_schema (
            schema_version INTEGER NOT NULL,
            compatibility_id TEXT NOT NULL
        );
        INSERT INTO millrace_os_schema VALUES (99, 'future');
        """
    )
    connection.commit()
    connection.close()
    before = database_path.read_bytes()

    with pytest.raises(IncompatibleStateError, match="newer"):
        SQLiteState.open(database_path)

    assert database_path.read_bytes() == before
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "SELECT schema_version FROM millrace_os_schema"
    ).fetchone() == (99,)
    connection.close()


def test_fractional_schema_version_enters_read_only_recovery(tmp_path) -> None:
    database_path = tmp_path / "state" / "state.sqlite3"
    database_path.parent.mkdir()
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE millrace_os_schema ("
        "schema_version REAL NOT NULL, compatibility_id TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO millrace_os_schema VALUES (?, ?)",
        (1.5, "millrace-os-release-1"),
    )
    connection.execute(
        "CREATE TABLE millrace_os_migrations ("
        "filename TEXT PRIMARY KEY, checksum TEXT NOT NULL)"
    )
    checksum = hashlib.sha256(
        MIGRATIONS_DIR.joinpath("001_initial.sql").read_bytes()
    ).hexdigest()
    connection.execute(
        "INSERT INTO millrace_os_migrations VALUES (?, ?)",
        ("001_initial.sql", checksum),
    )
    connection.commit()
    connection.close()
    before = database_path.read_bytes()

    with pytest.raises(IncompatibleStateError, match="version"):
        SQLiteState.open(database_path)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=database_path.parent,
        credential_path=tmp_path / "config" / "credentials.json",
        port=port,
    )
    service = Service(config)
    service.start()
    try:
        assert service._recovery is not None
        assert service._recovery.code == "incompatible_state"
        client = TestClient(
            service.app, base_url=f"http://127.0.0.1:{service.bound_port}"
        )
        assert client.get("/ready").status_code == 503
    finally:
        service.stop()
    assert database_path.read_bytes() == before


def test_malformed_schema_version_is_typed_as_incompatible(tmp_path) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE millrace_os_schema (
            schema_version TEXT NOT NULL,
            compatibility_id TEXT NOT NULL
        );
        INSERT INTO millrace_os_schema VALUES (
            'not-a-version', 'millrace-os-release-1'
        );
        CREATE TABLE millrace_os_migrations (
            filename TEXT PRIMARY KEY,
            checksum TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(IncompatibleStateError, match="version"):
        SQLiteState.open(database_path)


def test_failed_pending_migration_preserves_database_bytes(tmp_path, monkeypatch):
    from millrace_os.storage import database

    path = tmp_path / "state.sqlite3"
    SQLiteState.open(path).close()
    before = path.read_bytes()
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    original = database.MIGRATIONS_DIR.joinpath("001_initial.sql").read_bytes()
    (migrations / "001_initial.sql").write_bytes(original)
    (migrations / "002_bad.sql").write_text(
        "CREATE TABLE partial_change (id INTEGER);\nINVALID SQL;\n"
    )
    monkeypatch.setattr(database, "MIGRATIONS_DIR", migrations)
    with pytest.raises(IncompatibleStateError, match="preflight"):
        SQLiteState.open(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("state.sqlite3-*"))


def test_sidecars_are_refused_without_mutating_any_bytes(tmp_path):
    path = tmp_path / "state.sqlite3"
    SQLiteState.open(path).close()
    sidecar = tmp_path / "state.sqlite3-wal"
    sidecar.write_bytes(b"preserve damaged or active WAL")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(IncompatibleStateError, match="sidecars"):
        SQLiteState.open(path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
