import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyReply,
  BODY_LIMIT,
  MAX_ENTRY_BYTES,
  MAX_INTENTS,
  OPERATIONS,
  STOPS,
  buildRequest,
  checkEnvelope,
  exportText,
  fits,
  intentFromBuilt,
  kept,
  merge,
  newEntry,
  newRequestId,
  nextDelay,
  outstanding,
  readHistory,
  resolved,
  restoredEntry,
  snapshotOf,
  stateWith,
  unreadableText,
  utf8Bytes,
  verify,
} from "./requests.ts";
import type { Entry, Intent, Operation, RequestSpec } from "./requests.ts";
import type { CallObservation, Observed } from "./api.ts";

const INSTANCE = "a".repeat(32);
const RUN = "b".repeat(32);
const INVOCATION = "c".repeat(32);
const OTHER_RUN = "9".repeat(32);
const DIGEST = "d".repeat(64);
const OTHER_DIGEST = "e".repeat(64);
const KEYS = ["format", "operation", "instance_id", "run_id", "plane_id", "request_id", "expected_state_version", "payload"];
const PLANE_OPERATIONS = new Set(["pause_plane", "cancel_plane", "resume_plane", "retry_invocation", "redirect_plane", "reconcile_invocation"]);

let counter = 0;
/** A fresh valid request ID per call: 32 lowercase hex characters. */
function id(): string {
  counter += 1;
  return counter.toString(16).padStart(32, "0");
}

function spec(operation: Operation, overrides: Partial<RequestSpec> = {}): RequestSpec {
  return {
    home: "default",
    operation,
    instanceId: INSTANCE,
    runId: RUN,
    planeId: "main",
    version: 7,
    editMode: "locked",
    invocationId: INVOCATION,
    nodeId: "work",
    candidateId: "f".repeat(64),
    reason: "why",
    stopped: true,
    ...overrides,
  };
}

function intent(operation: Operation, overrides: Partial<RequestSpec> = {}): Intent {
  const built = buildRequest(spec(operation, overrides), id());
  assert.ok(built.ok, built.ok ? "" : built.reason);
  return intentFromBuilt("default", built);
}

function observation(source: Intent, overrides: Partial<CallObservation> = {}): CallObservation {
  return {
    home: source.home,
    requestId: source.requestId,
    requestDigest: DIGEST,
    instanceId: INSTANCE,
    runId: RUN,
    source: "os_owned_call",
    phase: "call_returned",
    coreReceipt: null,
    observedAt: "t",
    ...overrides,
  };
}

function receipt(source: Intent, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const request = JSON.parse(source.body).request;
  return {
    request_id: source.requestId,
    instance_id: request.instance_id,
    run_id: request.run_id,
    plane_id: request.plane_id,
    operation: request.operation,
    request_digest: DIGEST,
    status: "applied",
    ...overrides,
  };
}

test("request IDs are 16 random bytes as 32 lowercase hex characters", () => {
  const fixed = newRequestId((bytes) => {
    bytes.forEach((_, index) => (bytes[index] = index));
    return bytes;
  });
  assert.equal(fixed, "000102030405060708090a0b0c0d0e0f");
  const high = newRequestId((bytes) => bytes.fill(255));
  assert.match(high, /^[0-9a-f]{32}$/);
  assert.throws(() => newRequestId(() => new Uint8Array(8)));
});

test("every operation builds an exact eight-field envelope with its target, version and payload", () => {
  for (const operation of OPERATIONS) {
    const built = buildRequest(spec(operation), INVOCATION);
    assert.ok(built.ok, `${operation}: ${built.ok ? "" : built.reason}`);
    const request = built.envelope;
    assert.deepEqual(Object.keys(request), KEYS, operation);
    assert.equal(request.format, "millrace-router-request/v2");
    assert.equal(request.operation, operation);
    assert.equal(request.instance_id, INSTANCE);
    assert.equal(request.run_id, operation === "start_run" ? null : RUN, operation);
    assert.equal(request.plane_id, PLANE_OPERATIONS.has(operation) ? "main" : null, operation);
    assert.equal(request.expected_state_version, STOPS.has(operation) ? null : 7, operation);
    const payloads: Partial<Record<Operation, Record<string, unknown>>> = {
      start_run: { edit_mode: "locked" },
      accept_revision: { candidate_id: "f".repeat(64) },
      retry_invocation: { invocation_id: INVOCATION, reason: "why" },
      redirect_plane: { node_id: "work", reason: "why" },
      reconcile_invocation: { invocation_id: INVOCATION, reason: "why", stopped: true },
    };
    assert.deepEqual(request.payload, payloads[operation] ?? {}, operation);
    assert.equal(built.body, JSON.stringify({ home: "default", request }));
    assert.deepEqual(JSON.parse(built.body).request, request);
  }
});

test("Start is locked unless live is chosen", () => {
  const omitted = buildRequest(spec("start_run", { editMode: undefined }), id());
  assert.ok(omitted.ok && omitted.envelope.payload.edit_mode === "locked");
  const live = buildRequest(spec("start_run", { editMode: "live" }), id());
  assert.ok(live.ok && live.envelope.payload.edit_mode === "live");
});

test("reviewed requests that Core would refuse are refused before Send", () => {
  const refusals: [Operation, Partial<RequestSpec>][] = [
    ["reconcile_invocation", { stopped: false }],
    ["reconcile_invocation", { stopped: undefined }],
    ["retry_invocation", { reason: "" }],
    ["retry_invocation", { reason: "nul\0inside" }],
    ["accept_revision", { candidateId: "f".repeat(32) }],
    ["redirect_plane", { nodeId: "1bad" }],
    ["retry_invocation", { invocationId: "not-an-id" }],
    ["resume_run", { version: null }],
    ["resume_run", { version: 0 }],
    ["resume_run", { version: 1.5 }],
    ["pause_plane", { planeId: null }],
    ["pause_run", { home: "bad home" }],
    ["retry_invocation", { reason: "x".repeat(BODY_LIMIT) }],
  ];
  for (const [operation, overrides] of refusals) {
    const built = buildRequest(spec(operation, overrides), id());
    assert.equal(built.ok, false, `${operation} ${JSON.stringify(overrides).slice(0, 60)}`);
    assert.equal(typeof (built.ok ? null : built.reason), "string");
  }
  assert.equal(buildRequest(spec("pause_run"), "A".repeat(32)).ok, false);
});

test("the request body never exceeds the service's body limit", () => {
  let reason = "";
  let last = buildRequest(spec("retry_invocation", { reason: "x" }), id());
  for (let size = 1; size < BODY_LIMIT; size += 64) {
    reason = "x".repeat(size);
    const built = buildRequest(spec("retry_invocation", { reason }), id());
    if (!built.ok) break;
    last = built;
  }
  assert.ok(last.ok && utf8Bytes(last.body) <= BODY_LIMIT);
});

test("checkEnvelope accepts only the exact request/v2 shape", () => {
  const built = buildRequest(spec("resume_run"), id());
  assert.ok(built.ok);
  assert.equal(typeof checkEnvelope(built.envelope), "object");
  assert.equal(typeof checkEnvelope({ ...built.envelope, extra: true }), "string");
  const missing: Record<string, unknown> = { ...built.envelope };
  delete missing.payload;
  assert.equal(typeof checkEnvelope(missing), "string");
  assert.equal(typeof checkEnvelope({ ...built.envelope, operation: "update_instance_config" }), "string");
  assert.equal(typeof checkEnvelope({ ...built.envelope, format: "millrace-router-request/v1" }), "string");
});

test("history snapshots round-trip exact intents beside other state", () => {
  const first = intent("pause_run");
  const second: Intent = { ...intent("resume_run"), binding: { digest: DIGEST, runId: RUN } };
  const state = stateWith({ other: 1 }, [kept(first), kept(second)]);
  assert.equal(state.other, 1);
  const restored = readHistory(state);
  assert.equal(restored.invalid, false);
  assert.deepEqual(restored.kept, [kept(first), kept(second)]);
  assert.equal(restored.raw, null);
  assert.deepEqual(readHistory(null), { kept: [], invalid: false, raw: null });
  assert.deepEqual(readHistory({}), { kept: [], invalid: false, raw: null });
});

test("unreadable history entries are reported, and only valid intents restore", () => {
  const good = intent("pause_run");
  const wrongFormat = { millrace_os_intents: { format: "other/v1", intents: [] } };
  assert.equal(readHistory(wrongFormat).invalid, true);
  const snapshot = snapshotOf([kept(good)]) as { intents: Record<string, unknown>[] };
  const renamed = { ...snapshot.intents[0], request_id: OTHER_RUN };
  const mixed = { millrace_os_intents: { ...snapshot, intents: [snapshot.intents[0], renamed] } };
  const restored = readHistory(mixed);
  assert.equal(restored.invalid, true);
  assert.deepEqual(restored.kept, [kept(good)]);
  const tooMany = { millrace_os_intents: { ...snapshot, intents: Array(MAX_INTENTS + 1).fill(snapshot.intents[0]) } };
  const refused = readHistory(tooMany);
  assert.deepEqual([refused.kept, refused.invalid], [[], true]);
  assert.equal(refused.raw, JSON.stringify(tooMany.millrace_os_intents));
});

test("one history entry holds at most 20 intents and 64 KiB", () => {
  const small = Array.from({ length: MAX_INTENTS }, () => kept(intent("pause_run")));
  assert.equal(fits(small), true);
  assert.equal(fits([...small, kept(intent("pause_run"))]), false);
  const reason = "x".repeat(BODY_LIMIT - 400);
  const large = Array.from({ length: 19 }, () => kept(intent("retry_invocation", { reason })));
  assert.ok(utf8Bytes(JSON.stringify(snapshotOf(large))) > MAX_ENTRY_BYTES);
  assert.equal(fits(large), false);
});

test("merging history entries keeps every request and flags conflicts", () => {
  const known = intent("resume_run");
  const entries: Entry[] = [newEntry(known, "answered")];
  const added = intent("pause_run");
  const joined = merge(entries, [kept(added)]);
  assert.equal(joined.length, 2);
  assert.equal(joined[1].delivery, "restored");
  assert.equal(entries.length, 1); // the input is not mutated

  const bound: Intent = { ...known, binding: { digest: DIGEST, runId: RUN } };
  assert.deepEqual(merge(entries, [kept(bound)])[0].intent.binding, bound.binding);

  const altered: Intent = { ...known, body: known.body.replace('"expected_state_version":7', '"expected_state_version":8') };
  const conflicted = merge(entries, [kept(altered)]);
  assert.equal(conflicted.length, 1);
  assert.equal(conflicted[0].intent.body, known.body);
  assert.deepEqual(conflicted[0].conflicts, [altered.body]);

  const mine: Entry[] = [newEntry(bound, "answered")];
  const theirs: Intent = { ...known, binding: { digest: OTHER_DIGEST, runId: RUN } };
  assert.equal(typeof merge(mine, [kept(theirs)])[0].integrity, "string");
  assert.ok(merge(joined, []).length >= joined.length);
});

test("the first digest binds and later observations must agree", () => {
  const request = intent("resume_run");
  const first = verify(request, observation(request));
  assert.deepEqual(first, { binding: { digest: DIGEST, runId: RUN } });
  const bound: Intent = { ...request, binding: { digest: DIGEST, runId: RUN } };
  assert.ok("problem" in verify(bound, observation(bound, { requestDigest: OTHER_DIGEST })));
  assert.ok("problem" in verify(bound, observation(bound, { requestId: OTHER_RUN })));
  assert.ok("problem" in verify(bound, observation(bound, { home: "elsewhere" })));
  assert.ok("problem" in verify(bound, observation(bound, { instanceId: OTHER_RUN })));
  assert.ok("problem" in verify(bound, observation(bound, { runId: OTHER_RUN })));
});

test("Start learns its run only from a matching observation", () => {
  const start = intent("start_run");
  const queued = verify(start, observation(start, { runId: null }));
  assert.deepEqual(queued, { binding: { digest: DIGEST, runId: null } });
  const bound: Intent = { ...start, binding: { digest: DIGEST, runId: null } };
  const applied = verify(bound, observation(bound, { runId: RUN }));
  assert.deepEqual(applied, { binding: { digest: DIGEST, runId: RUN } });
  const known: Intent = { ...start, binding: { digest: DIGEST, runId: RUN } };
  assert.ok("problem" in verify(known, observation(known, { runId: OTHER_RUN })));
});

test("a Core receipt must name the same request, operation, target and digest", () => {
  const request = intent("pause_plane");
  assert.ok("binding" in verify(request, observation(request, { coreReceipt: receipt(request) })));
  for (const change of [
    { request_id: OTHER_RUN },
    { operation: "cancel_plane" },
    { instance_id: OTHER_RUN },
    { plane_id: "side" },
    { request_digest: OTHER_DIGEST },
    { run_id: OTHER_RUN },
  ]) {
    const result = verify(request, observation(request, { coreReceipt: receipt(request, change) }));
    assert.ok("problem" in result, JSON.stringify(change));
  }
});

test("applied or refused receipts and 4xx answers resolve; the rest stay unresolved", () => {
  const request = intent("resume_run");
  const entry = newEntry(request, "answered");
  const withReceipt = (status: string): Entry => ({ ...entry, observation: observation(request, { coreReceipt: receipt(request, { status }) }) });
  assert.equal(resolved(withReceipt("applied")), true);
  assert.equal(resolved(withReceipt("refused")), true);
  assert.equal(resolved(withReceipt("queued")), false);
  assert.equal(resolved({ ...entry, answer: { code: "router_busy", status: 429 } }), true);
  assert.equal(resolved({ ...entry, answer: { code: "router_call_failed", status: 503 } }), false);
  assert.equal(resolved({ ...newEntry(request, "unknown"), readError: { code: "observation_unavailable", status: 404 } }), false);
});

test("one unresolved non-stop request per target; stop requests stay available", () => {
  const pending = [newEntry(intent("resume_run"), "unknown")];
  assert.equal(typeof outstanding(pending, "retry_invocation", "default", INSTANCE, RUN), "string");
  assert.equal(outstanding(pending, "pause_run", "default", INSTANCE, RUN), null);
  assert.equal(outstanding(pending, "resume_run", "default", INSTANCE, OTHER_RUN), null);
  assert.equal(outstanding([newEntry(intent("pause_run"), "unknown")], "resume_run", "default", INSTANCE, RUN), null);
  const starting = [newEntry(intent("start_run"), "unknown")];
  assert.equal(typeof outstanding(starting, "start_run", "default", INSTANCE, null), "string");
  assert.equal(outstanding(starting, "resume_run", "default", INSTANCE, RUN), null);
});

test("failed reads back off 5, 15 and 30 seconds, then stop", () => {
  assert.equal(nextDelay(0, 2000), 2000);
  assert.equal(nextDelay(1, 2000), 5000);
  assert.equal(nextDelay(2, 2000), 15000);
  assert.equal(nextDelay(3, 2000), 30000);
  assert.equal(nextDelay(4, 2000), null);
});

test("an export holds exact bytes, bound facts and conflicting bytes only", () => {
  const request: Intent = { ...intent("pause_run"), binding: { digest: DIGEST, runId: RUN } };
  const entry: Entry = { ...newEntry(request, "unknown"), conflicts: ["{}"] };
  const exported = JSON.parse(exportText(entry));
  assert.deepEqual(Object.keys(exported).sort(), ["binding", "body", "conflicting_bodies", "format", "home", "request_id", "sent"]);
  assert.equal(exported.format, "millrace-os-stock-request-intent/v1");
  assert.equal(exported.body, request.body);
  assert.deepEqual(exported.binding, { digest: DIGEST, run_id: RUN });
  assert.deepEqual(exported.conflicting_bodies, ["{}"]);
});

// ---- corrections after draft QA (D1–D4) -------------------------------------------

function failed(status: number, code: string): Observed<CallObservation> {
  return { ok: false, status, code, summary: code, retryable: false, receivedAt: 0 };
}

function answered(value: CallObservation): Observed<CallObservation> {
  return { ok: true, value, observedAt: "t", receivedAt: 0, text: "" };
}

test("an initial outright refusal resolves the request", () => {
  const sending = newEntry(intent("resume_run"), "sending");
  const refused = applyReply(sending, failed(429, "router_busy"), true);
  assert.equal(refused.uncertain, false);
  assert.equal(resolved(refused), true);
  assert.equal(outstanding([refused], "resume_run", "default", INSTANCE, RUN), null);
});

test("a later refused attempt never resolves a request that may have arrived earlier", () => {
  const sending = newEntry(intent("resume_run"), "sending");
  for (const earlier of [failed(0, "network_unavailable"), failed(0, "client_deadline"), failed(200, "invalid_response"), failed(503, "router_call_failed")]) {
    const unknown = applyReply(sending, earlier, true);
    assert.equal(unknown.uncertain, true, earlier.ok ? "" : earlier.code);
    for (const later of [failed(401, "authentication_required"), failed(403, "csrf_invalid"), failed(409, "request_id_reused")]) {
      const replayed = applyReply({ ...unknown, delivery: "sending" }, later, true);
      assert.equal(resolved(replayed), false);
      assert.equal(typeof outstanding([replayed], "retry_invocation", "default", INSTANCE, RUN), "string");
    }
  }
  const restored = newEntry(intent("resume_run"), "restored");
  assert.equal(resolved(applyReply(restored, failed(403, "csrf_invalid"), true)), false);
});

test("only the matching receipt resolves an uncertain request; a 404 keeps it unknown", () => {
  const source = intent("resume_run");
  const unknown = applyReply(newEntry(source, "sending"), failed(0, "network_unavailable"), true);
  const missing = applyReply(unknown, failed(404, "observation_unavailable"), false);
  assert.equal(resolved(missing), false);
  assert.deepEqual(missing.readError, { code: "observation_unavailable", status: 404 });
  const budget = applyReply(unknown, failed(409, "core_inspection_too_large"), false);
  assert.equal(budget.halted, true);
  const seen = applyReply(unknown, answered(observation(source, { coreReceipt: receipt(source, { status: "applied" }) })), false);
  assert.equal(resolved(seen), true);
  assert.deepEqual(seen.intent.binding, { digest: DIGEST, runId: RUN });
});

test("a restored binding cannot replace a non-Start request's run", () => {
  const resume = intent("resume_run");
  const elsewhere = snapshotOf([kept({ ...resume, binding: { digest: DIGEST, runId: OTHER_RUN } })]);
  const restored = readHistory({ millrace_os_intents: elsewhere });
  assert.deepEqual([restored.kept, restored.invalid], [[], true]);
  const start = intent("start_run");
  const generated = snapshotOf([kept({ ...start, binding: { digest: DIGEST, runId: OTHER_RUN } })]);
  assert.equal(readHistory({ millrace_os_intents: generated }).kept[0].intent.binding?.runId, OTHER_RUN);
  // Even an in-memory binding naming another run never becomes target authority.
  const forged: Intent = { ...resume, binding: { digest: DIGEST, runId: OTHER_RUN } };
  assert.ok("problem" in verify(forged, observation(forged, { runId: OTHER_RUN })));
  assert.deepEqual(verify(forged, observation(forged, { runId: RUN })), { binding: { digest: DIGEST, runId: RUN } });
});

test("a receipt's digest is compared with the binding even when the outer digest is null", () => {
  const source: Intent = { ...intent("pause_plane"), binding: { digest: DIGEST, runId: RUN } };
  const contradicting = observation(source, { requestDigest: null, coreReceipt: receipt(source, { request_digest: OTHER_DIGEST }) });
  assert.ok("problem" in verify(source, contradicting));
  const missing = observation(source, { requestDigest: null, coreReceipt: receipt(source, { request_digest: null }) });
  assert.ok("problem" in verify(source, missing));
  const matching = observation(source, { requestDigest: null, coreReceipt: receipt(source) });
  assert.deepEqual(verify(source, matching), { binding: source.binding });
  const fresh = intent("pause_plane");
  const first = observation(fresh, { requestDigest: null, coreReceipt: receipt(fresh) });
  assert.deepEqual(verify(fresh, first), { binding: { digest: DIGEST, runId: RUN } });
});

test("a history snapshot over 64 KiB is refused whole and stays exportable", () => {
  const reason = "x".repeat(BODY_LIMIT - 400);
  const items = Array.from({ length: 17 }, () => kept(intent("retry_invocation", { reason })));
  assert.ok(items.length <= MAX_INTENTS && items.every((item) => utf8Bytes(item.intent.body) <= BODY_LIMIT));
  const snapshot = snapshotOf(items);
  const raw = JSON.stringify(snapshot);
  assert.ok(utf8Bytes(raw) > MAX_ENTRY_BYTES);
  const restored = readHistory({ millrace_os_intents: snapshot });
  assert.deepEqual([restored.kept, restored.invalid], [[], true]);
  assert.equal(restored.raw, raw);
  assert.deepEqual(JSON.parse(unreadableText([raw])), { format: "millrace-os-stock-unreadable-intents/v1", snapshots: [raw] });
});

test("conflicting bytes and integrity facts survive history and keep replay disabled", () => {
  const original = intent("resume_run");
  const other = original.body.replace('"expected_state_version":7', '"expected_state_version":8');
  const entry: Entry = { ...newEntry(original, "unknown"), conflicts: [other], integrity: "Two history entries bound different facts." };
  const back = readHistory(stateWith(null, [entry]));
  assert.equal(back.invalid, false);
  assert.deepEqual(back.kept, [{ intent: original, conflicts: [other], integrity: entry.integrity }]);
  const restored = restoredEntry(back.kept[0]);
  assert.deepEqual([restored.conflicts, restored.integrity, restored.delivery], [[other], entry.integrity, "restored"]);
  // An entry that only saw one copy learns the other's conflicts and integrity on merge.
  const plain = merge([newEntry(original, "answered")], back.kept)[0];
  assert.deepEqual([plain.conflicts, plain.integrity], [[other], entry.integrity]);
});

test("a stored conflicting copy must be another body for the same request", () => {
  const original = intent("resume_run");
  const foreign = intent("resume_run").body; // another request ID
  const snapshot = snapshotOf([{ intent: original, conflicts: [foreign], integrity: null }]);
  assert.deepEqual(readHistory({ millrace_os_intents: snapshot }).kept, []);
  const same = snapshotOf([{ intent: original, conflicts: [original.body], integrity: null }]);
  assert.deepEqual(readHistory({ millrace_os_intents: same }).kept, []);
});

test("conflicting copies count toward the history entry's 64 KiB", () => {
  const reason = "x".repeat(BODY_LIMIT - 400);
  const base = intent("retry_invocation", { reason });
  const copies = Array.from({ length: 16 }, (_, index) =>
    base.body.replace('"expected_state_version":7', `"expected_state_version":${100 + index}`),
  );
  assert.equal(fits([kept(base)]), true);
  assert.equal(fits([{ intent: base, conflicts: copies, integrity: null }]), false);
});
