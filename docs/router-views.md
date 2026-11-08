# Bounded router views

The bounded views give a finite read of configured Core state homes. They are
separate from the full headless reads in `router-operation.md`. Core still
owns every result. OS selects the named home, checks the request shape and
wraps the Core result unchanged. It adds no cache, snapshot store or index.

Each view needs Core's public bounded inspection
(`millrace-router-inspection/v1`), forwarded by the Plus control facade. If
Plus or Core is missing, or the forwarded marker differs, the view refuses
`router_inspection_unavailable` (503). There is no fallback to the full
inspection. The existing full reads, requests, drafts and catalog keep working
with an older Plus/Core that lacks this capability. Plus itself reports the
older capability as `core_contract_missing` or `core_contract_mismatch`.

## Routes

Every route uses the normal bearer or browser-session authentication and
host/origin checks. Each one also has a fixed query allowlist. A repeated or
unlisted query key refuses `invalid_request` (422) before any read.

| Route | Query or body | Core operation |
| --- | --- | --- |
| `GET /api/v1/router/homes` | `limit`, `cursor` | none (configured names only) |
| `GET /api/v1/router/views/instances` | `home`, `limit`, `cursor` | `list_instances_page` |
| `GET /api/v1/router/views/instances/{instance_id}` | `home`, `limit`, `cursor` | `inspect_instance_page` |
| `GET /api/v1/router/views/instances/{instance_id}/runs/{run_id}` | `home`, `section`, `limit`, `cursor`, `request_id`, `invocation_id` | `inspect_run_page` |
| `GET /api/v1/router/views/instances/{instance_id}/runs/{run_id}/revisions/{revision_id}` | `home` | `inspect_revision_graph` |
| `GET /api/v1/router/views/requests/{request_id}` | `home`, `instance_id` (required), `run_id` | strict request observation |
| `POST /api/v1/router/views/validate` | JSON `{home, instance_id, run_id}`; `run_id` is optional or null | `validate_revision_summary` |

The query values follow these rules:

- `limit` is a decimal integer from 1 to 50 and defaults to 50.
- A `cursor` is opaque: 1–128 characters of `A-Z a-z 0-9 _ -`.
- `section` is `summary` (the default), `invocations`, `transitions` or
  `controls`.
- `revision_id` is 64 lowercase hex characters.

Core applies its own selector rules:

- `request_id` selects one exact receipt (controls only, no cursor).
- `invocation_id` selects one exact invocation of the run (invocations only,
  no cursor).

Core refuses an invalid combination with `core_invalid_request`.

## Responses

The homes response is `millrace-os-router-homes/v1`:
`{contract, authority: "os-local", observed_at, items: [{name}], next_cursor}`.

- Names come only from the configured `router_state_homes` and are sorted.
- No state home is read or created, and no configured path is shown.
- The cursor is `h`, then 32 hex characters bound to the exact configured name
  set, then `_` and an offset. Changing the configured names makes an older
  cursor refuse `invalid_request`, so a page never drifts.

The other view routes return `millrace-os-router-view/v1`:
`{contract, home, data, source_provider: "core", authority: "core-state-home",
observed_at}`. `data` is the Core result unchanged, with Core's `format`,
`kind`, `items`, `next_cursor` and `data` fields. This envelope is distinct from
the full read `millrace-os-router-read/v1`.

Every new route measures its response before sending it. The measure is the
compact UTF-8 API encoding, and the limit is `RESPONSE_LIMIT` (1,049,600 bytes):
the 1 MiB Core result bound plus 1,024 bytes of envelope. A larger response
refuses `response_too_large` (409).

Error codes:

| Code | Status | Meaning |
| --- | --- | --- |
| `invalid_request` | 422 | Bad shape, query or homes cursor. |
| `unknown_router_home` | 422 | The home is not configured. |
| `router_inspection_unavailable` | 503 | The bounded capability is absent. |
| `core_inspection_too_large` | 409 | Core refused a budget. Valid history in the same instance can cause this. |
| `core_inspection_changed` | 409 | A cursor no longer matches its target or version. |
| `core_unknown_revision` | 409 | The revision is not part of this run. |
| `core_request_target_mismatch` | 409 | The receipt belongs to a different target. |
| other `core_*` | 409 | Another typed Core refusal. |
| `response_too_large` | 409 | The encoded response exceeds `RESPONSE_LIMIT`. |
| `router_read_busy` | 429 (retryable) | All eight existing read slots are busy. |

Every Core read first verifies the whole owning instance under the
per-instance budget. An exact invocation or receipt lookup can therefore
refuse because of unrelated history in that instance.

## Strict request observation

`GET /api/v1/router/views/requests/{request_id}` is the bounded peer of
`GET /api/v1/router/requests/{request_id}`. It negotiates call-v1 or call-v2
the same way (see `router-call-observations.md`). It returns 200 or 202 under
the same rules, and an unsupported observation version refuses 406 before any
read.

The strict lookup is one indexed Core receipt read under the inspection
budget, made before any OS-cached state is consulted:

1. A Core refusal, such as `core_inspection_too_large`, is returned
   immediately. A cached OS phase or receipt never bypasses it.
2. If OS still owns the exact call, the response is that call's observation.
   It reports a Core receipt only when the strict lookup found a row with the
   call's request digest. Otherwise `core_receipt` is null, for example during
   `admission_pending`. A cached or other-body receipt is never substituted.
3. Without an owned call, a found row is returned as a `core_receipt`
   observation (`holder_joined` is null in v2).
4. The route returns `observation_unavailable` (404) only when there is
   neither an owned call nor a matching Core row.

An instance-targeted Start receipt can be read with the instance alone. It can
also be read with the run it created, through that run's recorded start
request. A queued Start has not yet learned its run, so a supplied run must be
confirmed by a Core row with the call's digest. Any other supplied run refuses
`observation_target_mismatch` (409). A run that did not record this request
refuses `core_request_target_mismatch`.

The strict read never updates, releases or refreshes an owned call. It never
stops observation, cancels work or blocks joins or drain.

## Lifecycle reads stay separate

Default observation keeps using the full headless reads, with exactly their
earlier behavior. This covers:

- the receipt watcher;
- queued refresh and the retired-call fallback in
  `GET /api/v1/router/requests/{request_id}`;
- drain receipt checks and run inspection;
- draft Apply/repair publication receipts;
- the headless CLI commands.

None of these calls the bounded reader, so a view budget refusal cannot stop
Start, stop, join, slot release, drain or publication repair. These lifecycle
reads can exceed the view budgets. They are not covered by bounded-read
evidence.

`RouterClient.receipt(..., bounded=False)` and
`RouterWorkers.observe(..., bounded=False)` select the lifecycle path by
default. Only the strict route passes `bounded=True`. Callers that override
these methods with the earlier signatures continue to work for the default
path.

## Workflow outputs

A trusted operator-side publisher can attach small immutable output records
(`millrace-workflow-output/v1`) to an exact run. A record holds a fixed
plain-text report and, optionally, opaque producer JSON. OS serves these
records through Plus's public helper, `millrace_plus.workflow_views`
(`millrace-workflow-views/v1`). OS does not read the files itself or duplicate
the helper's validation, and it has no workflow-specific schema or code.

The helper is imported only when one of these routes is used. A missing helper,
or one with another version marker, refuses `workflow_views_unavailable` (503).
Listing and reading use stored bytes only. Publishing checks the record's exact
invocation through Core's bounded inspection, so it also needs that
capability (`router_inspection_unavailable` otherwise).

| Route | Query or body | Response |
| --- | --- | --- |
| `POST /api/v1/router/outputs` | JSON `{home, record}`, at most 1,048,576 bytes | `millrace-os-workflow-output-publication/v1` |
| `GET /api/v1/router/outputs/instances/{instance_id}/runs/{run_id}` | `home`, `limit`, `cursor` | `millrace-os-workflow-outputs/v1` |
| `GET /api/v1/router/outputs/instances/{instance_id}/runs/{run_id}/{output_id}` | `home`, `record_sha256` (64 lowercase hex), `representation` (`report` or `json`) | `millrace-os-workflow-output/v1` |

The publication body is parsed strictly before validation. A duplicate key,
`NaN` or `Infinity`, invalid UTF-8 or trailing data refuses `invalid_request`
(422). Publication is refused with `router_closing` while the service shuts
down; listing and reading continue. All three routes use the existing eight
read slots (`router_read_busy`, 429), and every response is measured against
`RESPONSE_LIMIT` like the views.

The responses are:

- **Publication:** `{contract, home, authority: "workflow-produced-evidence",
  observed_at, reference: {instance_id, run_id, output_id, record_sha256},
  report_sha256, json_sha256, status}`. `status` is `published` or
  `already_published`.
- **Listing:** `{contract, home, instance_id, run_id, observed_at, items,
  next_cursor}`. Items are sorted by output ID. An item that fails
  verification has `availability: "unavailable"`, `reason:
  "output_integrity_error"`, and null for every field except `output_id`.
- **Read:** `{contract, home, authority: "workflow-produced-evidence",
  observed_at, reference, producer, label, published_at, invocation,
  representation: {name, format, media_type, sha256, data}}`.

`workflow-produced-evidence` labels display evidence from a local publisher. It
proves neither task success, ownership, writer closure nor producer provenance.

Refusals:

| Code | Status |
| --- | --- |
| `invalid_output_record`, `invalid_request` | 422 |
| `output_not_found`, `output_representation_unavailable` | 404 |
| `output_too_large` (canonical record over 1,047,552 bytes) | 413 |
| `request_too_large` (body over 1,048,576 bytes) | 413 |
| `output_conflict`, `output_identity_mismatch`, `output_integrity_error`, `outputs_changed` | 409 |
| `output_publication_uncertain` | 503 |
| `core_*` (a Core refusal during the identity lookup) | 409 |

After `output_publication_uncertain`, read and compare first, then retry with
the same saved record. A retry returns `already_published` when the earlier
attempt became durable. The service never retries on its own. The helper's
guide in Plus, `docs/workflow-views.md`, gives the record schema, the
canonical JSON rule and the storage rules.

## Headless parity reads

These reads report existing OS facts. They read no Core store except where
noted, and they repair, replay, accept or resume nothing. Every route uses
normal trust and a fixed query allowlist. Each response is measured against
`RESPONSE_LIMIT`, and each uses the existing eight read slots
(`router_read_busy`, 429) except the local-call snapshot.

| Route | Query | Response |
| --- | --- | --- |
| `GET /api/v1/client/capabilities` | none | `millrace-os-client-capabilities/v1` |
| `GET /api/v1/router/local-calls/{request_id}` | `home` | `millrace-os-router-local-call/v1` |
| `GET /api/v1/router/publications/{instance_id}` | `home`, `request_id` | `millrace-os-router-publication/v1` |
| `GET /api/v1/router/drafts/{draft_id}/manifest` | `home`, `limit`, `cursor` | `millrace-os-draft-manifest/v1` |
| `GET /api/v1/router/drafts/{draft_id}/file` | `home`, `path` | `millrace-os-draft-file/v1` |

### Client capabilities

`{contract, api_version: "v1", operations: [{operation_id, method, path,
request_contract, response_contract, available, reason}], observed_at}`
lists the 44 headless operations, sorted by ID.

- **Contracts** are registry `contract_id`s or null.
- **Availability** reflects lazy import checks of the optional router
  components only (control, bounded inspection, the output helper, the
  catalog); no store is read.
- **Unavailable operations** name the first missing component as `reason`,
  such as `router_unavailable`. OS-local operations (drafts, presentation,
  homes, local calls, publication status) are always available.
- **`actions.invoke`** is always unavailable (`action_unavailable`).
- **Exclusions:** trust bootstrap, exchange and rotation, recovery
  diagnostics, and frontend and demo operations are not listed.

This is the client operation list. The observation contracts one home
offers are `OPTIONS /api/v1/router/requests?home=`.

### Local call results

`{contract, home, request_id, request_digest, operation, instance_id, run_id,
phase, holder_joined, error, result, observed_at}` reports one OS-local call
(catalog init/copy, Apply, publication repair) that OS still retains.

- **Phases:** `phase` is one of the four owned phases. The status is 202
  while `admission_pending` or `call_active`, else 200.
- **Results and errors:** `result` is non-null only for `call_returned` and
  equals that call's 200 `data`. `error` is the stored `os.error.v1` of a
  failed call.
- **Retention:** OS keeps a finished call for 300 seconds, and at most the
  latest 128 joined calls. After that, after a restart, or for a Core request,
  the read refuses `local_call_unavailable` (404).
- **Unknown, not failed:** that 404 means unknown, not refusal or success.
  There is no Core fallback and no replay.

Apply uses the caller's request ID. Catalog and repair calls receive an ID
that appears only in their 202 observation.

### Publication status

`{contract, home, instance_id, request_id, in_progress, completed,
observed_at}` reports the exact Apply's publication markers, read without the
publication lock.

- **Strict checks:** each present marker is strictly checked with the
  validator that repair uses. A malformed marker refuses
  `publication_evidence_invalid` (409), even if it belongs to another request.
- **Matching:** a marker is reported only when its `request_id` matches, and
  `frozen_token` is omitted.
- **Both markers:** while both exist, every in-progress value must equal the
  completed one, else `publication_evidence_invalid`.
- **Not found:** with no matching marker the read refuses
  `publication_not_found` (404). A later Apply replaces `completed.json`, so an
  older request becomes unknown in this way.

### Draft manifest and file reads

The manifest returns `{contract, home, draft_id, sha256, items: [{path,
sha256, size}], next_cursor, observed_at}`.

- **Order:** items are sorted by path.
- **Digest:** `sha256` is the draft read's whole-draft digest.
- **Cursor:** `d` followed by 40 hex characters of that digest, `_` and an
  offset. A changed draft makes an older cursor refuse `draft_changed` (409).
  A malformed cursor or one past the end refuses `invalid_request`.

The file read returns `{contract, home, draft_id, path, sha256, size,
content_base64, observed_at}`.

- **Guards:** the same path and owner guards as draft writes apply.
- **Refusals:** an absent file refuses `draft_file_not_found` (404), and
  content whose base64 exceeds 1,048,576 characters refuses
  `draft_file_too_large` (409).
- **Nothing runs:** a read executes nothing.

The Apply route's OpenAPI now documents its actual 202 response; Apply itself
is unchanged.

## CLI

```text
millrace-os router homes [--limit N] [--cursor C]
millrace-os router views --home NAME [--instance I [--run R [--revision REV]]]
    [--section S] [--request-id X] [--invocation-id Y] [--limit N] [--cursor C]
millrace-os router request --home NAME --request-id X --bounded --instance I [--run R]
millrace-os router validate --home NAME --instance I [--run R] --summary
millrace-os router outputs publish --home NAME --file RECORD
millrace-os router outputs list --home NAME --instance I --run R [--limit N] [--cursor C]
millrace-os router outputs read --home NAME --instance I --run R --output O
    --record-sha256 S --representation report|json
millrace-os router local-call --home NAME --request-id X
millrace-os router draft manifest --home NAME --draft D [--limit N] [--cursor C]
millrace-os router draft read-file --home NAME --draft D --path REL
millrace-os router draft replace-files --home NAME --draft D --entry REL=FILE ...
millrace-os router draft publication-status --home NAME --instance I --request-id X
millrace-os router request ... [--observation v1|v2]
millrace-os client inspect --kind KIND [--home NAME] [--contract-id C --digest S]
```

`client inspect` reads one foundation surface and prints it unchanged:

- **Kinds:** `health`, `identity`, `version`, `contracts`, `schema` (with
  `--contract-id` and `--digest`), `diagnostics`, `providers`, `actions`,
  `availability` (the single event), and `capabilities`.
- **`observation`** (with `--home`): the observation contracts that home
  offers.

`--observation v2` asks for the call-v2 observation; without it the
observation stays v1. `replace-files` replaces the whole draft with the given
files.

`router outputs publish` parses the saved record file strictly. A duplicate
key, `NaN`/`Infinity` or invalid UTF-8 refuses locally before any request. The
command sends the record unchanged as compact UTF-8 JSON and never retries.

The CLI passes the options you give it. The service query allowlist refuses
any option a view does not accept, for example `--section` without `--run`.
The CLI itself refuses the following before sending a request:

- `--run` without `--instance`;
- `--revision` without `--run`;
- `--bounded` with `--file`;
- `--summary` with `--draft`.

Without `--bounded` or `--summary`, `router request` and `router validate`
keep their earlier full-read behavior.
