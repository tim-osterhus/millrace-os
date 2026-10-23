# Independent OS service foundation

Python 3.12, FastAPI 0.116.1, Pydantic 2.11.9 and uvicorn 0.35.0 are the
foundation stack for Millrace OS 0.23.1. The base service has no Core or Plus dependency. Install `millrace-os[router]==0.23.1` for the optional public controls.
The wheel includes OS migration resources and needs no source checkout.

Release publication requires `uv.lock` with public-registry dependencies and exact Core, Plus, and OS 0.23.1 selections. CI checks lock freshness with `uv lock --check --no-sources --python 3.12` and installs the frozen development and router environment. Tag publication requires a present, fresh lock. Dependency sources use public PyPI; the OS project itself is the local root.

Builds use the fixed `SOURCE_DATE_EPOCH=1580601600`, independent of publication-control pin commits. Build with `uv build --force-pep517`.

`millrace-os serve` starts the foreground service on the configured explicit
loopback host and nonzero port (default 127.0.0.1:8731). A collision is an error;
the service never chooses another port. `health` returns minimal public liveness.
Authenticated state, recovery, schema, and router reads use the local trust boundary described in `api-foundations.md`.

`stop` requires the owner-only JSON daemon identity record. On supported macOS,
libproc supplies native process birth seconds and microseconds, fenced around
command inspection. UID, PID, parent, command and birth identity must match an OS
serve command before SIGTERM. Observation failures and changed identity are errors;
success requires observed exit within the configured observation timeout (five seconds by default). Other platforms fail closed.
Service shutdown owns its bounded Core call workers and drains them before closing HTTP and local storage. Unrelated provider-hosted processes retain their own lifecycle.

Configured lexical ancestors and targets reject symlinks, foreign ownership and
unsafe writable parents before mutation. Existing files are never chmodded.
State schema and migrations are validated in memory before writable opening.
Corrupt, incompatible and failed-migration stores remain unchanged. Existing
SQLite sidecars are conservatively refused for offline recovery, without opening
or changing them; this foundation does not implement crash-WAL recovery. It does
not restore old bytes over concurrent writers. Runtime directories must remain
owned by the operator; these checks are not a defense against an adversarial
process running with the same UID and racing filesystem or signal operations.

The retained foundation and headless router mapping have separate verification evidence. The historical Release 1 ledger does not qualify the current router release, all native providers, or the historical 89 integration gates. See [router operation](router-operation.md) for current controls.
