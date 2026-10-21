# Independent OS foundation candidate

Python 3.12, FastAPI 0.116.1, Pydantic 2.11.9 and uvicorn 0.35.0 are the
foundation stack. `uv.lock` pins transitive artifacts. Build with `uv build`;
the wheel includes the OS migration resources and needs no source checkout.

`millrace-os serve` starts the foreground service on the configured explicit
loopback host and nonzero port (default 127.0.0.1:8731). A collision is an error;
the service never chooses another port. `health` returns minimal public liveness.
All state, recovery and schema HTTP reads remain closed pending OS-P02 trust.

`stop` requires the owner-only JSON daemon identity record. On supported macOS,
libproc supplies native process birth seconds and microseconds, fenced around
command inspection. UID, PID, parent, command and birth identity must match an OS
serve command before SIGTERM. Observation failures and changed identity are errors;
success requires observed exit within five seconds. Other platforms fail closed.
No provider or external process is stopped by service shutdown.

Configured lexical ancestors and targets reject symlinks, foreign ownership and
unsafe writable parents before mutation. Existing files are never chmodded.
State schema and migrations are validated in memory before writable opening.
Corrupt, incompatible and failed-migration stores remain unchanged. Existing
SQLite sidecars are conservatively refused for offline recovery, without opening
or changing them; this foundation does not implement crash-WAL recovery. It does
not restore old bytes over concurrent writers. Runtime directories must remain
owned by the operator; these checks are not a defense against an adversarial
process running with the same UID and racing filesystem or signal operations.

This is a foundation candidate, not a released OS qualification. Authentication,
full recovery, Core mappings, tmux and all 89 released integration gates remain
outside this packet (NOT_RUN).
