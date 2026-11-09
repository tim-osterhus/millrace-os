# Complete browser frontends

Millrace OS serves one complete browser client at a time. The packaged stock
client is the default. As an alternative, an operator can select one prebuilt
local client that they trust as their own code. Either one is an ordinary
client of the same authenticated API that the CLI and MCP use. Selecting a
frontend grants no extra authority.

The stock client, the frontend launcher and recovery stay reachable whatever
is selected. A local client cannot hide them, and a broken one cannot stop the
service, the API or recovery from starting.

The service never copies, installs, builds or runs a local client. It does not
need Node, React or any stock build tool, and it gives a local client no
private stock objects. A local client is a static directory of prebuilt files
that the browser loads from this service.

## Bundle

A bundle is one directory. Its root holds the manifest
`millrace-os-frontend.json`:

```json
{
  "format": "millrace-os-frontend-bundle/v1",
  "frontend_id": "example.board",
  "frontend_version": "1.4.0",
  "entry": "index.html",
  "assets": [
    {"path": "assets/app.css", "sha256": "…", "media_type": "text/css; charset=utf-8"},
    {"path": "assets/app.js", "sha256": "…", "media_type": "text/javascript; charset=utf-8"},
    {"path": "fonts/body.woff2", "sha256": "…", "media_type": "font/woff2"},
    {"path": "index.html", "sha256": "…", "media_type": "text/html; charset=utf-8"}
  ],
  "api_version": "v1",
  "required_contracts": [
    {"contract_id": "FrontendState", "version": 1, "sha256": "…"}
  ],
  "required_capabilities": ["client.frontend.read", "router.views.run"],
  "presentation_format": "example.board-layout/v1",
  "default_presentation": {"columns": 3}
}
```

Every key is required, and no other key is allowed. Duplicate keys, `NaN` and
`Infinity` are refused.

| Field | Rule |
| --- | --- |
| `frontend_id` | `^[a-z0-9][a-z0-9._-]{0,63}$`. `stock` is reserved. |
| `frontend_version` | `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`. |
| `entry` | Exactly `index.html`, which must also be listed in `assets`. |
| `assets` | Every file the client loads: `path`, lowercase `sha256` and `media_type`. Paths are sorted and unique ignoring case. |
| `api_version` | Exactly `v1`. |
| `required_contracts` | Exact `{contract_id, version: 1, sha256}` entries from `GET /api/v1/contracts`. Name each contract you rely on, never the whole registry. |
| `required_capabilities` | Operation IDs from `GET /api/v1/client/capabilities`, each of which must currently be available. |
| `presentation_format`, `default_presentation` | Both null, or a format ID (`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`) and a JSON object. See [Presentation](#presentation). |

Asset paths are relative POSIX paths of at most 512 characters. Each component
matches `^[A-Za-z0-9_-][A-Za-z0-9._-]*$`, and no path starts with
`instances/`. The media type must be the one listed here for the extension:

| Extension | Media type |
| --- | --- |
| `.html` (the entry only) | `text/html; charset=utf-8` |
| `.js`, `.mjs` | `text/javascript; charset=utf-8` |
| `.css` | `text/css; charset=utf-8` |
| `.json` | `application/json` |
| `.txt` | `text/plain; charset=utf-8` |
| `.png` | `image/png` |
| `.jpg`, `.jpeg` | `image/jpeg` |
| `.webp` | `image/webp` |
| `.avif` | `image/avif` |
| `.ico` | `image/vnd.microsoft.icon` |
| `.woff2` | `font/woff2` |
| `.woff` | `font/woff` |

SVG, WebAssembly, source maps and every other type are refused.

### Bundle key

The bundle key, `bundle_sha256`, is the SHA-256 of the manifest's canonical
bytes (`millrace-canonical-json/v1`: sorted keys, compact separators, UTF-8,
no trailing newline). Reformatting the manifest file therefore keeps the key.
Changing any value, including an asset digest, gives a new key that must be
validated and selected again.

### Compatibility

Compatibility is positive. The API version, every required contract digest and
every required capability must match this service exactly. Anything unknown or
missing makes the bundle `frontend_incompatible`. A published contract
ID/version never changes; when a model changes, it gets a new version, so an
old required digest correctly makes the bundle incompatible.

### Files on disk

The directory path must be absolute and spelled one way: no `.` or `..`
components, no repeated or trailing slash and no control characters. Every
directory from `/` down to each listed file must be a real directory, not a
symbolic link. Each one must be owned by you or root and must not be writable
by other users, unless it is a sticky directory such as `/tmp`. Each listed
file must be a regular file owned by you or root, must not be writable by group
or other users, and must have its listed digest. Files that are not listed are
ignored and never served.

## Serving

| Route | Serves |
| --- | --- |
| `/ui/stock/` | The stock client, always. |
| `/ui/local/{bundle_sha256}/` | The selected local client's `index.html`, only while that bundle is the effective frontend. |
| `/ui/local/{bundle_sha256}/{path}` | Exactly the listed asset with its listed media type. |
| `/`, `/instances/…` and `{base}instances/…` | 303 to the effective base, plus the validated target. |
| `/ui/stock` | 303 to `/ui/stock/`. |
| `/client` | The frontend launcher. |
| `/recovery` | Recovery, independent of every frontend and of presentation. |

Targets use the query keys `home`, `instance` and `run`. An instance requires
a home, and a run requires an instance. With no target the client shows
inventory. Duplicate or unknown keys and invalid IDs answer 422. A redirect
`Location` never carries a fragment, so the browser keeps the sign-in nonce.

Each shell navigation and asset read rereads the selection, revalidates the
manifest and re-hashes every listed file. There is no cache.

- If anything fails, the stock client becomes effective, and `/` and the
  launcher go there.
- `/ui/local/{digest}/` for a digest that is not effective answers 503 with a
  small fixed page. The page names `frontend_unavailable` and links to the
  stock client, the launcher and recovery. Stock HTML is never served under a
  local base.
- An asset of a digest that is not effective answers 503 `frontend_unavailable`
  JSON.
- An unlisted path under the effective base answers 404 `route_not_found`
  JSON. There is no single-page fallback.

Both clients use the same headers. Every response is `no-store`,
`no-referrer`, `nosniff` and `DENY` framed. The two shell documents use this
policy:

```text
default-src 'none'; script-src 'self'; style-src 'self'; style-src-elem 'self';
style-src-attr 'none'; connect-src 'self'; img-src 'self'; font-src 'self';
worker-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'
```

A bundle cannot change this policy. Your client must work without inline
script, `eval`, inline `style` attributes in markup, remote assets, `data:`
or `blob:` URLs, workers and service workers. Fonts and images must be listed
assets.

The shell is always served at its exact base. Refer to assets
document-relatively (`./assets/app.js`), and import modules and stylesheets
relative to each other. The entry never needs its own digest, an injected base
or an inline loader.

## Sign-in and requests

A local client uses the same trust flow as the stock client:

1. Read the URL fragment once and remove it with `history.replaceState`,
   keeping the path and query, before any request.
2. If there was a nonce, send `POST /api/v1/auth/exchange` with
   `{"nonce": "…"}`. This sets the HttpOnly, SameSite=Strict session cookie
   and returns `{"csrf": "…"}`. Keep the CSRF value only in memory.
3. If there was no nonce, as after a reload, send
   `GET /api/v1/auth/session-context` with the header
   `X-Millrace-Session-Context: 1`. The browser adds
   `Sec-Fetch-Site: same-origin`. The answer is the same session's CSRF value
   and its remaining lifetime; it never extends the session. Bearer and
   recovery sessions are refused.
4. Send every request to this origin's `/api/v1/…` paths with
   `mode: "same-origin"`, `credentials: "same-origin"`, `redirect: "error"`
   and `cache: "no-store"`. Every `POST`/`PUT` also sends
   `Content-Type: application/json` and `X-CSRF-Token`.
5. After a 401 (expired session, rotated credential or restarted service),
   stop reading and writing, and show the `millrace-os open` command for a
   fresh session.

The service never puts a bearer credential, nonce, cookie or CSRF value in a
bundle, the selection, presentation data or any frontend read. There is no
cross-origin API, and CORS is not enabled.

## What a client must do

The service checks a bundle's files, not its behavior. Static validation
cannot prove that custom JavaScript works. A client you select must:

- read `GET /api/v1/client/capabilities` and treat unavailable operations as
  unavailable;
- make every change through the public API, saving each exact request
  envelope (its request ID, expected state version and body) before sending
  it;
- after a reply was lost, observe that same request or deliberately replay its
  exact saved envelope, never a new ID or a refreshed version;
- keep unknown outcomes visible as unknown, never as success or refusal;
- before the user switches frontends or reloads, offer an export of any
  unresolved requests and ask the user to resolve or deliberately discard
  them.

Requests are never copied into the selection or into presentation data.
Requests imported into another client start observe-only until the user
reviews and replays them. Sign-in never moves between frontends: each newly
loaded page signs itself in as above.

## Choosing a frontend

The choice is operator configuration, not a presentation preference. It
applies to every home and client of this service and is stored in
`config_dir/frontend.json` (`millrace-os-frontend-selection/v1`, owner-only).
The file is replaced atomically with a fresh version token, and nothing is
written by a read. It survives restarts and package upgrades. Recovery never
reads or clears it.

| Operation | API | CLI (`millrace-os`) | MCP (`millrace_v2_frontend`) |
| --- | --- | --- | --- |
| Inspect | `GET /api/v1/client/frontend` | `frontend inspect` | `inspect` |
| Validate | `POST /api/v1/client/frontend/validate` `{path}` | `frontend validate --path DIR` | `validate` `{path}` |
| Select | `PUT /api/v1/client/frontend` `{expected_version, desired}` | `frontend select --path DIR --bundle-sha256 KEY --expected-version V` | `select` `{expected_version, desired}` |
| Reset to stock | the same `PUT` with `desired: {"kind": "stock"}` | `frontend reset --expected-version V` | `reset` `{expected_version}` |

All four give the same results and errors, and none of them sends a Core
request.

- **Inspect** returns `millrace-os-frontend-state/v1`:
  - `status`: `absent` (nothing saved, so stock), `ready` or `invalid`;
  - `version`: the token to pass as `expected_version`, or null when nothing
    is saved;
  - `desired`: `{"kind": "stock"}` or `{"kind": "local", "path", "bundle_sha256"}`;
  - `effective`: `{kind, base, bundle_sha256}`;
  - `manifest`: the selected bundle's `frontend_id`, `frontend_version` and
    key, when its manifest still has that key;
  - `reason`: why the stock client is effective instead of the desired one.
- **Validate** reads and checks one absolute directory and returns
  `millrace-os-frontend-validation/v1`: `{path, bundle_sha256, manifest}`. It
  never selects, copies or installs anything. The path may be up to 3,968
  characters, so the request fits the 4,096-byte body policy.
- **Select** pins exactly the validated key. The service validates the same
  directory again before saving. If the bytes changed, the version is not the
  one you inspected, or another selection is being saved, it refuses
  `frontend_conflict` (409) and saves nothing. Pass `null` (`--expected-version
  null`) only when nothing is saved yet.
- **Reset** saves the stock choice with the same version check. It needs no
  working local client.

### Refusals and reasons

| Code | Status | Meaning |
| --- | --- | --- |
| `invalid_request` | 422 | The path is not an absolute, single-spelling directory path, or the body is malformed. |
| `unsafe_path` | 422 | A symbolic link, or a directory or file that another user owns or can change. |
| `frontend_incompatible` | 409 | Invalid manifest, unknown version or media type, an unmet contract or capability, or a default that would push the presentation read over its 1,048,576-byte limit. |
| `frontend_unavailable` | 503 | The directory, manifest or a listed file is missing or unreadable. |
| `frontend_conflict` | 409 | A listed file's bytes differ from its digest, the manifest no longer has the selected key, the version was not the expected one, another selection is being saved, or a presentation write is in progress while you select a bundle with a default. |
| `frontend_write_uncertain` | 503 | The selection file was replaced, but the service could not confirm the directory write. The new selection may already be in effect. |

After `frontend_write_uncertain`, inspect first. If the version changed, the
new choice is saved; if not, select again with the version you inspected. The
service never rolls back, journals or replays a selection, and an older
`expected_version` refuses as usual.

`reason` in the state uses the same codes. It also uses
`frontend_selection_invalid` when the saved file cannot be read. The service
never rewrites an unreadable selection; select or reset refuses
`frontend_conflict` until you repair or remove `config_dir/frontend.json`.

### Fallback and switching

A desired local client that becomes missing, changed, unsafe or incompatible
stays saved with its reason, and the stock client is effective. The stock
client then shows a notice with that reason and links to the launcher and
recovery. Every navigation and restart checks again, so it becomes effective
again as soon as its files match.

Changing the selection never reloads, replaces or retargets a page that is
already open, and never replays a request. If an open page's local base stops
being effective, its later asset reads refuse, and it can offer the stock
client or the launcher. Saving a presentation default does not change the
selected frontend, and the selected frontend does not change any presentation
data.

## The launcher and the stock client

`/client` is a fixed package-owned page that never loads code from a selected
bundle. It shows the selected frontend's ID, version, key and path, the
frontend in use and the reason for any fallback. It also links to the stock
client and recovery, and says so if the stock client itself is unavailable;
recovery works either way. Open it with `millrace-os open --client`. Open the stock
client directly with `millrace-os open --stock`, which takes the usual
`--home`, `--instance` and `--run` targets.

Plain `millrace-os open` reads the effective base first, then opens it with
the target and a one-use nonce in the fragment. `--recovery`, `--stock` and
`--client` cannot be combined, and the launcher and recovery take no target.
Invalid arguments are refused before the service is contacted.

### Leaving the stock client

The stock client's own links to the launcher and recovery, in its fallback
notice, replace the page. A request it holds exists only in that tab. So an
ordinary click on one of those links keeps the page open while the tab holds an
unsent reviewed request, or a request that is unresolved or still being sent or
observed. The notice then names what is held:

1. Export it first: **Export reviewed request JSON** beside **Discard**, or
   **Export request JSON** on the request. Both use the same request export
   format, with the exact bytes and request ID. An unsent review is exported
   with `sent: false`.
2. Then **Discard** the review, **Observe** the request until Core's receipt
   resolves it, or **Retire** it after confirming. Retiring only forgets the
   request in this tab; it stops or undoes nothing in Core.
3. Choose the link again. The page never leaves by itself when a receipt
   arrives or a request is retired, and leaving sends nothing.

Export, Discard and Retire work without browser trust or a usable service; only
Discard and Retire wait for a send or read already in flight to end. So
recovery is never out of reach. A click with a modifier key, or one that opens
a new tab or window, keeps this page and is not held. This is not a general
unload prompt: closing the tab, typing an address or other browser navigation
is not intercepted. Reload, Back and Forward within the stock client keep the
tab's requests in its history and restore them observe-only, as before.

## Presentation

A bundle may carry one immutable presentation default. `presentation_format`
names its own namespace, `{frontend_id, presentation_format}`, and
`default_presentation` is its payload. OS keeps that payload as plain finite
JSON and never reinterprets it as stock data.

While the bundle is the effective frontend, `GET /api/v1/client/presentation`
lists the default under `bundled_configs` with `id: "default"`, the payload's
digest and `bundle_sha256`. Generic reset, delete and default resolution in
that namespace then use it. When the bundle is not effective, the default is
not listed, and reset in that namespace refuses `presentation_incompatible`
without changing the record. Selection never creates, changes or migrates a
namespace, and other namespaces are untouched.

The presentation read must stay within 1,048,576 bytes with the store and that
default together. A selection that adds a default checks the size, and saves,
while it holds the presentation store's lock. A presentation write resolves the
effective default under the same lock. One of two overlapping writes therefore
refuses: the selection with `frontend_conflict`, or the store write with
`presentation_busy`. Neither can leave an oversized pair. A selection whose
default does not fit refuses `frontend_incompatible`.

See [presentation](presentation.md) for the store and its helpers.
