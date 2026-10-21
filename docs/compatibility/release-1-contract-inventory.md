# Release 1 compatibility inventory

**Status:** frozen lower-layer compatibility record for Packets 02–07.
**Released authority:** the exact pins and content digests below, not a
mutable checkout. **Inventory format:** Release 1 contract inventory v1.

This record covers released Millrace, Millrace Plus/LAD, installed package
identities, and stock tmux. It records supported public reads/actions/events,
stable identity fields, invocation shapes, freshness and failure behavior,
redaction boundaries, and explicit refusal rules. A later adapter must cite a
row here for every field, action, and provider operation it uses.

## Released identity and artifact digests

| Authority | Released identity / revision | Exact artifact or schema | SHA-256 digest |
| --- | --- | --- | --- |
| Millrace runtime | `millrace-ai==0.22.2`, tag `v0.22.2`, commit `b84f50da9b0f07ce4964f61dd69feb548e2ef754` | `docs/v0.22-compatibility.md` | `sha256:251d5b86c974c13f2fdaaa18b2a63d11e690dbb064bdcda739fec641d72f3c96` |
| Millrace daemon | same as above | `docs/daemon-lifecycle.md` | `sha256:4b69589f876dfd12fa103beeeff0295b6093e20555fa4ec2c2b66d9cfc0eceae` |
| Millrace runner contract | same as above | `docs/codex-runner.md` | `sha256:2e15dbbed9c8cad07a02b9dfda6f163a75394e73a2a8175d5f475b25b4d333ea` |
| Millrace public schema | same as above | `src/millrace/contracts/schema.py` | `sha256:3298ebbef30268a064de7eef6fda8d56f62c58c0f1adc265b28ea95c1f8370ba` |
| Millrace runner schema | same as above | `src/millrace/contracts/runner.py` | `sha256:a9af8519d7a362665162fbd0f8a2f05e1494432da5509a37f307d962feebb04f` |
| Millrace operator-wait schema | same as above | `src/millrace/contracts/operator_waits.py` | `sha256:a0dedeb3585b0675afc8ad8cb40a193d31e571cdb3d685f092e4ac7629b008eb` |
| Millrace Plus package | `millrace-plus==0.22.2`, tag `v0.22.2`, commit `7468ebb8f6171a38e63001680ca7e9a5d3856e9b` | `docs/release.md` | `sha256:e6459f444373b994118b60806d0ae4c14742c37c902d418d9bf3e3f1b8843fac` |
| LAD evidence | snapshot commit `9eb62b6f7e10652164830995b13e4698629cb7fd` | `README.md` | `sha256:3f29527ddb4c5c604e527a0005d1d6d69e90a77bb8345ac09d0215dc3b1730a6` |
| LAD Builder asset | same as above | `millrace/execution/builder-stage.md` | `sha256:9743a99e0eabb268d9f1d3071cced390b9e0b8f3c2ff28151760f2579eea4ff4` |
| Millmux substrate | `v0.3.0`, commit `afcb476c346b8c7a18a9ad616c3e4e9ef7b4e109` | `README.md` | `sha256:e0eecdb0695fc66ab6fd4b2ddfbe3b301a1eda4d98ad5e7fd9f503f8df82db96` |
| Millracer evidence | `v0.3.0`, commit `49659e0f5e417b839c8ec9d92aaf0b2ff9463119` | `README.md` | `sha256:f671a2f88cab7cd242a754e15ae0ef307eea83ec40e5460f330339c574ff1058` |
| Millrace OS package metadata | Release 1 `millrace-os==1.0.0` | `pyproject.toml` | `sha256:967338ec7791d203702451a501558dfca1956f0f8f5eb4ec870779169985b579` |
| Millrace OS resolution lock | Release 1 Python `>=3.12`, no runtime dependencies; dev checks are locked separately | `uv.lock` | `sha256:fa10cc6c133a01134ad446066f6c94246cbc3b7e49d5451a2d6fb51bff24c12a` |
| Packet 01 migration | Millrace OS Release 1 schema v1 | `src/millrace_os/storage/migrations/001_initial.sql` | `sha256:d4b0d0d3fcc887e0922d7501f565187d0d7a82f2c7a9e1c82f5d0f4fa93257da` |

The installed-package identity used by later adapter qualification is the
tested combination documented by Millrace Plus: `millrace==0.22.2` selects
`millrace-ai==0.22.2`, `millrace-plus==0.22.2`, and `millforge==0.1.0`.
Millrace OS itself has no runtime dependency on those packages. The stock
tmux evidence in this environment is `tmux 3.7b`; later tests must record
the `tmux -V` output rather than assume a fork or a private build.

## Public contract matrix

| Contract | Supported public reads | Supported actions / events | Stable identity fields | Freshness and failures | Redaction boundary |
| --- | --- | --- | --- | --- | --- |
| Millrace v0.22 selected workflow/runtime | `millrace status`, `runs show`, `trace show`, `doctor`, finite `runs follow --after-sequence N`, package doctor | Explicit queue/run/wait/intervention commands, selected daemon dispatch, durable runner-session events | run ID, work-item ID, selected-plan fingerprint, stage kind, generation, runner-session ID/fencing, event sequence | Read-only projections are current at command time; corrupt/missing authority and stale generations are typed refusals. No schema 6/7/8 upgrade is attempted. | Public projections exclude provider secrets and private diagnostics; evidence is not workflow truth until validated by the selected plan. |
| Millrace run-scoped pause/resume | current dispatch suspension/wait/intervention projection and exact identity | `millrace dispatch suspend` and `millrace dispatch resume` with the current plan fingerprint, suspension ID, input ID, and reason; `waits.resume` for a selected operator wait | plan fingerprint, suspension ID, input ID, actor ID, reason, generation, timestamps | Exact replay is idempotent; wrong identity, plan mismatch, stale generation, already suspended/resumed, or missing wait refuses without mutation. Suspension gates new claims only; accepted work keeps authority. | Reasons and projections are public text; credentials, provider usage, and private state are never carried in the command. |
| Millrace daemon launch/readiness/stop | `millrace status`, `millrace doctor`, stop summary, durable runner-session projection | Launch: `millrace run daemon [--budget-id ID ...]`; readiness: observe OS liveness plus bounded daemon progress/status; graceful stop: one `SIGINT` or `SIGTERM` to PID from `.millrace/daemon.lock`, then inspect stop summary | daemon lock owner, PID, run/session ID, selected plan, fencing, cancellation phase, cleanup, orphan risk, stop reason | Lock is diagnostic, not a control channel. Signal stop is truthful only after owned cleanup; live/unverifiable sessions can yield `orphan_risk`. A second signal cannot replace the first request. | Stop summaries omit secret/config values and do not claim unsupported cleanup. Shell/tmux state is not lifecycle authority. |
| Millrace runner wrapper | selected runner-session and event projections; Codex wrapper evidence envelope | Adapter invocation is only through the released runner contract; provider callbacks/webhooks are absent | runner kind, session ID, generation/fencing, invocation ID, input digest, outcome, evidence classification | Missing or contradictory provider usage is refused when governed; unsupported runner kinds return refusal. Runner output is evidence only. | Adapter configuration and tokens stay outside artifacts; redaction is applied before evidence reaches durable public projections. |
| Millrace Plus package data | installed resource/package metadata and workflow manifests | Import/select package data through Millrace public package commands; package data does not execute | distribution/version, package ID, workflow ID/version, asset ID/digest | Wheel installation/resource discovery must use the exact member versions; package absence, changed digest, or unsupported package is refused. | Skills and workflow prompts contain no credentials and cannot grant capabilities, plugins, or provider authority. |
| LAD prompts | read-only Markdown stage material from the pinned package snapshot | No runtime action; prompts produce evidence for the owning workflow | workflow/stage asset ID, content digest | Asset text is not runtime truth and cannot change routing. Missing/contradictory selected asset is refused. | Do not embed secrets or local credential paths in prompts or artifacts. |
| Stock tmux 3.7b | `tmux -V`, `has-session`, `list-sessions -F`, `capture-pane -p -J -t TARGET` | Later adapter may use supported stock commands: `new-session -d -s NAME`, `send-keys -t TARGET ...`, `kill-session -t NAME` | session name, target, window/pane target, command exit status, captured observation cursor if supplied by adapter | Each invocation is fresh and bounded; missing server/session/target, timeout, or nonzero status is a typed refusal. tmux output is observation, never workflow authority. | Do not persist pane text as durable Millrace state, infer success from text, type around absent APIs, or use a private tmux database. |
| Millmux v0.3.0 | typed `session.list/status/inspect`, `host.doctor`, bounded `observation.session_events`, `observation.logs`, `observation.screen`, and artifact/retention status | typed `session.start/stop/kill/archive/purge`, `host.reconcile`, attach ownership, input, resize, and bounded event/log operations | session ID, lifecycle, generation, registry revision, operation receipt/idempotency key, stream/cursor, owner epoch | Public protocol is local and owner-authenticated; unknown outcome is not replayed automatically. Client disconnect does not stop host/worker/child. | Use protocol responses only; never read segmented files, private metadata, child output, or control sockets directly. Millrace OS may refuse the adapter if the pinned public protocol is unavailable. |

## Exact pause/resume contract

The released run-scoped contract is intentionally narrow. A suspend request
must name the current selected-plan fingerprint, a new caller input ID, the
actor ID, and a nonblank reason. A successful response records one immutable
suspension identity and gates only new claim acceptance. Resume must name the
exact plan fingerprint and suspension ID, plus a new input ID, actor ID, and
reason. Replaying the same accepted input is idempotent; a changed payload,
wrong suspension identity, stale generation, already-resumed state, or absent
wait is refused without changing durable state. The response/event carries
the plan fingerprint, suspension ID, input ID, actor, reason, generation, and
recorded timestamps. Packet 01 does not implement this contract; it only
freezes it for later adapters.

## Exact daemon lifecycle contract

The released launch command is:

```text
millrace run daemon --budget-id ID [--max-wall-seconds N] [--max-invocations N] [--max-total-tokens N]
```

The command opens and integrity-checks the released state, acquires the
diagnostic `.millrace/daemon.lock`, reconciles sessions, and dispatches only
selected legal work. Readiness is not inferred from the lock: the caller must
observe the recorded PID as live, bounded daemon progress/status, and a
selected run/session projection. Graceful stop is one POSIX `SIGINT` or
`SIGTERM` to that PID, followed by a bounded wait for exit and inspection of
the JSON stop summary. Expected clean responses expose `stopped_reason`,
`last_result`, bounded counters, diagnostics, and the affected runner-session
projection. Refusal modes include unsupported/future/corrupt state,
contradictory identity, dependency not ready, unsupported adapter, and
`runner_session_orphan_risk`; none is repaired by editing files or scraping a
terminal.

## Packet 01 service contract

Packet 01 freezes its own lower-layer launch and readiness surface for the
foundation tests:

```text
millrace-os serve
GET http://127.0.0.1:8731/
GET http://127.0.0.1:8731/health
GET http://127.0.0.1:8731/ready
kill -TERM "$(cat "$XDG_STATE_HOME/millrace-os/millrace-os.pid")"
```

Expected healthy responses are HTTP 200 and JSON `{"status":"ok"}` on
`/health` and `/ready`; `/` serves an immutable HTML recovery shell. A
corrupt or incompatible local database keeps `/` and `/recovery` available,
returns HTTP 503 with `status=recovery` and a typed `error.code`, and does not
replace or rewrite the bytes. A bound default port yields typed
`port_collision`; unsafe credentials yield owner-only permission refusal;
an explicit non-loopback or currently unpinned Unix-socket bind yields
`configuration_error`. Packet 01 does not emulate later Millrace, Millmux,
tmux, or provider behavior.

## Compatibility refusal rules

- Do not read or write Millrace private runtime databases, CAS, lock files,
  pane output, Millmux segmented streams, or historical state files.
- Do not revive v0.20/v0.21 aliases, schema migration, automatic conversion,
  old config, or a compatibility profile.
- Do not make Millmux, Tauri, Node, Cargo, a remote service, or a second
  workflow scheduler a startup prerequisite.
- Do not infer action success from shell output, pane typing, process liveness
  alone, or an unpinned package asset.
- If a needed released public field/action/event is absent or its digest does
  not match this record, return a typed refusal and preserve the evidence.

## Inventory maintenance

This file is a release artifact consumed by name by Packets 02–07. Any
amendment must update the release pin, artifact/schema digest, invocation
shape, identity fields, freshness/failure semantics, and refusal rule in the
same change. A mutable source checkout, generated terminal transcript, or
private database is not a valid amendment source.
