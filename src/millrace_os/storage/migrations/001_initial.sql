CREATE TABLE millrace_os_schema (
    schema_version INTEGER NOT NULL,
    compatibility_id TEXT NOT NULL
);

INSERT INTO millrace_os_schema (schema_version, compatibility_id)
VALUES (1, 'millrace-os-release-1');

CREATE TABLE millrace_os_migrations (
    filename TEXT PRIMARY KEY,
    checksum TEXT NOT NULL
);
