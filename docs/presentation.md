# Shared presentation preferences

Saved presentation configurations live in one owner-only file per OS
configuration directory, `config_dir/presentation.json`. Every client of that
service shares them: the stock browser client, a custom frontend, the CLI and
MCP. Each client sees the same values after normal trust. Separate
configuration directories have separate stores.

These are presentation preferences only. They are not credentials, targets,
control intent, receipts or code, and they grant no execution authority.
Unsaved layout, selection, navigation, authentication and in-flight requests
stay in the browser.

The store is available headlessly through `GET`/`PUT
/api/v1/client/presentation` and the `millrace-os presentation` CLI. Both use
the store and helpers in `millrace_os.presentation`, described below. No
browser dashboard that uses these preferences ships with this release.

## Store

The file holds `millrace-os-presentation-store/v2`:

```text
{format, version, namespaces: [Namespace]}
Namespace {frontend_id, presentation_format, default_ref, configs: [Config]}
Config    {id, name, payload}
default_ref  null | {kind: "bundled", id} | {kind: "user", id}
```

- **`version`:** a 32-lowercase-hex token, freshly generated for every
  successful write. A write store omits it; the service assigns it.
- **Namespaces:** sorted by `(frontend_id, presentation_format)`, each pair
  unique. `frontend_id` matches `^[a-z0-9][a-z0-9._-]{0,63}$`.
- **Configs:** sorted by `id` (32 lowercase hex), each unique within its
  namespace. A `name` is a plain one-line label of 1–256 characters.
- **Names are not identity.** Two configurations may share a name, and no
  helper renames or dedupes them. IDs are the only identity.
- **No count limit.** The whole state must fit the 1,048,576-byte response
  policy.

The stock namespace is `{frontend_id: "stock", presentation_format:
"millrace-stock-presentation/v1"}`. `stock` is reserved for that format.

- **Payloads:** validated strictly as `{preset, theme, density, arrangement,
  panels, split_ratios}`:
  - `preset` is `precision`, `command` or `studio`;
  - `theme` is `system`, `light` or `dark`;
  - `density` is `comfortable` or `compact`;
  - `arrangement` is `detail-split`, `status-stack` or `graph-split`;
  - `panels` maps each of `planes`, `current-work`, `graph`, `inspector`,
    `history` and `revisions` to a boolean;
  - `split_ratios` maps each arrangement to a number strictly between 0 and 1.
- **Never stored:** status, notices, unresolved requests and actions. They
  cannot be hidden.
- **Default:** never null. A bundled default is `precision`, `command` or
  `studio`.

Every other namespace is custom.

- **Payloads:** any finite JSON object. The owning frontend validates their
  meaning; OS does not.
- **Default:** null, `{kind: "bundled", id: "default"}`, or a user
  configuration in the same namespace.

### Bundled stock presets

The bundled presets are immutable. Saving one creates a user copy with
complete values, and saved copies are never rewritten by a changed bundled
definition.

| Preset | Density | Arrangement | Hidden panels | Split ratios |
| --- | --- | --- | --- | --- |
| Precision | comfortable | detail-split | none | all 0.5 |
| Command | compact | status-stack | graph, inspector, revisions | all 0.5 |
| Studio | comfortable | graph-split | planes, current-work, revisions | graph-split 0.65, others 0.5 |

All three use the `system` theme. With no saved store, clients use Precision
in memory; nothing is written automatically.

## Reading

`read_state(config_dir)` returns the GET state
(`millrace-os-presentation-state/v1`):

`{contract, status, version, file_sha256, store, bundled_configs, observed_at}`.

| File | `status` | `version` | `file_sha256` | `store` |
| --- | --- | --- | --- | --- |
| absent | `absent` | null | null | null |
| a valid v2 store | `ready` | token | digest | store |
| a strict JSON object with format `millrace-os-presentation-store/vN`, N > 2 | `incompatible` | null | digest | null |
| any other safe regular file (malformed, invalid or oversized) | `invalid` | null | digest | null |

- **Unsafe files:** a symlink, a non-regular file, a file owned by another
  user, or one with group/other mode bits refuses `unsafe_path`. It reports no
  digest.
- **Oversized files:** hashed in chunks, never loaded whole.
- **No lock, no write:** reads open the file once, so a concurrent replace
  shows the old or the new whole file. A read never writes or repairs anything.
- **Startup is unaffected:** a broken file does not stop the service or
  recovery from starting. Recovery never reads it.

`bundled_configs` lists the three stock presets as `{frontend_id,
presentation_format, id, name, payload, payload_sha256, bundle_sha256: null}`.
A selected custom frontend later adds its own bundled `default`.

Malformed or newer files are preserved byte for byte. Clients show the reason
and use a built-in presentation in memory.

## Writing

`write_state(config_dir, write)` takes a `millrace-os-presentation-write/v1`
body and returns the new GET state:

```text
{format, mode: "compare", expected_version: token | null, store}
{format, mode: "replace_unreadable", observed_file_sha256, store}
```

`expected_version` is required in `compare` mode: `null` is a value, not an
omission.

The order of a write:

1. Validate the body and store (`invalid_request`).
2. Assign a fresh token.
3. Refuse `presentation_too_large` if the resulting GET state would exceed
   1,048,576 bytes.
4. Take `config_dir/.presentation.lock` without waiting (else
   `presentation_busy`), then reread the current file and check it:
   - **`compare`:**
     - an absent file requires `null`;
     - a ready store requires its exact token, else `presentation_conflict`;
     - an invalid or incompatible file refuses `presentation_integrity_error`
       or `presentation_incompatible`.
   - **`replace_unreadable`:** the current file must be invalid or
     incompatible with exactly the observed digest, else
     `presentation_conflict`. This is the only way to replace an unreadable
     file. It is not Reset.
5. Write a `.presentation-*` temporary file, fsync it, recheck the target and
   `os.replace` it into place.
6. Fsync `config_dir`.

Every replacement gets a fresh token, so an older token can never match again.

Failure handling:

- **Before or at `os.replace`:** a failure, such as a temporary-file write or
  fsync, a changed target or a failed replace, leaves the previous bytes
  unchanged and removes the temporary file.
- **After `os.replace`:** a failed `config_dir` fsync returns
  `presentation_write_uncertain` (503). The new store and its token may
  already be visible, and nothing is rolled back. Read the state and compare
  content and token before retrying; an identical visible store needs no
  rewrite.

| Code | Status |
| --- | --- |
| `invalid_request` | 422 |
| `presentation_config_not_found` | 404 |
| `presentation_conflict`, `presentation_integrity_error`, `presentation_incompatible` | 409 |
| `presentation_too_large` | 413 |
| `presentation_busy` | 429 (retryable) |
| `presentation_write_uncertain` | 503 |

## Helpers

`HELPERS` maps each of the fifteen operations to a pure function. Each takes a
GET state first and returns one of two things:

- **Mutations** return a `compare` write body that carries the state's own
  token as `expected_version`.
- **Reads** return `{status, version, file_sha256, result}`.

| Operation | Arguments after `state` | Result |
| --- | --- | --- |
| `get` | — | the state unchanged |
| `put` | `write` | the validated write body unchanged |
| `replace_unreadable` | `observed_file_sha256`, `store` | a `replace_unreadable` body |
| `list` | `namespace` (optional) | namespaces with `default_ref` and `[{id, name}]` |
| `read` | `namespace`, `ref` | one user or bundled configuration |
| `read_default` | `namespace` | the effective default |
| `export` | `namespace`, `ref` | a `millrace-os-presentation-config/v2` document |
| `create` | `namespace`, `name`, `payload`, `expected_version`? | a new configuration with a new ID |
| `update` | `namespace`, `config_id`, `payload`, `expected_version`? | the payload replaced; ID and name kept |
| `rename` | `namespace`, `config_id`, `name`, `expected_version`? | the name replaced |
| `duplicate` | `namespace`, `source`, `name`, `expected_version`? | the source payload copied to a new ID |
| `delete` | `namespace`, `config_id`, `expected_version`? | removed, with the default normalized |
| `set_default` | `namespace`, `default`, `expected_version`? | `default_ref` set; creates the namespace if absent |
| `reset` | `namespace`, `config_id`, `expected_version`? | the bundled base copied into the same ID and name |
| `import` | `config`, `name`?, `expected_version`? | a new ID in the document's namespace |

Rules shared by the helpers:

- **Namespaces:** every namespace is an explicit `{frontend_id,
  presentation_format}` pair. Nothing is inferred from a browser or the
  selected frontend. Two namespaces may use the same configuration IDs; each
  helper reads and changes only the namespace it names.
- **References:**
  - `ref` and `source` are `{kind: "user", id}` or `{kind: "bundled", id}`.
    A bundled ID must be valid for the namespace.
  - `default` may also be `null`, but only in a custom namespace.
  - A missing namespace or configuration ID refuses
    `presentation_config_not_found` before any write.
- **`expected_version`:** if given, it must equal the state's token, else
  `presentation_conflict` before any write. `null` means the store must be
  absent; leaving it out skips the check. A conflict is returned, never
  retried.
- **New namespaces:** a stock namespace starts with default `{bundled,
  precision}`, a custom one with `null`.
- **Delete:** removing the default configuration falls back to `{bundled,
  precision}` for stock. A custom namespace falls back to its bundled
  `default` when one is listed in `bundled_configs`, else `null`.
- **Reset:** copies the bundled preset named by a stock payload's `preset`,
  or the custom namespace's bundled `default`. Without one it refuses
  `presentation_incompatible`.
- **Duplicate and import:** always create a new ID and never overwrite.
  Duplicating a bundled source creates the namespace when absent.
- **Defaults:** `read_default` of an absent stock namespace is bundled
  Precision; of an absent custom namespace it is `null`.
- **Unreadable files:** reads and mutations refuse with
  `presentation_integrity_error` or `presentation_incompatible`.
- **Portable documents:** `millrace-os-presentation-config/v2` is `{format,
  frontend_id, presentation_format, name, payload}`. There is no importer for
  earlier formats.

A convenience performs GET, then the helper, then PUT with the GET token. Two
clients that both compute from the same GET cannot both succeed: the second
write gets `presentation_conflict`.

## HTTP API

Both routes need normal trust: a bearer credential or a browser session. A
session `PUT` also needs the exact configured `Origin` and `X-CSRF-Token`.
Neither route accepts query parameters.

- **`GET /api/v1/client/presentation`** returns the state above
  (`PresentationState`). It never writes.
- **`PUT /api/v1/client/presentation`** takes a write body
  (`PresentationWrite`) as `application/json` and returns the new state (200).
  - The body may be up to 1,048,576 bytes.
  - The raw body is parsed strictly first: duplicate keys, `NaN` and
    `Infinity` refuse `invalid_request` (422).
  - The store rules in [Writing](#writing) then apply.

Responses are bounded at 1,049,600 bytes in compact UTF-8. Writes keep the
state within 1,048,576 bytes, so only a hand-placed file near that size can
make a `GET` refuse `response_too_large` (409).

Refusals are typed `os.error.v1` bodies with the codes in
[Writing](#writing).

- **Retry:** only `presentation_busy` (429) is marked retryable. Retry it
  unchanged.
- **Uncertain write:** after `presentation_write_uncertain` (503), `GET` and
  compare the content and token first.
- **Other codes:** `request_too_large` (413), `json_required` (415) and
  `unsafe_path` (422).
- **Storage recovery:** while the service is in storage recovery, both routes
  refuse `storage_unavailable` (503). Recovery never reads the file.

The registered schemas are `PresentationState`, `PresentationWrite` and
`PresentationConfig`, the portable document.

## CLI

`millrace-os presentation <subcommand>` mirrors the fifteen helpers. Each
subcommand takes the usual `--host`, `--port`, `--config-dir`, `--state-dir`
and `--credential-path` options and calls the running service with its bearer
credential. Options are never abbreviated.

- **`NS`** is the required pair `--frontend F --format P`.
- **`REF`** is exactly one of `--config ID` (a user configuration) or
  `--bundled ID`.
- **`--expected-version`** is accepted by every convenience mutation
  (`create`, `update`, `rename`, `duplicate`, `delete`, `set-default`,
  `reset` and `import`):
  - `--expected-version TOKEN` requires that GET token;
  - `--expected-version null` requires an absent store;
  - leaving the option out sets no precondition.

  `put` takes a complete write body with its own `expected_version`, and
  `replace-unreadable` takes an observed digest and a store. Neither accepts
  the option.

| Subcommand | Arguments |
| --- | --- |
| `get` | none |
| `put` | `--file WRITE` |
| `replace-unreadable` | `--observed-sha256 S --store-file F` |
| `list` | optional `NS` |
| `read`, `export` | `NS REF` |
| `default` | `NS` |
| `create` | `NS --name N --payload-file F` |
| `update` | `NS --config ID --payload-file F` |
| `rename` | `NS --config ID --name N` |
| `duplicate` | `NS REF --name N` |
| `delete`, `reset` | `NS --config ID` |
| `set-default` | `NS` and exactly one of `--config ID`, `--bundled ID`, `--none` |
| `import` | `--file DOC`, optional `--name N` |

How the subcommands behave:

- **`get`** prints the service's state.
- **`put` and `replace-unreadable`** send one `PUT` and print the service's
  response.
- **Reads** (`list`, `read`, `default`, `export`) perform one `GET` and print
  `{status, version, file_sha256, result}`.
- **Every other mutation** performs one `GET`, applies the helper, then sends
  one `PUT` in `compare` mode with the GET token.

Files are parsed strictly (UTF-8, unique keys, finite numbers), and bodies are
sent as compact UTF-8.

Exit status and output:

- **Success:** 0.
- **Refusal:** 1.
  - A helper refusal prints `{"error": code}` and sends no `PUT`.
  - A service refusal prints the service's error body.
- **Unreadable file argument:** 2.
- **Conflict:** returned, never retried.
- **Lost `PUT` connection:** prints `{"error": "presentation_write_unknown"}`.
  Run `get` and compare before trying again.

## Persistence across upgrades

The store is service data in the OS configuration directory, outside the
installed package. Upgrading, downgrading or reinstalling `millrace-os` leaves
`config_dir/presentation.json` untouched.

- **No migration:** nothing reads, converts or rewrites the file at startup.
- **Newer formats:** a store written by a newer release reads as
  `incompatible` and is preserved byte for byte. Only an explicit
  `replace-unreadable` with its digest replaces it.
- **Bundled presets:** these ship with the package, so a newer release may
  change them. Saved copies keep their own complete values.
