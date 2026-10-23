# Headless router operation

The optional [MCP bridge](mcp.md) reuses these controls and saved request envelopes.
Direct LAD starts require an existing server-local project `request.md` with the
intended bytes; missing input yields workflow-owned `INTAKE_MISSING`. MCP adds no
upload, draft Apply or automatic acceptance/continuation tool. Keep the manual
Apply journey below, then explicitly accept the pending revision and request
continuation only when the resulting Core state requires it.

Millrace OS 0.23.1 maps `/api/v1/router` to public Plus/Core controls. Core owns graph semantics, durable instances, runs, receipts, planes, seams, and effective writer facts. OS keeps no second run registry or scheduler.

Install `millrace-os[router]==0.23.1` in Python 3.12 or later. Its optional dependency range is `millrace-plus[router]>=0.23.1,<0.24.0`. The selected release uses matched Core, Plus, and OS 0.23.1 artifacts. The public execution contract must be `millrace-router-execution/v2`; version equality alone is insufficient. Base installation supports service health, trust, and recovery without Core or Plus. Router calls load optional components lazily and refuse unavailable or incompatible contracts.

Start the service with explicit named homes:

```bash
millrace-os serve --router-state-home main=/absolute/path/to/core-state
```

Repeat `--router-state-home NAME=PATH` for additional homes. Names must be unique. Canonical path aliases are refused. `MILLRACE_OS_ROUTER_STATE_HOMES` accepts an equivalent JSON object mapping names to paths. HTTP clients select a configured name, never an arbitrary Core state root.

The router extra requires Plus 0.23.1 for this release's bundled LAD workflow, subworkflow commands, Paperclip bridge, and OpenCode adapter.

## Inspect and submit

```bash
millrace-os router instances --home main
millrace-os router inspect --home main --instance INSTANCE_ID
millrace-os router inspect --home main --instance INSTANCE_ID --run RUN_ID
millrace-os router validate --home main --instance INSTANCE_ID --run RUN_ID
millrace-os router request --home main --file saved-request.json
millrace-os router request --home main --request-id REQUEST_ID --instance INSTANCE_ID --run RUN_ID
```

Request files contain the exact public `millrace-router-request/v2` envelope. Its fields are `format`, `operation`, `instance_id`, `run_id`, `plane_id`, `request_id`, `expected_state_version`, and `payload`. Preserve the file for explicit replay. OS never refreshes a stale version or invents a plane target.

Run and plane pause/cancel use a null expected version. Other controls use the applicable positive instance-settings or run-state version. Create uses version zero and null target IDs. Core validates operation payloads, targeting, idempotency, and refusal rules.

HTTP 202 reports a queued Core receipt or an owned OS call whose receipt is not yet available. HTTP 200 can report an applied or refused receipt while its holder call remains active. Inspect the receipt and call phase separately. Returning an HTTP response never releases controller ownership.

`GET /api/v1/router/requests/{request_id}` observes an existing call. Optional known `instance_id` and `run_id` permit public receipt lookup after OS observation loss. This GET never replays a request. An unavailable observation returns `observation_unavailable`, which does not claim Core lost the request.

Each instance has one foreground slot. While a sibling plane's holder call occupies it, `resume_plane`, `retry_invocation`, and `redirect_plane` can return HTTP 429 `router_busy`. Wait for that call to return and join, inspect the current Core version, then submit the intended request explicitly. OS does not refresh versions or retry controls automatically.

Core reads have `authority: core-state-home`, their exact public data, and an observation time. Receipt stop targets remain immutable lists of invocation-ID and plane-ID strings at the receipt version. A later run inspection reports current writer facts separately.

## Author without a model

```bash
millrace-os router catalog list --home main
millrace-os router catalog init --home main --selector bundled:NAME --destination /absolute/path/to/workflow
millrace-os router draft create --home main
millrace-os router draft put --home main --draft DRAFT_ID --path workflow.json --file ./workflow.json
millrace-os router draft put --home main --draft DRAFT_ID --path scripts/check.py --file ./scripts/check.py
millrace-os router draft read --home main --draft DRAFT_ID
millrace-os router catalog copy --home main --draft DRAFT_ID --destination /absolute/path/to/independent-workflow
```

Choose a selector returned by `catalog list`. Catalog init/copy calls Plus's public ownership checks. Draft files live under the OS state directory, outside watched workflow and project folders. Initial authoring copies the draft to an independent folder, then uses a normal Core create request. Core supplies graph diagnostics.

Catalog endpoints report Plus catalog refusals as `catalog_*` HTTP 409 errors. A catalog ownership refusal inside `execute_request` currently surfaces as generic `router_call_failed` HTTP 503; that response does not establish a Core receipt.

Uploads contain `{path, content_base64}` entries. Names must be plain relative regular-file paths. Traversal, symlinks, case aliases, nonregular files, and `.millrace-router-owner` paths are refused. File-set and per-file PUT routes accept at most 1 MiB of encoded JSON. Ordinary mutations retain the 4096-byte limit. Split larger authoring changes into bounded per-file requests.

## Publish and continue explicitly

Use an existing instance for settings context and a run ID for compatibility validation:

```bash
millrace-os router draft validate --home main --draft DRAFT_ID --instance INSTANCE_ID --run RUN_ID
millrace-os router draft apply --home main --draft DRAFT_ID --instance INSTANCE_ID --run RUN_ID --base-state-version VERSION --request-id REQUEST_ID
```

Apply freezes the complete file set and records its target and hash. It acquires the shared publication lock, requests public pause, and waits for the exact applied receipt, stopped effective writers, and final owned-call drain. It validates the frozen draft against the observed run version before writing watched files. Published validation must produce the same candidate hash before Core submission.

Published files are replaced with mode 0600; newly created directories use 0700. Existing workflow directories must belong to the service user and disallow group/other writes; read/execute access for others is permitted. An unsafe directory can cause `unsafe_path` after earlier files were written, leaving the frozen publication for repair.

Apply leaves the run paused with a pending revision. Later explicit `accept_revision` and continuation requests use saved envelopes and current versions. Core acceptance clears the run gate when permitted and can continue runnable planes. Request another continuation only when the resulting Core state requires it. Individually paused planes remain paused until explicitly continued. OS never performs those later requests automatically.

A long Apply returns HTTP 202 with a local owned-call observation. Repeat the exact Apply body and request ID to retrieve its final result while that observation remains available. This reports the same call and does not repeat publication. Local observations last only within the service lifetime. Completed observations expire 300 seconds after call return, and the cache retains at most the last 128 joined calls. Active owned calls do not expire.

Observation GETs report phase and Core receipt, without a later local result or error detail. Publication records and frozen files are durable evidence separate from that cache. After observation loss, inspect Core and use explicit repair if publication remains incomplete. A repair that outlasts its initial response also returns an observation; repeating repair after completion does not retrieve the previous local result.

A partial publication retains an in-progress marker and frozen bytes. Ordinary cooperating OS mutations refuse `publication_incomplete`. Repair uses only that recorded snapshot:

```bash
millrace-os router draft repair-publication --home main --instance INSTANCE_ID
```

Nonterminal repair repeats pause, quiescence, validation, install, hash verification, and submission. It stays paused/pending. If cancellation made the target terminal, repair validates files for a fresh run and preserves the terminal results. It never submits or resumes that terminal run. Missing or corrupt frozen evidence refuses repair.

If Apply encounters an uncertain writer, it can refuse with `publication_writer_unresolved` and retain its marker. Verify that exact writer has stopped, then submit public `reconcile_invocation` with the current version and the operator's evidence. Reconciliation bypasses the publication guard while retaining its control slot and normal closing-intake restriction. Repair can then finish paused/pending; OS still never accepts or resumes automatically.

All cooperating OS services share `<state_home>/os-client/publications/<instance_id>/`. Ordinary control admission holds the publication lock until its exact Core receipt or resolved call outcome establishes ordering. Pause, cancel, reconciliation, and read bypass this lock. Admission may create a local publication directory for a nonexistent 32-hex instance ID before Core refuses it. Non-hex IDs receive OS `invalid_instance_id`. Concurrent direct-Core or file edits during OS publication are unsupported.

## Stop and recover

The first SIGTERM/SIGINT closes ordinary router intake and starts one drain coordinator. Reads, diagnostics, and stop requests remain available. Every potential holder stays owned until its public call returns and is joined. Core's frozen invocation timeout or earlier run deadline governs dispatched work.

A queued start can remain unresolved without a live holder, for example after a competing holder was killed. Its watcher keeps shutdown waiting, and a second signal cannot target a run ID that does not yet exist. Submit a public pause/cancel on that instance so a controller enters, processes the queued start, and stops its run. A persistently unreadable Core store also keeps shutdown closing until readable truth is restored; a read failure never means writers stopped. Exact `core_unknown_run` and `core_invalid_id` refusals identify no matching target, but every actual owned call must still return and join. `core_instance_integrity` can mean an absent or corrupt instance, so it remains unknown and keeps shutdown closing while reads and stop intake remain available.

A second deliberate signal requests Core cancellation. OS never directly kills native children. Forced process death leaves Core's normal uncertainty and holds visible. The retained OS SQLite foundation refuses crash WAL/SHM sidecars and exposes its recovery shell instead of normal router routes. It preserves those files. An operator can explicitly select a fresh owned OS state directory with the same configured Core home to inspect and recover Core state. This does not recover old OS drafts or sessions automatically. The shared Core-home publication marker still blocks incomplete publication across OS state directories. No worker is recreated from an OS observation.

`millrace-os stop --timeout SECONDS` verifies the recorded daemon identity and observes exit. Its timeout can expire while draining continues. A timeout never prints `stopped` or authorizes escalation to a kill.
