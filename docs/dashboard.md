# Stock browser client

Millrace OS ships one prebuilt browser client, the stock client. It is an
optional view of the same authenticated API that the CLI and MCP use. Opening a
browser is never required; Core remains the execution authority. The client
inspects inventory, instances, runs, captured graphs and generic workflow
outputs. This build adds no operational controls of its own.

## Opening it

```sh
millrace-os open                                  # configured state homes
millrace-os open --home NAME                      # one home's instances
millrace-os open --home NAME --instance ID        # one instance
millrace-os open --home NAME --instance ID --run ID
millrace-os open --recovery                       # trust/diagnostics only
```

`open` mints a one-use 60-second nonce with the bearer credential. It then
opens the effective frontend's exact base (`/ui/stock/` unless a local client is
selected; see [frontends](frontend.md)) with the validated target query and
puts the nonce only in the URL fragment. `open --stock` always opens this
client. Targets follow one grammar on the CLI, the server and the
client: `home` is an identifier, while `instance` and `run` are 32 lowercase hex
characters. An instance requires a home and a run requires an instance.

If a selected local frontend is unavailable (its files are missing, changed or
incompatible), `/` opens this client instead, and it shows a notice with the
reason and links to the frontend launcher (`/client`) and recovery. The saved
selection is kept; see [frontends](frontend.md).

## Routes

| Route | Serves |
| --- | --- |
| `/ui/stock/` with optional `home`, `instance`, `run` | The shell document only; it contains no operational data. Duplicate or unknown keys and invalid targets answer 422. |
| `/ui/stock/{path}` | Exactly the files listed in the packaged manifest, with their listed media type. An unlisted path answers 404 JSON. A listed file whose bytes changed or went missing answers 503 `frontend_unavailable`. |
| `/ui/stock` | 303 to `/ui/stock/` plus the validated target. |
| `/`, `/instances/{instance}`, `/instances/{instance}/runs/{run}` and the same `instances` forms under a shell base | 303 to the effective frontend's base plus the validated target (`/ui/stock/` unless a local client is selected and available). The `Location` has no fragment, so the browser keeps the nonce fragment. |
| `/recovery` | The independent recovery shell; it never loads the stock client. |

Every response is `no-store`, `no-referrer`, `nosniff` and `DENY` framed. The
shell document alone uses this policy:

```text
default-src 'none'; script-src 'self'; style-src 'self'; style-src-elem 'self';
style-src-attr 'none'; connect-src 'self'; img-src 'self'; font-src 'self';
worker-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'
```

It permits no inline script, eval, inline style attribute, remote asset,
worker, data or blob source. API and recovery responses keep their existing
policy.

## Browser trust

One private module owns trust; other code never receives the CSRF value.

1. It reads the fragment once and removes it with `history.replaceState`,
   keeping the path, query and state, before any request.
2. With a nonce, it exchanges it at `POST /api/v1/auth/exchange`. This sets the
   HttpOnly, SameSite=Strict session cookie and returns the CSRF value.
3. Without one, as after a reload, it reads `GET /api/v1/auth/session-context`
   with `X-Millrace-Session-Context: 1`. The browser adds
   `Sec-Fetch-Site: same-origin`. The read returns the same session's CSRF value
   and remaining lifetime, and never renews the session.
4. Every request uses `mode: same-origin`, `credentials: same-origin`,
   `redirect: error` and `cache: no-store`. Each one reads at most 1,049,600
   bytes and ends after 5 seconds. An ended request means "not observed", never
   "stopped".
5. A 401 (session expired, credential rotated, service restarted) stops all
   reads and writes. The page then shows the exact `millrace-os open …` command
   for a new session. A 403 trust refusal disables writes.

The nonce, bearer credential and CSRF value never enter the DOM, history state,
browser storage, links or exported files. Valid-cookie reload depends on a
browser that sends Fetch Metadata. A browser or webview that does not send it
fails closed and needs a fresh `open`. Recovery sessions cannot read operational
data in the stock client.

After trust, the client reads `GET /api/v1/demo/context`. Only an authenticated
404 `demo_context_unavailable` establishes an ordinary service. A pending,
failed or unreadable context is shown, and no control may treat it as ordinary.
Until an ordinary or a bound demo context is observed, presentation saves and
every control stay disabled; a transport failure is not an ordinary service.

## What it shows

- **Homes and instances.** The client shows configured homes, then a home's
  instances. Empty, unavailable and integrity-refused instances are shown
  separately. Paging is explicit Next/Previous over pages of at most 50 rows.
- **Instance.** The instance's identity, its common runtime defaults and
  operator policy (an absent key shows "not set"), and its runs, newest first.
- **Run.**
  - *Status* cannot be hidden. It shows the exact target with full IDs, run
    status, edit mode, state version, run gate, pause reason, cancellation and
    Core quiescence, meaning every invocation's effective writer is stopped.
    It shows recorded and effective writer counts separately, in-flight
    invocations, each plane's recorded pause or terminal-dependency blocker,
    revisions and the observation time, even when *Planes* is hidden. A failed
    refresh keeps the last observation, labelled stale. `may_live`, `unknown`,
    `running` and `stopped` are never merged. Quiescence never claims an OS
    caller or holder closure.
  - *Planes* and *Current work* list recorded facts only.
  - *Graph* is read-only. It draws one plane of the captured revision, with the
    always-present equivalent node, route and seam tables. Selecting a node or a
    table row opens *Inspector* for that exact identity. Layout is directed
    (Dagre) or a deterministic grid, with grid fallback if the directed layout
    fails. Positions are never saved.
  - *History* reads one section page (invocations, transitions and moves, or
    control receipts) only after the section is chosen.
  - *Revisions* shows the startup, active and pending revisions. An invalid
    candidate shows its diagnostic instead of a graph. **Validate current
    editable files** is an explicit bounded read of the files as they are now.
    It publishes nothing, captures no candidate and never replaces the
    captured Graph or the active routing revision.
- **Reports.** These load only when opened.

Absent or wrongly typed facts read "unknown". A recorded null reads "none".
Codes Core does not freeze, such as statuses and reasons, appear exactly as
recorded. Reads run one at a time: homes or the selected page, then the selected
summary. A hidden optional panel performs no reads.

One coordinator observes the selected target. It refreshes a visible run
summary every 2 seconds and unresolved requests at 1 second, then every 2
seconds. Each observation window is five minutes, then **Continue monitoring**
offers another. A hidden tab reads nothing. Failures back off to 5, 15 and 30
seconds, then stop with **Retry**. A bounded-view budget refusal stops automatic
reads at once; there is no fallback to a full read. A terminal, quiescent run
stops fast refresh, which still proves no holder closure. **Refresh** reads once
on demand.

## Control requests

The instance screen offers **Start**; the run screen offers pause, cancel,
resume and recover for the run; pause, cancel and resume for a plane; retry,
redirect and reconcile; and submit and accept for revisions. Each form shows the
exact target, operation, payload, expected state version and a new request ID
before **Send**.

- **Envelope.** Each form builds one `millrace-router-request/v2` envelope with
  all eight fields and explicit nulls, wrapped as `{home, request}` for
  `POST /api/v1/router/requests`.
- **Request ID.** The ID is 16 random bytes as 32 lowercase hex characters.
- **Versions.** Stop requests carry a null version. Every other request carries
  the reviewed observed version: run state, or instance settings for Start.
- **Start mode.** Start is locked unless **live** is chosen.
- **Deliberate inputs.** Retry, redirect and reconcile need a reason.
  Reconciling needs an explicit, never preselected attestation that every writer
  has stopped.
- **Redirect.** Redirect offers only nodes of the active captured revision. It
  reads that graph once if Graph is hidden.
- **Accept.** Accept names the pending candidate and may resume routing.
- **Authority.** Core and Plus decide applicability. A disabled control is a
  hint, never enforcement.

The reviewed bytes are serialized once. They are the only bytes sent, and the
only bytes replayed. An answer is an observation, not task success:

- **Core receipt.** The receipt status (queued, applied, refused) and code are
  shown separately from the OS call phase.
- **Holder join.** This client uses call/v1, which cannot attest a holder join.
- **Lost answers.** If the answer is lost or unreadable, the request is
  "Delivery unknown". It is observed only through the strict view,
  `GET /api/v1/router/views/requests/{id}`, by exact ID and known target. A 404
  stays unknown.
- **Start.** Start learns its run only from a matching receipt.
- **Binding.** The first digest observed for a request is bound to it. A later
  mismatch is an integrity error that disables replay.
- **No automatic follow-ups.** A stale version is refused under its original ID.
  Nothing refreshes the version, changes the ID, resends, accepts or resumes
  automatically.
- **One at a time.** One unresolved non-stop request is allowed per target. Stop
  requests stay available.

**Requests in this tab** cannot be hidden by a preset. It lists every request
with **Observe**, **Replay exact request**, **Export request JSON** and
**Retire**. Intents are kept in memory and in this tab's history entries only,
never in storage or preferences, and never with trust material:

- **Navigation.** Each history entry holds the whole set. Back and Forward merge
  the visited entry, so newer requests are never lost. Reload restores requests
  observe-only.
- **Limits.** An entry holds at most 20 intents and 64 KiB. Over that, new sends
  wait until requests are exported and retired; nothing is evicted silently.
- **Integrity.** Different bytes under one ID disable replay.
- **History unavailable.** If the history entry cannot be written, the page
  warns that a reload would lose replay bytes before it sends anything.

## Reports

Reports list the run's generic workflow outputs and render only the
`millrace-report-display/v1` representation, using fixed elements:

- the title, paragraphs, facts and tables are escaped plain text;
- each source becomes a link only for an absolute `https:` URL. The URL must
  have no credentials and must not name `localhost`, a `.localhost` host, or a
  loopback, private, shared, link-local or unspecified IP address. Hosts are
  checked after standard URL parsing, without DNS. Links open in a new tab with
  `noopener noreferrer` and no referrer. Every other source is inert text.

Nothing from a report executes as HTML, Markdown or script. A report is
workflow-produced evidence: the producer is a claim, and an available report
proves no task success, writer closure, ownership or join. Downloads save the
exact API response envelope. Its file bytes are not the canonical bytes behind
the representation SHA-256 shown on the page.

## Presentation

The three bundled presets share every region and inspect action:

- **Precision** (default): detail split of Graph and Inspector.
- **Command**: compact density; Planes and History; Graph, Inspector and
  Revisions start hidden.
- **Studio**: graph-focused split.

Theme, density, arrangement, optional panels and the split ratio are separate
choices. Status, notices and the Actions navigation are never hidden. Every
optional panel stays reachable from Actions. Selecting a node reveals a hidden
Inspector in memory. A presentation change never changes the target,
selection, trust or any request.

Saved presentations use the shared service store described in
`presentation.md`. Changes are written only by an explicit action, and the
service's expected-version check refuses a write if another client changed the
store first. The page then offers a reload and keeps the unsaved layout. A
reply marks saved only the exact payload that was sent; edits made while it was
pending stay unsaved, and a reply never changes a presentation chosen since.
Nothing is retried automatically. If the store is unreadable or newer, the
client keeps its bytes and shows Precision in memory with the reason; replacing
it is a separate confirmed action.

## Building the client (maintainers)

Installing, building or running the Python package needs no Node. The wheel and
sdist carry the prebuilt closure under `millrace_os/assets/dashboard/`:

- `index.html`;
- the hashed JS/CSS files;
- `THIRD_PARTY_NOTICES.txt`;
- `dependency-inventory.json`;
- the stock manifest `millrace-os-frontend.json` (`millrace-os-frontend-bundle/v1`),
  which is not itself served.

These files are generated; never edit them by hand. To regenerate them on the
pinned toolchain (Node 24.20.0, npm 11.19.0):

```sh
npm ci --prefix frontend
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
python -I scripts/export_dashboard.py          # validate and write the closure
python -I scripts/export_dashboard.py --check  # compare without writing
```

Export the HTTP schemas first. The manifest pins the registry digests of the
envelopes the client consumes, and a stale build answers
`frontend_unavailable` instead of mis-parsing.

The exporter writes only the fixed package directory, and only after
validation. It refuses:

- nonlocal, inline or unlisted HTML references;
- source maps;
- symlinks;
- unexpected staging files;
- lock entries without registry provenance;
- any emitted module it cannot attribute to `frontend/src`, a locked production
  package, or a Vite/Rolldown helper;
- a module closure that omits, repeats or adds a JS chunk relative to the
  emitted closure, or records a chunk without emitted code.

Notices and the dependency inventory come from the actual lock and the emitted
module closure. The sdist also carries `frontend/` sources, the lock and the
exporter, without `node_modules` or build staging.
