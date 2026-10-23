# Optional MCP bridge

The foreground bridge connects trusted operator clients to an already running
OS API. OS owns its controller calls and Core owns runs, receipts and writers.
Client disconnect or bridge shutdown closes connections without stopping OS or
cancelling its work.

Install the explicit `mcp` extra with Python 3.12 or 3.13:

```bash
python -m pip install 'millrace-os[mcp]==0.23.1'
millrace-os mcp serve --config /absolute/path/to/mcp.json
```

The selected profile uses official MCP Python SDK 1.30.0, protocol `2025-11-25`,
JSON responses and stateless Streamable HTTP. Other Python/SDK profiles refuse
before serving. Base OS retains Python >=3.12 without MCP, Core or Plus imports.
Install the separate `router` extra where the backing daemon needs public router
controls. Initial interoperability covers independent pinned official SDK client
processes. Named GUI clients need their own evidence. Deprecated standalone
HTTP+SSE, sampling, elicitation, subscriptions and experimental tasks are excluded.

## Explicit configuration

```json
{
  "format": "millrace-os-mcp-config/v1",
  "transport": "stdio",
  "os_origin": "http://127.0.0.1:8731",
  "os_credential_file": "/absolute/path/to/existing/credentials.json",
  "homes": ["main"]
}
```

The origin is a literal HTTP loopback origin with a port and no userinfo, path,
query or fragment. Homes select existing OS aliases, never filesystem paths.
Every backend request rereads the existing owner-only regular credential file,
without creating, rotating or caching it. A rotation-racing 401 is surfaced;
the next explicit request reads the new credential. No remote bearer, cookie or
identity header is sent to OS. Redirects and ambient HTTP proxies are disabled.

The CLI transport defaults to stdio and must match the file. Unknown fields
refuse. stdio rejects HTTP deployment fields, including explicit defaults.
The daemon's environment configuration does not fill missing bridge fields.
Stdout contains protocol messages only. Failures omit credentials and input
values. Packaged `schemas/mcp-config-v1.json` defines strict field shapes;
runtime checks also enforce URL/transport combinations. Endpoints are bounded
at 2048 characters, credential paths at 4096, and lists at 32 nonempty entries
of at most 512 characters. Duplicates and wildcards refuse.

## Trusted shared HTTP

Use `--transport streamable-http`, change the file's transport and add:

```json
{
  "resource_url": "https://mcp.example/mcp",
  "issuer_url": "https://issuer.example/",
  "jwks_url": "https://issuer.example/jwks",
  "trusted_subjects": ["operator-subject"],
  "allowed_hosts": ["mcp.example"],
  "allowed_origins": ["https://client.example"],
  "listen_port": 8792,
  "proxy_peer": "127.0.0.1"
}
```

These are fixed HTTPS deployment inputs. The bridge binds literal loopback.
Port defaults to 8792 and accepts integers 1024–65535; proxy peer defaults to
127.0.0.1 and must be a loopback IP. The intended path is trusted client → HTTPS
reverse proxy → loopback bridge → loopback OS. Expose only MCP and public
protected-resource metadata. Keep OS HTTP and its bearer private. Remote proxy,
issuer and credential deployment requires a separate operational grant.

Every admitted subject receives existing local operator authority, including
server paths, executable/PATH settings and environment allowlists in public
create/configure requests. There is no tenant, role or workload isolation model.
All routes, including metadata, require exact public Host and allowed Origin
values. Present empty, null, duplicate or disallowed access headers refuse.
Authenticated native clients may omit Origin. CORS is exact, never wildcard.
Only the configured proxy peer may supply forwarding headers; admission checks
the real peer before forwarding is applied. Preserve the public Host at the
proxy. Forwarded identity headers grant no authority.

Public RFC9728 bootstrap metadata contains only configured resource URL, issuer
URL and `millrace:operator` scope. Operational tools/list, discovery, reads and
mutators require an external resource JWT. The bridge issues no tokens.
Verification requires RS256, exact issuer/audience, allowed subject and operator
scope. Tokens are at most 16 KiB. Required exp/iat and optional nbf must be finite
numeric times with zero leeway. JWKS uses only the configured HTTPS URL, a
five-second total timeout, 256-KiB response cap, no redirects or ambient proxies,
and a hard 300-second cache lifetime. Fresh known keys verify without waiting
for a refresh. Missing or expired keys may trigger a refresh, with at least
30 seconds between attempts across all key IDs, including failed attempts.
This fixed internal availability policy can delay key rotation or recovery
after an issuer outage by up to 30 seconds; it is not an RFC requirement.
Expired keys refuse during the cooldown. Only eligible RSA/RS256 signing keys
require nonempty, unique key IDs; irrelevant keys are ignored. Token key URLs
are never fetched. Configuration changes require explicit restart.

Selected HTTP requests use JSON POST; GET event streams are excluded. Initialize
carries the selected protocol version; subsequent operations require its header.
With stdio the SDK can answer unsupported initialize using fallback negotiation.
The bridge checks original client parameters before tool list/call and OS
dispatch. Fallback negotiation does not establish supported-client admission.

## Fixed tools and result identity

| Tool | Strict arguments | Existing API/v1 behavior |
| --- | --- | --- |
| `millrace_v1_discover` | none | Package/profile/schema identity and aliases |
| `millrace_v1_instances` | home | GET router instances |
| `millrace_v1_instance` | home, instance_id | GET exact instance |
| `millrace_v1_run` | home, instance_id, run_id | GET exact run |
| `millrace_v1_request` | home, request | POST unchanged request/v2 |
| `millrace_v1_receipt` | home, request_id, instance_id, optional run_id | Targeted observation |
| `millrace_v1_validate` | home, instance_id, optional run_id | POST validation without draft mutation |

Wrappers reject unknown fields/coercion before SDK conversion. IDs are current
lowercase 32-character UUID hex strings. The seven strict schemas and SHA-256
are packaged in `schemas/mcp-tools-v1.json`; discovery returns the same digest.
Startup authenticates version/contracts reads and refuses unsupported API or
router read/call versions before admitting tools.

Allowed request operations are create_instance, start_run, pause_run, cancel_run,
resume_run, update_instance_config, submit_revision, accept_revision, pause_plane,
resume_plane, cancel_plane, redirect_plane and retry_invocation. Core performs
complete public request/v2 validation. The bridge preserves the dictionary,
caller UUID, expected state version and targets. It adds no retry scheduler,
reconciliation, writer assertion, hold release, draft Apply, catalog write or
credential change.

Results carry `millrace-os-mcp-tools/v1` and an unchanged OS envelope under `os`,
as structured content plus JSON-text fallback. OS observations and Core receipts
remain separate; queued/applied/refused retain their meanings. Sanitized
`access_error` includes code, status, retryable flag and known correlation. It
means unknown observation rather than a fabricated Core refusal. Backend 413
and retryable 429 retain their access-error meanings.

## Saved identity, limits and recovery

Save the full request before submission. JSON-RPC IDs are separate from Core
UUIDs. Two clients replaying the same canonical home/request UUID and unchanged
digest observe the same receipt; changed bodies conflict. Fresh intent after
definite refusal needs a new UUID/current version. An uncertain response never
justifies a replacement create/start.

After bridge disconnect/restart, inspect the saved exact target or replay the
same request. After OS restart, recover unknown creation by replaying its exact
saved POST, even without a learned instance ID. Receipt reads require an
instance ID; they never scan inventory to guess one. Creation receipts are not
instance controls. Stateless HTTP provides no durable transport session.

Ingress is capped at 64 KiB before SDK parsing, total responses at 1 MiB, tool
concurrency at eight and backend requests at five seconds. Excess calls get an
immediate retryable busy result. OS's ordinary 4096-byte mutation limit and
0.15-second observation interval remain unchanged. A larger/longer operation
can return 413 or unknown timeout while OS retains ownership. Callers may back
off and repeat a read or the identical saved mutation. Graph limits stay intact.

Direct LAD starts require existing server-local project `request.md` bytes.
Missing input yields workflow-owned `INTAKE_MISSING`; no upload tool is added.
Edits retain manual Apply, explicit acceptance and state-dependent continuation;
see [router operation](router-operation.md).

The public universal lock retains base pins and an exhaustive `<3.14` / `>=3.14`
solver partition covering the full unchanged base range. This prevents uv 0.13.5
from leaking MCP's inactive Python 3.14 Pydantic requirement. MCP/PyJWT retain
their `<3.14` markers. Local candidate Core/Plus wheel overrides are separate
integration evidence, not a final unpublished public registry lock.

The selected shared-server proof used two pinned SDK clients and one deterministic
workflow with no model calls. It covers that exact deployment and request profile.
That predecessor proof used the earlier auth implementation. The release auth
repair has deterministic issuer/cache and SDK regression coverage; it has not
repeated the external issuer/proxy deployment, so exact-artifact native proof
does not carry forward from the predecessor.
Native provider, named GUI-client and general cross-platform claims need separate
evidence. Live Paperclip task/account/server proof remains deferred.
