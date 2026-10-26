# Router call join observations

A Core receipt describes a request's durable disposition. OS separately owns
the worker and receipt-watcher threads used to make that public call. A final
receipt or a `call_returned` phase does not establish that both threads joined.

Existing request POST and GET clients receive the strict
`millrace-os-router-call/v1` payload with no additional fields. Clients that
need physical call closure select
`Accept: application/vnd.millrace.router-call.v2+json` on
`POST /api/v1/router/requests` and
`GET /api/v1/router/requests/{request_id}?home=NAME`.

The opt-in `millrace-os-router-call/v2` payload has all v1 fields and a required
`holder_joined` field:

- `true`: the exact owned worker and watcher returned and OS joined them.
- `false`: OS still knows this exact call and has not joined both threads.
- `null`: OS has only a Core receipt, including after process restart or
  observation-cache loss. Unknown calls without a receipt refuse with 404.

POST runs the same retirement path as GET before returning a v2 observation.
Physical join occurs through `_retire` or the existing `wait_group` path; both
join the exact worker and watcher. A blocked watcher keeps `holder_joined`
false after its worker returns. A join
for one request never supplies evidence for another request, digest or target.
A joined queued request can still have an active child: call closure and Core
run/writer closure are separate facts.

A process restart loses the in-memory call cache. In-process `Service.stop()`
and `Service.start()` can retain it, subject to the 300-second observation TTL
and 128-entry completed-call eviction. After cache loss, re-POSTing the same
request ID and digest creates a new owned call. Its `holder_joined: true`
proves only that new call joined, not that the lost earlier call joined.
Clients must observe the original call and retain its exact join evidence, or
use a separately authorized fresh recovery proof. Do not replay a start request
to establish closure of a lost call.

Before dispatch, clients can probe
`OPTIONS /api/v1/router/requests?home=NAME`. The authenticated response is
`millrace-os-router-observation-capabilities/v1` with the selected `home`,
`observation_contracts` containing call-v1 and call-v2, and
`holder_joined_supported: true`. Discovery validates the configured named home
without importing or reading Core or creating a state directory. Exactly one
`home` query is required; duplicate and unknown parameters refuse.

The route retains normal bearer authentication, host/origin checks and recovery
restrictions. Browser sessions also require the exact origin and CSRF header
for OPTIONS, as they do for other methods besides GET and HEAD.

The exact v2 media type, with surrounding whitespace ignored, selects v2 and
is returned as its Content-Type. Ordinary legacy Accept headers continue to
select v1, including weighted `application/json` and ordinary comma lists.
Explicit unsupported or malformed `application/vnd.millrace.router-call.*`
requests refuse with 406 before submitting a call or reading its observation.
Repeated Accept fields are combined as a comma list before selection. Vendor
comma lists or parameters also refuse: this endpoint does not implement
weighted vendor negotiation. The exact call-v1 media type selects v1.
Capability probing is optional for existing clients and grants no additional
Core execution authority.
