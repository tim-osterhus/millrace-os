# OS API foundations

The retained foundation implements local trust and OS-owned version-one contracts. Millrace OS 0.23.1 adds the public router mapping documented in `router-operation.md`. Core retains durable execution and receipt authority. Historical released OS/provider gates remain separate evidence.

`millrace-os serve` creates the owner-only bearer credential. CLI/script clients
read that explicitly configured credential file and send `Authorization: Bearer
<token>` over the configured loopback endpoint. File possession is not an HTTP
session. `millrace-os open` authenticates an HTTP nonce-mint request and opens the
browser with the nonce only in the fragment. `open --recovery` requests a separate
recovery nonce and opens `/recovery`. No nonce is printed by the CLI. The service
must be running at the configured host and port; the CLI does not follow redirects
or use HTTP proxies for credential-bearing requests.

The browser immediately clears its fragment, then exchanges the nonce using a
same-origin POST. The response sets an HttpOnly, SameSite=Strict, host-only cookie
with a 15-minute expiry and returns a CSRF token held only in JavaScript memory.
The cookie is intentionally not Secure because the service uses HTTP on explicit
loopback. A browser mutation requires that cookie, exact configured Origin, and
`X-CSRF-Token`. An explicit bearer header selects bearer authentication; a bad
bearer is never rescued by a cookie. Every supplied Origin must match, including
bearer requests. Minting and rotation require bearer authentication. CORS is
absent, Host is checked against bind configuration, and forwarded headers do not
select the trusted origin. Aliases such as localhost and 127.0.0.1 are distinct.

Nonces use 256 bits of randomness, expire after 60 seconds, and are single use.
At most 32 outstanding nonces and 16 sessions exist. Expiry is monotonic; capacity
refuses with 429. `millrace-os rotate-credential` uses an authenticated local POST,
atomically replaces the credential, and invalidates every existing session and
nonce. Restart also clears transient trust. Rotation requires a running service;
there is no offline fallback that leaves its in-memory bearer valid.

The package-owned recovery bundle consists of `assets/recovery.html` and the
separate `recovery/bootstrap.js` resource. The recovery-owned script is served at
`/recovery/bootstrap.js`; the normal landing page uses its own
`assets/bootstrap.js` at `/assets/bootstrap.js`. Recovery does not load that
normal asset, `/`, normal HTML, preferences, operational SQLite or lower providers.
Missing normal landing HTML returns 503. The recovery shell itself returns 200;
public readiness returns 503 if storage is unsafe. Authenticated recovery
`diagnostics` returns 200 with a bounded error code and no raw exception, path,
credential or state content. A recovery-scoped session cannot access normal API
reads even when the store is healthy. Corrupt, future and migration-failed stores
are preserved by the P01 storage boundary. No recovery reset, database deletion,
receipt rewriting or lower-state mutation endpoint exists in this packet.

## Route contract

Only `/health`, `/ready` and `/api/v1/health` expose the public minimal
`{"status":"ok"|"unavailable"}`. HTML and script routes expose static bootstrap
assets only. Every other data route requires bearer or cookie authentication.

- `GET /api/v1/version`, `/diagnostics`, `/identity` and `/contracts`: typed read
  envelopes with OS authority, observation time, redaction and foundation-only
  compatibility; no fabricated lower-layer cursor.
- `GET /api/v1/contracts/{contract_id}/1/{sha256}`: authenticated immutable schema
  identity/digest lookup. Invalid path syntax is 422; unknown valid identity is 404.
- `GET /api/v1/providers` and `/actions`: empty, explicitly unavailable foundations,
  with `limit` 1–100 (default 20). Valid cursors are refused with 409 because no
  paging continuation exists yet. Malformed cursors and unknown query keys are 422.
- `POST /api/v1/actions/{action_id}:invoke`: bounded `target_id` input and required
  `Idempotency-Key` / `X-Correlation-Id` identifiers. Invalid input is 422; every
  well-formed action is 409 `action_unavailable`. No success, receipt, job or
  provider invocation is fabricated. Pending and unknown remain distinct future
  outcomes; a caller observation deadline cannot imply completion.
- `GET /api/v1/events`: authenticated, one small `availability` event followed by
  closure. It explicitly declares non-durable scope and unavailable replay, emits
  no ID, and retains no history or provider buffers. Eight concurrent responses
  maximum; excess is 429. Malformed Last-Event-ID is 422; any syntactically valid
  Last-Event-ID is 409 `event_replay_unavailable`. This is not P05 SSE qualification.
- `POST /api/v1/auth/bootstrap`, `/auth/exchange`, `/auth/rotate`: bounded typed trust
  inputs and outputs. Rotation returns only status. Exchange needs exact Origin.
- `GET /api/v1/recovery/diagnostics`: authenticated, redacted, storage-independent.

Documented router GET routes additionally accept exact home/target query keys. Unknown or duplicate router query keys are refused. JSON mutations are capped at 4096 bytes, including chunked bodies, and require application/json. Only exact router draft file-set/per-file PUT uploads permit 1 MiB of encoded JSON. Auth/Origin checks happen
before input validation or handlers. Unknown input fields are forbidden. Public
validation errors never include rejected values. Error envelopes are stable and
bounded; this foundation does not claim a diagnostic record or receipt exists.

`contracts.py` authors distinct reads, errors, action inputs/outputs, receipts,
events and storage models. `api.export_artifacts(app)` generates deterministic
OpenAPI 3.1 and JSON Schema 2020-12 artifacts plus a version/digest registry. All
three JSON files are packaged wheel resources and checked byte-for-byte by tests.

## Router contracts

`RouterRead` reports exact Core data with Core authority and an observation time. It never inherits foundation-only compatibility or redaction claims. Validation includes a typed `validation_context` and positive observed `run_state_version` for run-aware validation. Nested plane/node settings remain intact.

`RouterCallObservation` separates owned-call phase from the immutable Core receipt. Queued, applied, and refused receipts retain their exact IDs, request digest, plane target, state version, and stop sets. An applied pause receipt does not establish final writer drain. OS worker errors remain sanitized OS errors, never fabricated refused Core receipts.

Router controls use tracked non-daemon workers. Core is imported lazily through the public Plus execution/v2 handshake. Missing or mismatched Core produces a typed router availability error without changing foundation trust/readiness. Catalog authoring imports independently and does not require Core.

The packaged JSON schemas and OpenAPI derive from the source models/routes through the existing exporter. Session mutations document Origin and CSRF requirements. The original action shell and foundation SSE remain separate unavailable/non-durable surfaces.
