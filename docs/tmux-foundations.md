# Stock tmux provider foundation

The retained OS-P03 foundation supplies a small in-process reader for the pinned stock tmux executable.
`TmuxProvider` accepts a `TmuxConfig` with an absolute executable and an
explicit absolute Unix socket. A missing socket is unavailable; the provider
never falls back to tmux's default server. The provider default uses
the recorded 3.7c executable and SHA-256
`1d101560ca3d4fb6f86bb53e6d9946e7b98444343d1fdab4a2a6e72980e232a5`.

The configured server has one shared session, named `millrace` by default.
Fixture or later lifecycle code owns creation of that server, session, windows,
panes, and user options. The read provider only invokes bounded `tmux` argv
commands for version, session, window, and pane formats. It does not use a
shell, inspect terminal text, send keys, create or kill a session, attach a
client, or expose arbitrary command execution.

Each managed window carries these stock user options:

| Option | Meaning |
| --- | --- |
| `@millrace_workspace_id` | Stable workspace identity |
| `@millrace_instance_id` | Stable Millrace instance identity |

The options are authoritative identity fields. Window labels and indexes are
presentation facts. A labeled window must have both options and exactly one
pane; duplicate workspace or instance IDs, partial options, and malformed
formats refuse reconciliation. Unlabeled windows may remain in the shared
session and are reported as windows without a managed mapping.

`reconcile()` always reads a new server/session/window/pane inventory. The
inventory carries the exact socket, server PID, tmux version and executable
digest, session/window/pane IDs, observed-at timestamp and `current` freshness.
`last_inventory` is only a replaceable projection and is never used as a
substitute for a fresh read. `hint()` records a hook/change hint and marks the
provider `reconciling`; callers still invoke `reconcile()`. `reset()` drops the
projection and returns the provider to `reconciling` without touching tmux.

Pane liveness, dead status, exit status and pane PID remain tmux facts. They do
not produce workflow `running`, `paused`, or `completed` states. A dead pane
can therefore be retained as process evidence while workflow truth remains
unavailable to this packet.

`attach_descriptor(workspace_id, instance_id)` first reconciles and returns a
read-only `AttachDescriptor`. It contains an argv array and `shlex.join`
display string together with exact server generation, session, window, pane,
workspace and instance identities. The display is for manual copying only;
the module has no method that executes it. `validate_attach_descriptor()` reads
again and refuses a missing target, changed target IDs, socket mismatch, or a
different server generation. The generation includes socket path, server PID,
session creation time, tmux version and executable digest, so a restarted
server cannot reuse an old descriptor merely by reusing window or pane IDs.

Expected failures are typed with stable codes, including executable missing or
hash mismatch, unsupported version, unavailable server/session/socket,
malformed output, command failure, timeout, output overflow, invalid topology,
duplicate mapping and stale target. Health reports these states as structured
availability data; reconciliation raises the typed failure before returning
an inventory.

The provider has no Core workspace/run mapping and no mutation or lifecycle
authority. Positive ensure, relaunch, pause, resume, workflow-state projection,
and general native qualification are outside this read-only provider. Current workflow operation uses [public router controls](router-operation.md). The Release 1 inventory retains its separate historical 3.7b evidence; neither record qualifies every tmux installation.
