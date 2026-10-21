# Release 1 legacy disposition and ownership ledger

**Status:** frozen for Packet 01. **Authority:** read-only evidence only; this
document does not authorize importing a private runtime, mutating a historical
checkout, or reconstructing an unavailable contract.

## Method and decision vocabulary

The inventory was performed against the frozen checkouts under
`/Users/timinator/Desktop/Millrace-Dev/dev/` and `ref/`. The historical
checkouts remain read-only. A disposition means:

| Decision | Meaning in Release 1 |
| --- | --- |
| **retain** | Keep the concept or public boundary, with a new implementation owned by the named Packet. |
| **adapt** | Preserve the user-visible contract only after mapping it to a pinned released public contract. |
| **preserve as evidence** | Keep the historical source, test, fixture, or digest as provenance; it is not runtime input. |
| **retire** | Do not copy, import, emulate, or provide a compatibility shim for the surface. |

Every row has a current owner. A later Packet may consume this ledger and the
compatibility inventory, but it may not silently promote evidence to runtime
authority.

## Historical source and contract ledger

| Evidence surface | Evidence reviewed | Disposition | Reason | Current owner | Safety / negative-test evidence |
| --- | --- | --- | --- | --- | --- |
| Millrace compiler, immutable selected plans, graph routes, terminal actions, and schema declarations | `dev/source/millrace/src/millrace/compiler/`, `contracts/`, `kernel/`; released v0.22.2 tag | adapt | Later packets need selected-plan identity and route facts, not the old implementation or private tables. | Packet 02 (contracts) | Refuse missing/changed identity, unsupported schema, stale generation, and unsupported compatibility profiles. |
| Durable queues, runs, waits, interventions, traces, artifacts, and runner sessions | `dev/source/millrace/src/millrace/substrate/`, `kernel/`, `operator/`; `docs/how-millrace-works.md` | adapt | These are lower-layer authority. Packet 01 owns only OS state needed to boot; it must not create a second workflow scheduler. | Packets 02–06 | No private runtime database reads or writes; state corruption, future schema, and contradictory authority remain refusal paths. |
| Public CLI reads and actions | `docs/v0.22-compatibility.md`, `docs/errors.md`, `docs/daemon-lifecycle.md`, `src/millrace/adapters/cli/` | preserve as evidence | The exact released command and error contract is pinned in the compatibility inventory. Packet 01 does not reimplement it. | Packets 02–06 | Absent commands return a current refusal; v0.20/v0.21 aliases are not restored. |
| Runner boundary and provider-hosted process ownership | `src/millrace/adapters/runner_contract.py`, `codex-runner.md`, `millforge-runner.md` | retain | Provider processes are injected external dependencies; OS shutdown closes only OS-owned resources. | Packet 01 boundary; Packets 05–06 adapters | No provider callback, credential, process, or external effect is started or stopped by foundation shutdown. |
| Millrace Plus workflow packages and advisory skills | `dev/assets/millrace-plus` v0.22.2 release line and package tests | adapt | Package data may be imported by later validation, but it cannot register executable authority or a plugin. | Packet 02 | Package identity and member version pins are exact; absent package behavior is refused. |
| LAD stage prompts, including Builder/Checker/Fixer/Recovery roles | `dev/workflow/lad/millrace/{planning,execution,learning}/` | preserve as evidence | Prompt text explains workflow intent; it is not a kernel scheduler, runtime database, or authorization source. | Packets 02–07 | No stage prompt can change queue, route, approval, capability, or durable state by itself. |
| Historical tests, fixtures, schemas, and release QA | `dev/source/millrace/tests/`, `dev/assets/millrace-plus/tests/`, `dev/source/millmux/docs/`, `ref/` | preserve as evidence | Tests identify safety and negative cases to carry forward; their fixtures are not copied into the new service. | Packet 01 evidence; consuming Packet | Negative cases include corrupt/future state, stale identity, unsafe permissions, redaction, orphan risk, and unsupported commands. |
| Redaction and secret boundaries | Millrace runner configuration docs, lower-layer error matrices, and legacy agent evidence | adapt | Keep no-secrets-in-artifacts, owner-only local credentials, and public-safe projections. | Packet 01 for credentials; Packet 04 for evidence | Credential values never appear in health, logs, docs, or artifacts; provider secrets and adapter config secrets are refused. |
| Recovery and restart behavior | `docs/daemon-lifecycle.md`, runner-session architecture, Millmux lifecycle QA | retain | Restart must classify durable state and leave external workers/processes alone unless an owned public contract later proves a supported action. | Packet 01 for OS state; Packets 05–06 for adapters | Corrupt/incompatible state exposes typed recovery; unverified external effects are never guessed successful. |
| Mission Control information architecture | historical `millrace-web` references, operator/status/doctor projections, and Millmux cockpit QA | preserve as evidence | The Release 1 browser endpoint starts with an immutable health/recovery shell; normal Mission Control is later scope. | Packet 06 | No UI-only state becomes authority and no Tauri sidecar is required to boot the foundation. |
| Millracer | `dev/harness/millracer`, tag `v0.3.0`, README and release tests | preserve as evidence; retire as runtime dependency | Millracer remains historical QA/harness evidence. It is not a Release 1 runner, provider, scheduler, or compatibility target. | Packet 01 ledger; no runtime owner | No Millracer import, process launch, credential use, or v0.20/v0.21 compatibility is permitted. |

The obsolete **second-runtime** assumption is explicitly retired; the matrix
and rules below use that term as the boundary label for audits and negative
tests.

## Explicit obsolete assumptions

| Assumption | Disposition | Refusal rule |
| --- | --- | --- |
| A second runtime (Node, Cargo, Tauri, or a sidecar) is required to run Millrace OS | retire | The wheel and service must run on Python 3.12 plus the standard library. A missing later UI does not block health/recovery. |
| Millmux is mandatory for foundation startup | retire | Packet 01 starts without Millmux. A later adapter may consume only its pinned public protocol; no private storage or CLI scraping substitutes for it. |
| Tauri-only browser/control surface | retire | The fixed loopback HTTP shell is the only Packet 01 browser surface. Native packaging is later scope. |
| v0.20 or v0.21 state/config/CLI compatibility | retire | Existing old state is not read, migrated, repaired, or rewritten. The service refuses incompatible state visibly. |
| Legacy scaffold or copied runtime database | retire | No historical scaffold or private database file is copied into `src/millrace_os/` or tests. |

## Ownership boundary for Packet 01

Packet 01 owns only the standalone package metadata, configuration and
credential paths, OS-local SQLite state, recovery shell, service lifecycle,
and this evidence ledger. Packet 01 does not own DTOs, workflow routes,
providers, actions, events, Mission Control, qualification, candidate
authoring, remote access, or dynamic plugins. Later packets must consume
`docs/compatibility/release-1-contract-inventory.md` as a named input.
