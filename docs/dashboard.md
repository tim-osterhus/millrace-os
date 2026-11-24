# Stock browser client

Millrace OS ships the stock dashboard as a prebuilt browser client. It shares
the authenticated OS API with the CLI and MCP bridge. Use it to inspect
instances, runs, captured graphs, and reports, and review and submit supported
public control requests. Core remains the execution authority.

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
workflow controls stay disabled; a transport failure is not an ordinary service.
Theme, density and panel changes still apply to the page.

## Workbench layout

The stock client follows one operator journey: choose a home and instance,
open a run, read its state, inspect what needs attention, then review a
deliberate control. A run shows one view at a time:

- **Overview** (always first): the recorded status, one plain sentence built
  only from recorded facts (what is finished, uncertain, held, paused or working
  and where to look next), any recorded run-level pause, gate, cancellation or
  pending candidate, and the planes that need attention with **Inspect node**
  and **Review resume**. **Run reports** is the lead button only when the
  sentence points at Reports; otherwise it is a link beside the observation
  time, and the Reports view is always one choice away.
- **Graph**: the read-only Directed/Grid graph with its tables, the Inspector
  and Revisions.
- **Activity**: Current work and blockers, the full Planes table and History.
  Current work groups its recorded facts under **Run** and under each plane
  that records one; a plane's ID is stated once, by its group, and a long ID
  keeps its full value one **Show full value** disclosure away.
- **Reports**: published outputs with their provenance. Opening the view reads
  the output list; nothing is read before. **Refresh outputs** re-reads the
  current page of that list on request (see [Reports](#reports)).
- **Control**: the exact request form, review and Send.

A view's panels mount and read only after that view is first opened for the
selected run, and stay mounted afterwards so a review, selection, zoom or
History section survives switching views. Away from Overview, the view bar
keeps the recorded run status, and says when the observation is stale. One
run announcement for assistive technology (status, any recorded run-level
pause reason, gate or cancellation request, plane blockers, or a failed read
with stale facts) sits outside the views and is exposed on every view; it
carries no clock or read time, so it changes only with those facts. A requested
cancellation is announced as requested, never as stopped work.
Choosing a view in **Run views** moves keyboard focus to that view's own
container, which the chosen button names and controls (`aria-controls`); the
view bar does not scroll away. Inspecting a node or invocation from any view
opens the Graph view's Inspector and focuses it; **Review resume** opens and
focuses Control; **Run reports** opens and focuses Reports.
The same node name in another plane or revision is never highlighted as the
selected object.

Overview leads with observed state, freshness and recorded plane blockers. The
lead sentence resolves run-level facts first: an unknown or missing run status
(no next step is inferred), a terminal status (finished only when Core is
quiescent with every recorded and effective writer stopped and nothing in
flight; otherwise the status with "writer closure is not confirmed"), a
requested cancellation that is not yet recorded complete, then a run-level
pause or gate. Only an otherwise ordinary run reads its paused planes as
waiting on a resume decision. Paused planes appear in the sentence as a count
that points at their rows under Plane activity; every exact plane ID stays in
its row. A paused plane may record no node to inspect, so the sentence asks for
that only where a node is shown. The sentence is wording only: it enables or
disables no operation, and the service still decides whether a request applies.
**Review resume** is drawn with review emphasis only while a fresh observation's
sentence recommends reviewing a plane resume. For an unknown, terminal,
cancelling or stale observation the same button is an ordinary secondary one:
equally reachable, with the same busy and trust conditions, and no claim about
whether the request will apply. When the latest read
fails, a separate statement under the status says so in ordinary text, and the
facts and sentence below it are the last successful observation, not the
current state. The
resume link fills the plane and operation only: it does not establish
applicability, create a request or send one. An existing unsent review or busy
request is not replaced by that link. Run terminal status, Core quiescence and
recorded/effective writer facts remain separate. When Core is quiescent, every
recorded and effective writer is stopped and nothing is in flight, the writer
counts move under **Exact target and run details**; in every other case the
writer and in-flight facts stay open on Overview. Exact target IDs, edit mode,
state version and supporting run facts are under the same disclosure.
Requests sent from this tab, and every notice, stay above the views on every
view until resolved or retired.

This layout supersedes the earlier single stacked page and its Jump to section
list. The preset payload, panel visibility, split ratios and explicit
save/version semantics are unchanged, and no saved presentation is rewritten.
What changed is only how a saved arrangement is shown: it no longer orders one
long page. It still names the pair that shares the resizable row (Graph and
Inspector in the Graph view for Detail split and Graph split; Planes and
History in the Activity view for Status stack). A hidden optional panel stays
hidden and unread; its view offers **Show** for it, in memory only. Status,
notices, unresolved requests, Reports and Control never become hideable
presentation data.


Inventory display labels come only from the displayed project and workflow
path basenames; they are not new stored names. After an instance has been
opened, the instance breadcrumb shows that project folder basename while a run
of the same home and instance is open; the exact instance ID stays in the
link's name and title. A run opened directly, or another home or instance,
shows the instance ID: no extra read fills the name in. **Show full value** disclosures
retain selectable exact identifiers and paths. Instance runs precede the
settings and exact-path disclosure. Neither compact labels nor layout changes
alter the validated target query or history.

Observation ages and monitoring countdowns are ordinary accessible text.
Dedicated live messages describe meaningful observed-state, freshness failure
and monitoring transitions; the cosmetic clock does not announce every tick.

## What it shows

- **Homes and instances.** The client shows configured homes, then a home's
  instances. Empty, unavailable and integrity-refused instances are shown
  separately. Paging is explicit Next/Previous over pages of at most 50 rows.
- **Instance.** The instance's identity, its common runtime defaults and
  operator policy (an absent key shows "not set"), with runs newest first and
  settings in a separate disclosure.
- **Run.**
  - *Status* (the Overview view) cannot be hidden. Its exact-target disclosure retains full IDs, run
    status, edit mode, state version, run gate, pause reason, cancellation and
    Core quiescence, meaning every invocation's effective writer is stopped.
    It shows recorded and effective writer counts separately, in-flight
    invocations, each plane's recorded pause or terminal-dependency blocker,
    revisions and the observation time, even when *Planes* is hidden. A failed
    refresh keeps the last observation, labelled stale under the status. `may_live`, `unknown`,
    `running` and `stopped` are never merged. Quiescence never claims an OS
    caller or holder closure.
  - *Planes* and *Current work* list recorded facts only.
  - *Graph* is read-only. It draws one plane of the captured revision, with the
    always-present equivalent node, route and seam tables. Selecting a node or a
    table row opens *Inspector* for that exact identity. Layout is directed
    (Dagre) or a deterministic grid, with grid fallback if the directed layout
    fails. The initial canvas favors readable labels; **Fit all** shows the
    complete topology and **Readable size (100%)** returns to reading scale,
    centered on the recorded cursor when it is in the displayed graph,
    otherwise the Inspector's node or the start node.
    Long canvas labels also have a full-value title and a whole identifier in
    the equivalent tables. Positions are never saved.
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
stops fast refresh; holder closure is not established. **Refresh** reads once
on demand.

## Control requests

The instance screen offers **Start**; the run screen offers pause, cancel,
resume and recover for the run; pause, cancel and resume for a plane; retry,
redirect and reconcile; and submit and accept for revisions. Each form shows the
exact target, operation, payload, expected state version and a new request ID
before **Send**. A run starts with **Choose a request**, rather than assuming
that Pause is the next action. Each of the thirteen operations has a scope and
effect description. Review displays the full home, instance, run and plane
beside the retained raw bytes. The primary send button names the reviewed
operation; export and discard remain separate actions.

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

Reports show a document title, producer/published byline and content before
**Exact output provenance**. That disclosure retains invocation, revision,
producer claims and both full digests. The execution-authority warning and
exact-envelope downloads remain separate from the report's claims.

The output list is read when Reports is first opened and when its page
changes. Nothing re-reads it automatically, and the page-header **Refresh**
re-reads the run summary, not this list. **Refresh outputs** re-reads the
current page for the selected run: it finds outputs published since the last
read and retries a list read that failed. A successful read clears the earlier
failure. If a re-read fails after a successful one, the last successful list
stays, labelled with its read time. The selected report, its content and its
exact provenance are kept through a list refresh; a row is marked selected only
while it names the same output ID and record digest.

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
choices. Overview, notices and the view bar are never hidden. Every hidden
optional panel stays reachable from **Show** in its view. Selecting a node reveals a hidden
Inspector in memory. A presentation change never changes the target,
selection, trust or any request.

The header's **Layout** menu calls a presentation a *layout*. Opened, it shows
three everyday rows (**Layout**, with built-in and saved layouts; **Theme**;
**Density**) and one line that says whether the page's settings are saved.
Edits apply to this page only until an explicit action writes them:

- **Save changes** appears for a saved layout with unsaved changes.
- **Save as new layout…** names and saves the page's current settings,
  including unsaved changes. It is the only save for a built-in layout, which
  cannot be overwritten.
- **Discard changes** returns the page to the saved settings and writes nothing.

When saving is unavailable, the same line names the unmet precondition: browser
trust, the service context, saved layouts that could not be read, or an
unreadable file. Two disclosures hold the rest:

- **Panels and split** groups the optional panels by the view that shows them
  (Graph view; Activity view). **Side by side** is the arrangement: it names
  the pair that shares a resizable row when both are shown on a wide screen,
  with its current division; otherwise the two stack.
- **Manage layouts** acts on the chosen layout: **Rename…**, **Duplicate…**,
  **Make default**, **Reset…**, **Delete…** and **Export file**; then
  **Import file…** and **Reload saved layouts** for the store. Duplicate and
  Export use the saved settings, without unsaved changes. After Duplicate or
  Import the page shows the new layout as stored, discarding unsaved changes
  unless the page was edited while the write was pending. Reset and Delete ask
  for confirmation. Rename, Reset and Delete are not offered for a built-in
  layout, and Make default is not offered for the default. Reload shows the
  default layout again and discards unsaved changes; it reports success only
  after a usable read, and otherwise says what failed. Saved-layout writes
  and another reload wait until a pending write or reload settles.

Choosing another layout also discards unsaved changes, and the menu says so
while there are any. Until saved layouts are read, the Layout choice is
disabled with the reason beside it; theme, density and panels still change the
page.

Each result is reported, and focused, in the section whose action produced it.
Escape cancels an open naming or confirmation step, otherwise closes the menu
and returns focus to its button.

Saved presentations use the shared service store described in
`presentation.md`. Changes are written only by an explicit action, and the
service's expected-version check refuses a write if another client changed the
store first. The page then offers **Reload saved layouts** beside the refusal
and keeps the unsaved layout until that reload. A
reply marks saved only the exact payload that was sent; edits made while it was
pending stay unsaved, and a reply never changes a presentation chosen since.
Nothing is retried automatically. If the store is unreadable or newer, the
client keeps its bytes and shows Precision in memory with the reason; Export is
unavailable, and replacing the file is a separate confirmed action under
**Manage layouts**.

The Run overview labels stale observations beside the recorded status. Plane
activity lists recorded blockers and pending routing first, followed by the other
planes; it does not classify every plane as waiting. Reports is a run-level action.
The compact graph legend distinguishes the recorded cursor from a selected captured node,
and the Inspector foregrounds the exact object it is showing. Beside the graph the
Inspector is as tall as its content and starts at the top of its side of the split;
resizing, Widen and the saved ratios are unchanged. Confirmed quiescence,
stopped writer counts and no in-flight work use a compact summary under **Exact
target and run details**; uncertain or active writer facts remain fully labeled
on Overview above the plane list. Graph
help explains the marks and overview scale in a secondary disclosure. Exact targets and the meaning of quiescence remain
available in run details.

Run-level gate, pause, requested cancellation and pending-change facts remain
visible in Overview's Status, including when secondary panels are hidden; edit mode is
listed under **Exact target and run details**. A pending
candidate is a recorded pending change, not automatically a blocker for the current
run. Recorded running work, active invocation links and unknown-cursor work stay
expanded alongside recorded plane attention; Other plane activity
is available in a counted disclosure without a row cap. Writer uncertainty remains
above both groups. In-flight presence remains visible with exact invocation links
in a secondary disclosure.

A new captured topology opens at a reading scale around its exact recorded cursor,
then an exact selected node or the start node when no cursor matches. Routine reads
do not reset a manual pan or zoom. Fit all still shows the complete topology, including
action endpoints; the initial current-work view may crop its neighbors. Fit all
and Readable size (100%) are named toolbar controls outside the drawing. Readable size is the
deliberate way back to current work, for example after the window was resized
while another view was open: it sets 100% and centers the exact recorded cursor
of the displayed plane and revision, otherwise the Inspector's node there, otherwise
the start node. It selects nothing and changes no run state. No read, resize or
view switch recenters the graph. Switching views changes no saved presentation.

At narrow widths, populated Inventory and Instance run tables scroll locally so
all columns, links and exact-value disclosures stay available without widening
the page. Refresh stays beside the Run title; target breadcrumbs wrap at segment
boundaries and the view bar scrolls in one row; the compact Layout label retains the layout name in its accessible name and menu. Fit all and Readable size stay together outside
the drawing. Inspect selection appears when the Inspector is not adjacent.

State chips share a common UI face, border and shape. Paused uses warning colors,
failed uses error colors, and unknown, cancelled and succeeded remain neutral.
Stale observation is a separate qualification of the read, not a Core run status.

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

Invocation Inspector shows the successful snapshot's read time separately from
Created. A reused History row is labelled History snapshot. Manual Refresh reads
the exact selected invocation again; summary monitoring does not refresh it. A
failed invocation read retains the earlier facts and their original read time,
with the failed read's time and error alongside them.

Inventory and Instance run tables keep the primary identity visible in a sticky
first column while the remaining columns scroll horizontally. A visible hint
explains the local scroll; exact-value disclosures and all columns remain available.

Invocation Created, Deadline and Run deadline numeric epoch seconds are shown
as readable UTC timestamps with their raw seconds in a disclosure. Null,
nonnumeric and unrepresentable dates remain their recorded values. The separate
snapshot read time and failed-read time retain their own meaning.

The current run status is a flat recorded fact beside the Reports action. Both
contextual and form Review entry points use the same outlined treatment; Send
remains the solid primary action. Graph read failures size to their message,
guidance and retry action; the resizable split keeps a bounded handle when no
canvas exists. Planes, Graph and History tables include horizontal-scroll cues.
Plane identity remains available while its columns scroll, with full identifiers
in disclosures. An invocation selected in Inspector is explicitly not marked as
a graph node.
