// Exact control requests: reviewed Core request/v2 envelopes, their retained
// bytes and this tab's one lexical set of request intents.
//
// A request is built once from a reviewed form and serialized once; only those
// bytes are sent, and only those bytes are replayed. Its ID never changes, its
// expected version is never refreshed and nothing is resent automatically.
// Observations are bound to the first authoritative digest (and, for Start, the
// generated run) and every later observation must agree. No digest is computed
// here: Core's canonical JSON is Python's, and the service verifies it.
// Intents live in memory and in this tab's history entries only: never in
// storage, cookies, preferences or the URL, and never with trust material.
// Core and Plus decide applicability, ownership and versions.

import type { CallObservation, Observed } from "./api.ts";

export const REQUEST_FORMAT = "millrace-router-request/v2";
export const SNAPSHOT_FORMAT = "millrace-os-stock-intents/v1";
export const EXPORT_FORMAT = "millrace-os-stock-request-intent/v1";
export const UNREADABLE_FORMAT = "millrace-os-stock-unreadable-intents/v1";
/** The key of this client's intent snapshot inside `history.state`. */
export const HISTORY_KEY = "millrace_os_intents";
/** Per history entry, as specified: at most 20 intents and 64 KiB of UTF-8 JSON. */
export const MAX_INTENTS = 20;
export const MAX_ENTRY_BYTES = 65_536;
/** The service's existing JSON body limit for POST /api/v1/router/requests. */
export const BODY_LIMIT = 4_096;

/** The fixed observation cadence (one selected-target coordinator). */
export const SUMMARY_MS = 2_000;
export const RECEIPT_FIRST_MS = 1_000;
export const RECEIPT_MS = 2_000;
export const WINDOW_MS = 300_000;
export const BACKOFF_MS = [5_000, 15_000, 30_000] as const;

export const OPERATIONS = [
  "start_run",
  "pause_run",
  "cancel_run",
  "resume_run",
  "recover_run",
  "pause_plane",
  "cancel_plane",
  "resume_plane",
  "retry_invocation",
  "redirect_plane",
  "submit_revision",
  "accept_revision",
  "reconcile_invocation",
] as const;
export type Operation = (typeof OPERATIONS)[number];

export const OPERATION_LABELS: Record<Operation, string> = {
  start_run: "Start run",
  pause_run: "Pause run",
  cancel_run: "Cancel run",
  resume_run: "Resume run",
  recover_run: "Recover run",
  pause_plane: "Pause plane",
  cancel_plane: "Cancel plane",
  resume_plane: "Resume plane",
  retry_invocation: "Retry invocation",
  redirect_plane: "Redirect plane",
  submit_revision: "Submit current revision",
  accept_revision: "Accept revision",
  reconcile_invocation: "Reconcile writer",
};

/** Stop requests carry no expected version and stay available as safety actions. */
export const STOPS: ReadonlySet<Operation> = new Set(["pause_run", "cancel_run", "pause_plane", "cancel_plane"]);
const PLANE_OPERATIONS: ReadonlySet<Operation> = new Set([
  "pause_plane",
  "cancel_plane",
  "resume_plane",
  "retry_invocation",
  "redirect_plane",
  "reconcile_invocation",
]);

const CORE_ID = /^[0-9a-f]{32}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const HOME = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$/;

type Json = Record<string, unknown>;

function record(value: unknown): Json | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Json) : null;
}

function exactKeys(value: Json, keys: readonly string[]): boolean {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => own.includes(key));
}

export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/**
 * A Core request ID: exactly 16 random bytes as 32 lowercase hex characters.
 * The bytes live in an ordinary (not shared) ArrayBuffer, which is what Web
 * Crypto's getRandomValues accepts.
 */
export function newRequestId(
  random: (bytes: Uint8Array<ArrayBuffer>) => Uint8Array = (bytes) => crypto.getRandomValues(bytes),
): string {
  const bytes = random(new Uint8Array(16));
  if (bytes.length !== 16) throw new Error("request IDs need exactly 16 random bytes");
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

// ---- envelopes ------------------------------------------------------------------

export interface Envelope {
  format: typeof REQUEST_FORMAT;
  operation: Operation;
  instance_id: string;
  run_id: string | null;
  plane_id: string | null;
  request_id: string;
  expected_state_version: number | null;
  payload: Record<string, string | boolean>;
}

/** A reviewed form: the selected target, the observed version and its inputs. */
export interface RequestSpec {
  home: string;
  operation: Operation;
  instanceId: string;
  runId: string | null;
  planeId: string | null;
  /** The reviewed observed version (run state, or instance settings for Start). Stops send none. */
  version: number | null;
  editMode?: "locked" | "live";
  invocationId?: string;
  nodeId?: string;
  candidateId?: string;
  reason?: string;
  stopped?: boolean;
}

export type Built = { ok: true; envelope: Envelope; body: string } | { ok: false; reason: string };

const isText = (value: unknown): value is string => typeof value === "string" && value !== "" && !value.includes("\0");

/** The payload keys each operation takes, as Core's request/v2 validation requires. */
function payloadProblem(operation: Operation, payload: Json): string | null {
  switch (operation) {
    case "start_run":
      return exactKeys(payload, ["edit_mode"]) && (payload.edit_mode === "locked" || payload.edit_mode === "live")
        ? null
        : "Start needs an edit mode of locked or live.";
    case "accept_revision":
      return exactKeys(payload, ["candidate_id"]) && typeof payload.candidate_id === "string" && payload.candidate_id.length === 64
        ? null
        : "Accept needs the exact 64-character pending candidate ID.";
    case "retry_invocation":
    case "reconcile_invocation": {
      const keys = operation === "reconcile_invocation" ? ["invocation_id", "reason", "stopped"] : ["invocation_id", "reason"];
      if (!exactKeys(payload, keys)) return "This request needs an invocation and a reason.";
      if (typeof payload.invocation_id !== "string" || !CORE_ID.test(payload.invocation_id)) {
        return "The invocation ID must be 32 lowercase hex characters.";
      }
      if (!isText(payload.reason)) return "A reason is required.";
      if (operation === "reconcile_invocation" && payload.stopped !== true) {
        return "Reconcile needs your explicit attestation that every writer has stopped.";
      }
      return null;
    }
    case "redirect_plane":
      if (!exactKeys(payload, ["node_id", "reason"])) return "Redirect needs a node and a reason.";
      if (typeof payload.node_id !== "string" || !IDENTIFIER.test(payload.node_id)) return "Choose a node of the active revision.";
      return isText(payload.reason) ? null : "A reason is required.";
    default:
      return exactKeys(payload, []) ? null : "This request takes no payload.";
  }
}

/** The request/v2 shape this client sends; a string explains a refusal. */
export function checkEnvelope(value: unknown): Envelope | string {
  const request = record(value);
  const keys = ["format", "operation", "instance_id", "run_id", "plane_id", "request_id", "expected_state_version", "payload"];
  if (request === null || !exactKeys(request, keys)) return "The request must have exactly the eight request/v2 fields.";
  const operation = request.operation as Operation;
  if (request.format !== REQUEST_FORMAT || !OPERATIONS.includes(operation)) return "Unsupported request format or operation.";
  if (typeof request.request_id !== "string" || !CORE_ID.test(request.request_id)) return "Invalid request ID.";
  if (typeof request.instance_id !== "string" || !CORE_ID.test(request.instance_id)) return "Invalid instance ID.";
  if (operation === "start_run") {
    if (request.run_id !== null) return "Start targets an instance: its run must be null.";
  } else if (typeof request.run_id !== "string" || !CORE_ID.test(request.run_id)) {
    return "This request needs the exact run.";
  }
  if (PLANE_OPERATIONS.has(operation)) {
    if (typeof request.plane_id !== "string" || !IDENTIFIER.test(request.plane_id)) return "Choose the exact plane.";
  } else if (request.plane_id !== null) {
    return "This request targets the whole run: its plane must be null.";
  }
  const version = request.expected_state_version;
  if (STOPS.has(operation)) {
    if (version !== null) return "Stop requests carry no expected version.";
  } else if (typeof version !== "number" || !Number.isInteger(version) || version < 1) {
    return "This request needs the observed state version.";
  }
  const payload = record(request.payload);
  if (payload === null) return "The payload must be an object.";
  const problem = payloadProblem(operation, payload);
  return problem ?? (request as unknown as Envelope);
}

function payloadOf(spec: RequestSpec): Record<string, string | boolean> {
  switch (spec.operation) {
    case "start_run":
      return { edit_mode: spec.editMode ?? "locked" };
    case "accept_revision":
      return { candidate_id: spec.candidateId ?? "" };
    case "retry_invocation":
      return { invocation_id: spec.invocationId ?? "", reason: spec.reason ?? "" };
    case "redirect_plane":
      return { node_id: spec.nodeId ?? "", reason: spec.reason ?? "" };
    case "reconcile_invocation":
      return { invocation_id: spec.invocationId ?? "", reason: spec.reason ?? "", stopped: spec.stopped === true };
    default:
      return {};
  }
}

/** Build and serialize one reviewed request with a new ID. The bytes are final. */
export function buildRequest(spec: RequestSpec, requestId: string): Built {
  if (!HOME.test(spec.home)) return { ok: false, reason: "Invalid home." };
  const envelope = {
    format: REQUEST_FORMAT,
    operation: spec.operation,
    instance_id: spec.instanceId,
    run_id: spec.operation === "start_run" ? null : spec.runId,
    plane_id: PLANE_OPERATIONS.has(spec.operation) ? spec.planeId : null,
    request_id: requestId,
    expected_state_version: STOPS.has(spec.operation) ? null : spec.version,
    payload: payloadOf(spec),
  };
  const checked = checkEnvelope(envelope);
  if (typeof checked === "string") return { ok: false, reason: checked };
  const body = JSON.stringify({ home: spec.home, request: checked });
  if (utf8Bytes(body) > BODY_LIMIT) {
    return { ok: false, reason: `The request is larger than the service's ${BODY_LIMIT.toLocaleString("en")}-byte limit; shorten the reason.` };
  }
  return { ok: true, envelope: checked, body };
}

// ---- intents ----------------------------------------------------------------------

/** First authoritative observation facts; later observations must agree. */
export interface Binding {
  digest: string;
  /** For Start, the run Core generated; otherwise the request's own run. */
  runId: string | null;
}

/** What one history entry keeps for a request: exact bytes and bound facts only. */
export interface Intent {
  home: string;
  requestId: string;
  /** The exact POST body, `{"home":…,"request":{…}}`, sent and replayed unchanged. */
  body: string;
  /** Dispatch was attempted (from Send onwards). */
  sent: boolean;
  binding: Binding | null;
}

/** The envelope inside an intent's retained body (validated on creation and restore). */
export function envelopeOf(intent: Intent): Envelope {
  return (JSON.parse(intent.body) as { request: Envelope }).request;
}

function bodyEnvelope(body: string, home: string, requestId: string): Envelope | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return null;
  }
  const wrapper = record(parsed);
  if (wrapper === null || !exactKeys(wrapper, ["home", "request"]) || wrapper.home !== home) return null;
  const envelope = checkEnvelope(wrapper.request);
  return typeof envelope === "string" || envelope.request_id !== requestId ? null : envelope;
}

export function intentFromBuilt(home: string, built: { envelope: Envelope; body: string }): Intent {
  return { home, requestId: built.envelope.request_id, body: built.body, sent: true, binding: null };
}

/** What one history entry keeps per request: exact bytes, bound facts and integrity state. */
export interface Kept {
  intent: Intent;
  /** Other bytes seen under this home and ID: replay stays disabled. */
  conflicts: string[];
  /** Why an observation or another entry contradicted this request, if one did. */
  integrity: string | null;
}

export const kept = (intent: Intent): Kept => ({ intent, conflicts: [], integrity: null });

const KEPT_KEYS = ["home", "request_id", "body", "sent", "binding", "conflicting_bodies", "integrity"];

function parseKept(value: unknown): Kept | null {
  const item = record(value);
  if (item === null || !exactKeys(item, KEPT_KEYS)) return null;
  const intent = parseIntent(item);
  const conflicts = item.conflicting_bodies;
  if (intent === null || !Array.isArray(conflicts)) return null;
  for (const body of conflicts) {
    // A conflicting copy is another valid body under this same home and ID.
    if (typeof body !== "string" || body === intent.body || utf8Bytes(body) > BODY_LIMIT) return null;
    if (bodyEnvelope(body, intent.home, intent.requestId) === null) return null;
  }
  if (item.integrity !== null && (typeof item.integrity !== "string" || item.integrity === "")) return null;
  return { intent, conflicts: conflicts as string[], integrity: item.integrity as string | null };
}

function parseIntent(item: Json): Intent | null {
  const { home, request_id: requestId, body, sent } = item;
  if (typeof home !== "string" || !HOME.test(home) || typeof requestId !== "string" || typeof body !== "string") return null;
  if (typeof sent !== "boolean" || utf8Bytes(body) > BODY_LIMIT) return null;
  const envelope = bodyEnvelope(body, home, requestId);
  if (envelope === null) return null;
  let binding: Binding | null = null;
  if (item.binding !== null) {
    const bound = record(item.binding);
    if (bound === null || !exactKeys(bound, ["digest", "run_id"])) return null;
    if (typeof bound.digest !== "string" || !SHA256.test(bound.digest)) return null;
    if (bound.run_id !== null && (typeof bound.run_id !== "string" || !CORE_ID.test(bound.run_id))) return null;
    // Only Start learns a run; any other request's run is its envelope's, exactly.
    if (envelope.operation !== "start_run" && bound.run_id !== envelope.run_id) return null;
    binding = { digest: bound.digest, runId: bound.run_id as string | null };
  }
  return { home, requestId, body, sent, binding };
}

export function snapshotOf(items: Kept[]): Json {
  return {
    format: SNAPSHOT_FORMAT,
    intents: items.map(({ intent, conflicts, integrity }) => ({
      home: intent.home,
      request_id: intent.requestId,
      body: intent.body,
      sent: intent.sent,
      binding: intent.binding === null ? null : { digest: intent.binding.digest, run_id: intent.binding.runId },
      conflicting_bodies: conflicts,
      integrity,
    })),
  };
}

/** Whether one history entry may hold these requests, conflicting copies included (count and UTF-8 bytes). */
export function fits(items: Kept[]): boolean {
  return items.length <= MAX_INTENTS && utf8Bytes(JSON.stringify(snapshotOf(items))) <= MAX_ENTRY_BYTES;
}

/** A history state carrying these requests beside whatever else it holds. */
export function stateWith(state: unknown, items: Kept[]): Json {
  return { ...(record(state) ?? {}), [HISTORY_KEY]: snapshotOf(items) };
}

function rawText(value: unknown): string | null {
  try {
    return JSON.stringify(value) ?? null;
  } catch {
    return null;
  }
}

/**
 * The intents a history entry holds. A snapshot must itself be a bounded entry
 * (20 intents, 64 KiB of UTF-8 JSON). Anything not restored is reported, and
 * its raw text is returned so it can be exported rather than silently lost.
 */
export function readHistory(state: unknown): { kept: Kept[]; invalid: boolean; raw: string | null } {
  const holder = record(state);
  if (holder === null || !(HISTORY_KEY in holder)) return { kept: [], invalid: false, raw: null };
  const raw = rawText(holder[HISTORY_KEY]);
  const snapshot = record(holder[HISTORY_KEY]);
  const items = snapshot !== null && snapshot.format === SNAPSHOT_FORMAT && Array.isArray(snapshot.intents) ? snapshot.intents : null;
  if (items === null || items.length > MAX_INTENTS || raw === null || utf8Bytes(raw) > MAX_ENTRY_BYTES) {
    return { kept: [], invalid: true, raw };
  }
  const parsed = items.map(parseKept);
  const valid = parsed.filter((item): item is Kept => item !== null);
  const invalid = valid.length !== parsed.length;
  return { kept: valid, invalid, raw: invalid ? raw : null };
}

/** Deliberate export of history data that could not be restored, byte for byte. */
export function unreadableText(raws: string[]): string {
  return JSON.stringify({ format: UNREADABLE_FORMAT, snapshots: raws }, null, 1);
}

// ---- the lexical set ------------------------------------------------------------------

export type Delivery = "sending" | "answered" | "unknown" | "restored";

/** One request in the tab's lexical set, with what has been observed about it. */
export interface Entry extends Kept {
  delivery: Delivery;
  /** The latest observation that matched the bound identity. */
  observation: CallObservation | null;
  /** The service's error answer to the latest send or replay (a refusal, never success). */
  answer: { code: string; status: number } | null;
  /** The latest failed observation read: no observation, which is not an answer. */
  readError: { code: string; status: number } | null;
  /** A strict-view budget refusal stopped automatic observation of this request. */
  halted: boolean;
  /**
   * Some send of this request may have arrived (lost or unreadable answer, a
   * 5xx, or restored from history). Only Core's matching applied/refused
   * receipt or an explicit retire ends that; a later attempt's answer cannot.
   */
  uncertain: boolean;
}

export const keyOf = (intent: { home: string; requestId: string }): string => `${intent.home}|${intent.requestId}`;

export function newEntry(intent: Intent, delivery: Delivery): Entry {
  return {
    intent,
    delivery,
    observation: null,
    answer: null,
    readError: null,
    conflicts: [],
    integrity: null,
    halted: false,
    uncertain: delivery === "unknown" || (delivery === "restored" && intent.sent),
  };
}

/** A request restored from history: observe-only, keeping its conflicts and integrity facts. */
export function restoredEntry(item: Kept): Entry {
  return { ...newEntry(item.intent, "restored"), conflicts: item.conflicts.slice(), integrity: item.integrity };
}

/**
 * Merge another history entry's requests into the lexical set. Nothing is
 * dropped: a new ID joins as an observe-only restored request, the same bytes
 * keep the bound facts, and different bytes under one ID are an integrity
 * conflict that disables replay. Conflicting copies and integrity facts from
 * either side are kept, so they survive the merged entry being written back.
 */
export function merge(entries: Entry[], incoming: Kept[]): Entry[] {
  const next = entries.slice();
  for (const item of incoming) {
    const intent = item.intent;
    const index = next.findIndex((entry) => keyOf(entry.intent) === keyOf(intent));
    if (index < 0) {
      next.push(restoredEntry(item));
      continue;
    }
    let entry = next[index];
    if (intent.sent && !entry.intent.sent) entry = { ...entry, uncertain: true };
    const conflicts = entry.conflicts.slice();
    for (const body of [...item.conflicts, intent.body]) {
      if (body !== entry.intent.body && !conflicts.includes(body)) conflicts.push(body);
    }
    entry = { ...entry, conflicts, integrity: entry.integrity ?? item.integrity };
    if (entry.intent.body === intent.body) {
      const mine = entry.intent.binding;
      const theirs = intent.binding;
      if (mine !== null && theirs !== null && (mine.digest !== theirs.digest || mine.runId !== theirs.runId)) {
        entry = { ...entry, integrity: entry.integrity ?? "Two history entries bound different facts to this request." };
      } else if (mine === null && theirs !== null) {
        entry = { ...entry, intent: { ...entry.intent, binding: theirs, sent: entry.intent.sent || intent.sent } };
      } else if (intent.sent && !entry.intent.sent) {
        entry = { ...entry, intent: { ...entry.intent, sent: true } };
      }
    }
    next[index] = entry;
  }
  return next;
}

// ---- observations ---------------------------------------------------------------------

/**
 * Check an observation against the intent's own target and bound identity.
 * The first observation that carries a digest binds it (and, for Start, a
 * generated run); later ones must agree. Returns the binding to keep, or why
 * the observation does not belong to this request.
 */
export function verify(intent: Intent, observation: CallObservation): { binding: Binding | null } | { problem: string } {
  const envelope = envelopeOf(intent);
  const start = envelope.operation === "start_run";
  if (observation.home !== intent.home || observation.requestId !== intent.requestId) {
    return { problem: "The observation names another request." };
  }
  if (observation.instanceId !== null && observation.instanceId !== envelope.instance_id) {
    return { problem: "The observation names another instance." };
  }
  const bound = intent.binding;
  const receipt = observation.coreReceipt;
  if (receipt !== null) {
    if (receipt.request_id !== intent.requestId || receipt.operation !== envelope.operation) {
      return { problem: "The Core receipt names another request or operation." };
    }
    if (receipt.instance_id !== envelope.instance_id || receipt.plane_id !== envelope.plane_id) {
      return { problem: "The Core receipt names another target." };
    }
    if (typeof receipt.request_digest !== "string" || !SHA256.test(receipt.request_digest)) {
      return { problem: "The Core receipt carries no valid request digest." };
    }
    if (observation.requestDigest !== null && receipt.request_digest !== observation.requestDigest) {
      return { problem: "The Core receipt digest differs from the observation's." };
    }
  }
  // The effective digest is the observation's or its receipt's (they agree);
  // every one must match the digest first bound to these bytes.
  const digest = observation.requestDigest ?? (receipt === null ? null : (receipt.request_digest as string));
  if (bound !== null && digest !== null && digest !== bound.digest) {
    return { problem: "The observed request digest differs from the one first bound to these bytes." };
  }
  // A non-Start request's run is its envelope's alone; only Start learns a run.
  const runs = [observation.runId, receipt === null ? null : receipt.run_id].filter((run) => run !== null && run !== undefined);
  let runId = start ? (bound?.runId ?? null) : envelope.run_id;
  for (const run of runs) {
    if (typeof run !== "string" || !CORE_ID.test(run)) return { problem: "The observation names an invalid run." };
    if (runId === null && start) runId = run;
    else if (run !== runId) return { problem: "The observation names another run." };
  }
  if (digest === null) return { binding: bound };
  if (bound === null) return { binding: { digest, runId } };
  return { binding: runId === bound.runId ? bound : { digest: bound.digest, runId } };
}

export function receiptStatus(entry: Entry): string | null {
  const status = entry.observation?.coreReceipt?.status;
  return typeof status === "string" ? status : null;
}

/**
 * Resolved: Core recorded applied/refused for this exact request, or its only
 * delivery was refused outright (4xx) with no send that may have arrived.
 */
export function resolved(entry: Entry): boolean {
  const status = receiptStatus(entry);
  if (status === "applied" || status === "refused") return true;
  return (
    !entry.uncertain &&
    entry.observation === null &&
    entry.answer !== null &&
    entry.answer.status >= 400 &&
    entry.answer.status < 500
  );
}

/**
 * Fold one reply into an entry. A send's 4xx answer refuses that attempt; a
 * lost or unreadable answer or a 5xx leaves the request possibly delivered. An
 * observation that contradicts the bound identity is an integrity problem.
 */
export function applyReply(entry: Entry, result: Observed<CallObservation>, fromSend: boolean): Entry {
  if (result.ok) {
    const checked = verify(entry.intent, result.value);
    if ("problem" in checked) {
      return { ...entry, delivery: fromSend ? "answered" : entry.delivery, integrity: checked.problem };
    }
    return {
      ...entry,
      delivery: fromSend || entry.delivery === "sending" ? "answered" : entry.delivery,
      observation: result.value,
      answer: fromSend ? null : entry.answer,
      readError: null,
      intent: { ...entry.intent, binding: checked.binding },
    };
  }
  const failure = { code: result.code, status: result.status };
  if (fromSend) {
    const answered = result.status > 0 && result.code !== "invalid_response";
    return answered
      ? { ...entry, delivery: "answered", answer: failure, uncertain: entry.uncertain || result.status >= 500 }
      : { ...entry, delivery: "unknown", uncertain: true };
  }
  return { ...entry, readError: failure, halted: entry.halted || result.code === "core_inspection_too_large" };
}

/** The run (or, for Start, the instance) an entry targets, for one-outstanding-mutation checks. */
export function sameTarget(entry: Entry, home: string, instanceId: string, runId: string | null): boolean {
  const envelope = envelopeOf(entry.intent);
  if (entry.intent.home !== home || envelope.instance_id !== instanceId) return false;
  return envelope.operation === "start_run" ? runId === null : envelope.run_id === runId;
}

/** Why a new non-stop request to this target must wait, or null. Stops are never blocked. */
export function outstanding(entries: Entry[], operation: Operation, home: string, instanceId: string, runId: string | null): string | null {
  if (STOPS.has(operation)) return null;
  const blocking = entries.find(
    (entry) => !STOPS.has(envelopeOf(entry.intent).operation) && !resolved(entry) && sameTarget(entry, home, instanceId, runId),
  );
  return blocking === undefined
    ? null
    : `Request ${blocking.intent.requestId} to this target is unresolved. Observe, replay or retire it first; stop requests stay available.`;
}

/** Delay before the next automatic read after `errors` consecutive failures, or null: stop and offer Retry. */
export function nextDelay(errors: number, normal: number): number | null {
  if (errors <= 0) return normal;
  return errors <= BACKOFF_MS.length ? BACKOFF_MS[errors - 1] : null;
}

/** Deliberate export of one intent: exact bytes, bound facts and any conflicting bytes. */
export function exportText(entry: Entry): string {
  return JSON.stringify(
    {
      format: EXPORT_FORMAT,
      home: entry.intent.home,
      request_id: entry.intent.requestId,
      body: entry.intent.body,
      sent: entry.intent.sent,
      binding: entry.intent.binding === null ? null : { digest: entry.intent.binding.digest, run_id: entry.intent.binding.runId },
      conflicting_bodies: entry.conflicts,
    },
    null,
    1,
  );
}
