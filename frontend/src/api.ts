// Fixed OS API routes and the runtime validation the stock client relies on.
//
// Every read goes through `session.read`, so it is same-origin, bounded to the
// 1,049,600-byte response policy and ends after 5 seconds. A failed read is an
// observation that did not happen; it never implies that work stopped.
// Core results are validated at their envelope (contract, format, kind); the
// fields a panel shows are read through the accessors below, and anything
// absent or of an unexpected type renders as "unknown".

import { read, write, type Reply } from "./session.ts";

export const IDENTIFIER = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$/;
export const CORE_ID = /^[0-9a-f]{32}$/;
export const SHA256 = /^[0-9a-f]{64}$/;
export const STOCK_BASE = "/ui/stock/";

export interface Target {
  home: string | null;
  instance: string | null;
  run: string | null;
}

export const NO_TARGET: Target = { home: null, instance: null, run: null };

/** The shell's query grammar, identical to the server's. Null if invalid. */
export function parseTarget(search: string): Target | null {
  const params = new URLSearchParams(search);
  const keys = [...params.keys()];
  if (new Set(keys).size !== keys.length) return null;
  if (keys.some((key) => key !== "home" && key !== "instance" && key !== "run")) return null;
  const home = params.get("home");
  const instance = params.get("instance");
  const run = params.get("run");
  if (home !== null && !IDENTIFIER.test(home)) return null;
  if (instance !== null && (home === null || !CORE_ID.test(instance))) return null;
  if (run !== null && (instance === null || !CORE_ID.test(run))) return null;
  return { home, instance, run };
}

export function targetHref(target: Target): string {
  const query = new URLSearchParams();
  if (target.home !== null) query.set("home", target.home);
  if (target.instance !== null) query.set("instance", target.instance);
  if (target.run !== null) query.set("run", target.run);
  const text = query.toString();
  return STOCK_BASE + (text === "" ? "" : "?" + text);
}

export function sameTarget(a: Target, b: Target): boolean {
  return a.home === b.home && a.instance === b.instance && a.run === b.run;
}

// ---- small typed accessors -------------------------------------------------

export type Json = Record<string, unknown>;

export function record(value: unknown): Json | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Json) : null;
}

export function list(value: unknown): unknown[] | null {
  return Array.isArray(value) ? value : null;
}

export function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

export function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

// ---- observations -----------------------------------------------------------

export interface Failure {
  ok: false;
  status: number;
  code: string;
  summary: string;
  retryable: boolean;
  receivedAt: number;
}

export type Observed<T> = { ok: true; value: T; observedAt: string; receivedAt: number; text: string } | Failure;

function failure(reply: Reply): Failure {
  if (reply.ok) {
    return {
      ok: false,
      status: reply.status,
      code: "invalid_response",
      summary: "the response did not match its declared contract",
      retryable: false,
      receivedAt: reply.receivedAt,
    };
  }
  return { ok: false, ...reply.error, receivedAt: reply.receivedAt };
}

function observed<T>(reply: Reply, parse: (body: Json) => T | null): Observed<T> {
  if (!reply.ok) return failure(reply);
  const body = record(reply.body);
  const value = body === null ? null : parse(body);
  const observedAt = body === null ? null : text(body.observed_at);
  if (value === null || observedAt === null) return failure(reply);
  return { ok: true, value, observedAt, receivedAt: reply.receivedAt, text: reply.text };
}

function query(values: Record<string, string | null | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined) params.set(key, value);
  }
  const encoded = params.toString();
  return encoded === "" ? "" : "?" + encoded;
}

const segment = encodeURIComponent;

// ---- configured homes -------------------------------------------------------

export interface HomesPage {
  names: string[];
  nextCursor: string | null;
}

export async function homes(cursor: string | null): Promise<Observed<HomesPage>> {
  const reply = await read("/api/v1/router/homes" + query({ cursor }));
  return observed(reply, (body) => {
    const items = list(body.items);
    if (body.contract !== "millrace-os-router-homes/v1" || items === null) return null;
    const names = items.map((item) => text(record(item)?.name)).filter((name) => name !== null);
    return names.length === items.length ? { names, nextCursor: text(body.next_cursor) } : null;
  });
}

// ---- Core inspection views --------------------------------------------------

export interface CoreView {
  home: string;
  kind: string;
  instanceId: string | null;
  runId: string | null;
  stateVersion: unknown;
  items: unknown[];
  nextCursor: string | null;
  data: Json;
}

function coreView(kind: string): (body: Json) => CoreView | null {
  return (body) => {
    const result = record(body.data);
    const home = text(body.home);
    if (body.contract !== "millrace-os-router-view/v1" || result === null || home === null) return null;
    const items = list(result.items);
    const data = record(result.data);
    if (result.format !== "millrace-router-inspection/v1" || result.kind !== kind) return null;
    if (items === null || data === null) return null;
    return {
      home,
      kind,
      instanceId: text(result.instance_id),
      runId: text(result.run_id),
      stateVersion: result.state_version,
      items,
      nextCursor: text(result.next_cursor),
      data,
    };
  };
}

const VIEWS = "/api/v1/router/views/instances";

export async function instances(home: string, cursor: string | null): Promise<Observed<CoreView>> {
  return observed(await read(VIEWS + query({ home, cursor })), coreView("instances"));
}

export async function instance(home: string, id: string, cursor: string | null): Promise<Observed<CoreView>> {
  return observed(await read(`${VIEWS}/${segment(id)}` + query({ home, cursor })), coreView("instance"));
}

function runPath(target: Target): string {
  return `${VIEWS}/${segment(target.instance ?? "")}/runs/${segment(target.run ?? "")}`;
}

export async function runSummary(target: Target): Promise<Observed<CoreView>> {
  return observed(await read(runPath(target) + query({ home: target.home })), coreView("run-summary"));
}

export type HistorySection = "invocations" | "transitions" | "controls";

export async function runHistory(
  target: Target,
  section: HistorySection,
  cursor: string | null,
): Promise<Observed<CoreView>> {
  const path = runPath(target) + query({ home: target.home, section, cursor });
  return observed(await read(path), coreView(section));
}

/** One exact invocation by ID: zero or one row, never a guessed newest. */
export async function invocation(target: Target, invocationId: string): Promise<Observed<CoreView>> {
  const path = runPath(target) + query({ home: target.home, section: "invocations", invocation_id: invocationId });
  return observed(await read(path), coreView("invocations"));
}

export async function revisionGraph(target: Target, revisionId: string): Promise<Observed<CoreView>> {
  const path = `${runPath(target)}/revisions/${segment(revisionId)}` + query({ home: target.home });
  return observed(await read(path), coreView("graph"));
}

// ---- exact control requests ---------------------------------------------------

/**
 * One `millrace-os-router-call/v1` observation. Its phase is the OS call's, its
 * receipt is Core's; neither is run completion, writer closure or a holder
 * join (call/v1 cannot attest one).
 */
export interface CallObservation {
  home: string;
  requestId: string;
  requestDigest: string | null;
  instanceId: string | null;
  runId: string | null;
  source: "os_owned_call" | "core_receipt";
  phase: string;
  coreReceipt: Json | null;
  observedAt: string;
}

const PHASES = new Set(["admission_pending", "call_active", "call_returned", "call_failed", "receipt_observed"]);

function callObservation(body: Json): CallObservation | null {
  const home = text(body.home);
  const requestId = text(body.request_id);
  const digest = body.request_digest;
  const source = body.source;
  const phase = text(body.phase);
  const receipt = body.core_receipt;
  if (
    body.contract !== "millrace-os-router-call/v1" ||
    home === null ||
    requestId === null ||
    (digest !== null && (typeof digest !== "string" || !SHA256.test(digest))) ||
    (source !== "os_owned_call" && source !== "core_receipt") ||
    phase === null ||
    !PHASES.has(phase) ||
    (receipt !== null && record(receipt) === null) ||
    (body.instance_id !== null && text(body.instance_id) === null) ||
    (body.run_id !== null && text(body.run_id) === null)
  ) {
    return null;
  }
  return {
    home,
    requestId,
    requestDigest: digest as string | null,
    instanceId: text(body.instance_id),
    runId: text(body.run_id),
    source: source as CallObservation["source"],
    phase,
    coreReceipt: record(receipt),
    observedAt: text(body.observed_at) ?? "",
  };
}

/** POST the exact retained body bytes. 200/202 is an OS observation, never task success. */
export async function submitRequest(body: string): Promise<Observed<CallObservation>> {
  return observed(await ordinaryWrite("POST", "/api/v1/router/requests", body), callObservation);
}

/** The strict bounded receipt view by exact ID and known target. 404 stays unknown. */
export async function observeRequest(
  home: string,
  requestId: string,
  instanceId: string,
  runId: string | null,
): Promise<Observed<CallObservation>> {
  const path = `/api/v1/router/views/requests/${segment(requestId)}` + query({ home, instance_id: instanceId, run_id: runId });
  return observed(await read(path), callObservation);
}

/** Bounded validation of the current editable files; it publishes and replaces nothing. */
export async function validateCurrent(target: Target): Promise<Observed<CoreView>> {
  const body = JSON.stringify({ home: target.home, instance_id: target.instance, run_id: target.run });
  return observed(await ordinaryWrite("POST", "/api/v1/router/views/validate", body), coreView("validation"));
}

// ---- generic workflow outputs ----------------------------------------------

export interface OutputItem {
  outputId: string;
  available: boolean;
  reason: string | null;
  recordSha256: string | null;
  producer: { id: string; version: string } | null;
  label: string | null;
  publishedAt: string | null;
  invocation: { planeId: string; nodeId: string; invocationId: string; producingRevisionId: string } | null;
  report: { format: string; sha256: string } | null;
  json: { format: string; sha256: string } | null;
}

export interface OutputsPage {
  items: OutputItem[];
  nextCursor: string | null;
}

function representationInfo(value: unknown): { format: string; sha256: string } | null {
  const info = record(value);
  const format = text(info?.format);
  const sha256 = text(info?.sha256);
  return format !== null && sha256 !== null && SHA256.test(sha256) ? { format, sha256 } : null;
}

function outputItem(value: unknown): OutputItem | null {
  const item = record(value);
  const outputId = text(item?.output_id);
  if (item === null || outputId === null) return null;
  const available = item.availability === "available";
  if (!available && item.availability !== "unavailable") return null;
  const producer = record(item.producer);
  const invocationValue = record(item.invocation);
  const representations = record(item.representations);
  return {
    outputId,
    available,
    reason: text(item.reason),
    recordSha256: text(item.record_sha256),
    producer:
      producer && text(producer.id) !== null && text(producer.version) !== null
        ? { id: producer.id as string, version: producer.version as string }
        : null,
    label: text(item.label),
    publishedAt: text(item.published_at),
    invocation: invocationValue
      ? {
          planeId: text(invocationValue.plane_id) ?? "unknown",
          nodeId: text(invocationValue.node_id) ?? "unknown",
          invocationId: text(invocationValue.invocation_id) ?? "unknown",
          producingRevisionId: text(invocationValue.producing_revision_id) ?? "unknown",
        }
      : null,
    report: representationInfo(representations?.report),
    json: representations === null ? null : representationInfo(representations.json),
  };
}

const OUTPUTS = "/api/v1/router/outputs/instances";

export async function outputs(target: Target, cursor: string | null): Promise<Observed<OutputsPage>> {
  const path = `${OUTPUTS}/${segment(target.instance ?? "")}/runs/${segment(target.run ?? "")}` + query({ home: target.home, cursor });
  return observed(await read(path), (body) => {
    const items = list(body.items);
    if (body.contract !== "millrace-os-workflow-outputs/v1" || items === null) return null;
    const parsed = items.map(outputItem);
    if (parsed.some((item) => item === null)) return null;
    return { items: parsed as OutputItem[], nextCursor: text(body.next_cursor) };
  });
}

export interface OutputRead {
  reference: { instanceId: string; runId: string; outputId: string; recordSha256: string };
  producer: { id: string; version: string };
  label: string;
  publishedAt: string;
  invocation: { planeId: string; nodeId: string; invocationId: string; producingRevisionId: string };
  representation: { name: "report" | "json"; format: string; sha256: string; data: unknown };
}

export async function output(
  target: Target,
  outputId: string,
  recordSha256: string,
  representation: "report" | "json",
): Promise<Observed<OutputRead>> {
  const path =
    `${OUTPUTS}/${segment(target.instance ?? "")}/runs/${segment(target.run ?? "")}/${segment(outputId)}` +
    query({ home: target.home, record_sha256: recordSha256, representation });
  return observed(await read(path), (body) => {
    const reference = record(body.reference);
    const producer = record(body.producer);
    const invocationValue = record(body.invocation);
    const chosen = record(body.representation);
    if (
      body.contract !== "millrace-os-workflow-output/v1" ||
      body.authority !== "workflow-produced-evidence" ||
      reference === null ||
      producer === null ||
      invocationValue === null ||
      chosen === null ||
      chosen.name !== representation ||
      reference.output_id !== outputId ||
      reference.record_sha256 !== recordSha256
    ) {
      return null;
    }
    const sha256 = text(chosen.sha256);
    const format = text(chosen.format);
    if (sha256 === null || !SHA256.test(sha256) || format === null) return null;
    return {
      reference: {
        instanceId: text(reference.instance_id) ?? "",
        runId: text(reference.run_id) ?? "",
        outputId,
        recordSha256,
      },
      producer: { id: text(producer.id) ?? "unknown", version: text(producer.version) ?? "unknown" },
      label: text(body.label) ?? outputId,
      publishedAt: text(body.published_at) ?? "unknown",
      invocation: {
        planeId: text(invocationValue.plane_id) ?? "unknown",
        nodeId: text(invocationValue.node_id) ?? "unknown",
        invocationId: text(invocationValue.invocation_id) ?? "unknown",
        producingRevisionId: text(invocationValue.producing_revision_id) ?? "unknown",
      },
      representation: { name: representation, format, sha256, data: chosen.data },
    };
  });
}

// ---- shared presentation preferences ---------------------------------------

/** The presentation GET keeps its raw text so other namespaces stay byte-exact. */
export async function presentationState(): Promise<Observed<Json>> {
  return observed(await read("/api/v1/client/presentation"), (body) =>
    body.contract === "millrace-os-presentation-state/v1" ? body : null,
  );
}

export async function putPresentation(body: string): Promise<Observed<Json>> {
  return observed(await ordinaryWrite("PUT", "/api/v1/client/presentation", body), (value) =>
    value.contract === "millrace-os-presentation-state/v1" ? value : null,
  );
}

// ---- capabilities and private demo context ---------------------------------

export interface Capability {
  operationId: string;
  available: boolean;
  reason: string | null;
}

export async function capabilities(): Promise<Observed<Capability[]>> {
  return observed(await read("/api/v1/client/capabilities"), (body) => {
    const operations = list(body.operations);
    if (body.contract !== "millrace-os-client-capabilities/v1" || operations === null) return null;
    const parsed = operations.map((value) => {
      const item = record(value);
      const operationId = text(item?.operation_id);
      const available = bool(item?.available);
      return operationId === null || available === null
        ? null
        : { operationId, available, reason: text(item?.reason) };
    });
    return parsed.some((item) => item === null) ? null : (parsed as Capability[]);
  });
}

/** The saved complete-frontend choice, as far as this stock client shows it. */
export interface FrontendChoice {
  /** Why the desired frontend is not in use (stock is), or null when it is. */
  reason: string | null;
  /** The desired local frontend's ID while its manifest still matches. */
  frontendId: string | null;
  /** The saved selection file itself could not be read. */
  unreadable: boolean;
}

/** `GET /api/v1/client/frontend`: one read of the operator's frontend choice. */
export async function frontendChoice(): Promise<Observed<FrontendChoice>> {
  return observed(await read("/api/v1/client/frontend"), (body) => {
    const status = text(body.status);
    const reason = text(body.reason);
    const manifest = record(body.manifest);
    if (
      body.contract !== "millrace-os-frontend-state/v1" ||
      (status !== "absent" && status !== "ready" && status !== "invalid") ||
      record(body.effective) === null ||
      (body.reason !== null && (reason === null || !IDENTIFIER.test(reason))) ||
      (body.manifest !== null && manifest === null)
    ) {
      return null;
    }
    const frontendId = text(manifest?.frontend_id);
    return {
      reason,
      frontendId: frontendId !== null && IDENTIFIER.test(frontendId) ? frontendId : null,
      unreadable: status === "invalid",
    };
  });
}

export interface DemoOutputRef {
  reference: { instanceId: string; runId: string; outputId: string; recordSha256: string };
  reportSha256: string;
  jsonSha256: string | null;
}

export type DemoMode =
  | { kind: "ordinary" }
  | { kind: "demo"; home: string; instanceId: string; runId: string; finishAvailable: boolean; acceptedOutputRefs: DemoOutputRef[]; outputError: string | null }
  | { kind: "pending" }
  | { kind: "unknown"; code: string };

// This shared send guard is synchronous at the network boundary. A reviewed
// form, validation request or retained Replay cannot bypass a context change.
let mutationContext: DemoMode | null = null;

function blockedMutation(): Reply {
  return { ok: false, status: 0, receivedAt: Date.now(), error: {
    status: 0, code: "demo_mutation_disabled", retryable: false,
    summary: "The terminal owns this temporary demo; Finish cancels or closes it.",
  } };
}

async function ordinaryWrite(method: "PUT" | "POST", path: string, body: string): Promise<Reply> {
  if (mutationContext?.kind !== "ordinary") return blockedMutation();
  return write(method, path, body);
}

function exactKeys(value: Json, keys: string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

function demoOutputRef(value: unknown, instanceId: string, runId: string): DemoOutputRef | null {
  const item = record(value);
  const reference = record(item?.reference);
  if (item === null || reference === null || !exactKeys(item, ["reference", "report_sha256", "json_sha256"]) ||
      !exactKeys(reference, ["instance_id", "run_id", "output_id", "record_sha256"]) ||
      reference.instance_id !== instanceId || reference.run_id !== runId ||
      typeof reference.output_id !== "string" || !IDENTIFIER.test(reference.output_id) ||
      typeof reference.record_sha256 !== "string" || !SHA256.test(reference.record_sha256) ||
      typeof item.report_sha256 !== "string" || !SHA256.test(item.report_sha256) ||
      (item.json_sha256 !== null && (typeof item.json_sha256 !== "string" || !SHA256.test(item.json_sha256)))) return null;
  return { reference: { instanceId, runId, outputId: reference.output_id, recordSha256: reference.record_sha256 },
    reportSha256: item.report_sha256, jsonSha256: item.json_sha256 as string | null };
}

async function readDemoContext(): Promise<DemoMode> {
  const reply = await read("/api/v1/demo/context");
  if (!reply.ok) {
    if (reply.status === 404 && reply.error.code === "demo_context_unavailable") return { kind: "ordinary" };
    if (reply.status === 503 && reply.error.code === "demo_context_pending") return { kind: "pending" };
    return { kind: "unknown", code: reply.error.code };
  }
  const body = record(reply.body);
  const instanceId = text(body?.instance_id);
  const runId = text(body?.run_id);
  const finishAvailable = bool(body?.finish_available);
  if (
    body?.format !== "millrace-os-demo-context/v1" ||
    !exactKeys(body, ["format", "home", "instance_id", "run_id", "finish_available", "accepted_output_refs", "output_error"]) ||
    body.home !== "demo" ||
    instanceId === null || !CORE_ID.test(instanceId) ||
    runId === null || !CORE_ID.test(runId) ||
    finishAvailable === null || !Array.isArray(body.accepted_output_refs) ||
    (body.output_error !== null && (typeof body.output_error !== "string" || !IDENTIFIER.test(body.output_error))) ||
    new TextEncoder().encode(reply.text).length > 4096
  ) {
    return { kind: "unknown", code: "invalid_response" };
  }
  const refs = (body.accepted_output_refs as unknown[]).map((item) => demoOutputRef(item, instanceId, runId));
  if (refs.some((item) => item === null) || new Set(refs.map((item) => item?.reference.outputId)).size !== refs.length) {
    return { kind: "unknown", code: "invalid_response" };
  }
  return { kind: "demo", home: "demo", instanceId, runId, finishAvailable,
    acceptedOutputRefs: refs as DemoOutputRef[], outputError: body.output_error as string | null };
}

/** Only an authenticated typed 404 establishes an ordinary service. */
export async function demoContext(): Promise<DemoMode> {
  const mode = await readDemoContext();
  mutationContext = mode;
  return mode;
}

/** The sole demo browser mutation; exact visible target checked before I/O. */
export async function finishDemo(target: Target): Promise<{ ok: true; requested: true } | Failure> {
  const mode = mutationContext;
  if (mode?.kind !== "demo" || !mode.finishAvailable || target.home !== mode.home ||
      target.instance !== mode.instanceId || target.run !== mode.runId) {
    return failure(blockedMutation());
  }
  const reply = await write("POST", "/api/v1/demo/finish", "{}");
  if (!reply.ok) return failure(reply);
  const body = record(reply.body);
  if (body === null || !exactKeys(body, ["format", "requested"]) ||
      body.format !== "millrace-os-demo-finish/v1" || body.requested !== true) return failure(reply);
  // This fixed acknowledgement has no observed_at: it accepts the intent,
  // without claiming that shutdown, export or writer closure was observed.
  return { ok: true, requested: true };
}

// ---- captured revision graph -------------------------------------------------

export interface CapturedAction {
  kind: string | null;
  target: string | null;
  reason: string | null;
  limit: number | null;
  exhausted: CapturedAction | null;
}

export interface CapturedNode {
  id: string;
  binding: string | null;
  timeoutSeconds: number | null;
  outcomes: string[];
  routes: [string, CapturedAction][];
  onError: CapturedAction | null;
  onUnknown: CapturedAction | null;
  requiresTerminal: { planeId: string; status: string }[];
}

export interface CapturedPlane {
  planeId: string;
  start: string | null;
  nodes: CapturedNode[];
}

export interface CapturedSeam {
  id: string;
  producerPlane: string | null;
  producerNode: string | null;
  outcome: string | null;
  consumerPlane: string | null;
  consumerNode: string | null;
}

export interface CapturedGraph {
  revisionId: string;
  graphFormat: string | null;
  limits: Json;
  bindings: string[];
  planes: CapturedPlane[];
  seams: CapturedSeam[];
}

const EMPTY_ACTION: CapturedAction = { kind: null, target: null, reason: null, limit: null, exhausted: null };

function capturedAction(value: unknown): CapturedAction | null {
  const action = record(value);
  if (action === null) return null;
  return {
    kind: text(action.kind),
    target: text(action.target),
    reason: text(action.reason),
    limit: integer(action.limit),
    exhausted: capturedAction(action.exhausted),
  };
}

/** Core's captured graph projection; null if its frozen shape is not present. */
export function parseCapturedGraph(data: Json): CapturedGraph | null {
  const revisionId = text(data.revision_id);
  const planes = record(data.planes);
  if (revisionId === null || planes === null) return null;
  const parsedPlanes: CapturedPlane[] = [];
  for (const [planeId, value] of Object.entries(planes).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    const plane = record(value);
    const nodes = record(plane?.nodes);
    if (plane === null || nodes === null) return null;
    const parsedNodes: CapturedNode[] = [];
    for (const [nodeId, nodeValue] of Object.entries(nodes).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
      const node = record(nodeValue);
      if (node === null) return null;
      const routes = record(node.routes) ?? {};
      parsedNodes.push({
        id: nodeId,
        binding: text(node.binding),
        timeoutSeconds: typeof node.timeout_seconds === "number" ? node.timeout_seconds : null,
        outcomes: (list(node.outcomes) ?? []).map((item) => text(item) ?? "unknown"),
        routes: Object.entries(routes)
          .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
          .map(([outcome, action]): [string, CapturedAction] => [outcome, capturedAction(action) ?? EMPTY_ACTION]),
        onError: capturedAction(node.on_error),
        onUnknown: capturedAction(node.on_unknown),
        requiresTerminal: (list(node.requires_terminal) ?? []).map((item) => ({
          planeId: text(record(item)?.plane_id) ?? "unknown",
          status: text(record(item)?.status) ?? "unknown",
        })),
      });
    }
    parsedPlanes.push({ planeId, start: text(plane.start), nodes: parsedNodes });
  }
  const seams = Object.entries(record(data.seams) ?? {})
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([id, value]) => {
      const seam = record(value);
      return {
        id,
        producerPlane: text(seam?.producer_plane),
        producerNode: text(seam?.producer_node),
        outcome: text(seam?.outcome),
        consumerPlane: text(seam?.consumer_plane),
        consumerNode: text(seam?.consumer_node),
      };
    });
  return {
    revisionId,
    graphFormat: text(data.graph_format),
    limits: record(data.limits) ?? {},
    bindings: (list(data.bindings) ?? []).map((item) => text(item) ?? "unknown"),
    planes: parsedPlanes,
    seams,
  };
}


// ---- display helpers --------------------------------------------------------

/** Exact code text for recorded values; "unknown" for anything not observed. */
export function display(value: unknown): string {
  if (value === null || value === undefined) return "unknown";
  if (typeof value === "string") return value === "" ? "(empty)" : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.length === 0 ? "none" : value.map(display).join(", ");
  return JSON.stringify(value);
}

export function yesNo(value: unknown, yes: string, no: string): string {
  return value === true ? yes : value === false ? no : "unknown";
}

export function counts(value: unknown): string {
  const map = record(value);
  if (map === null) return "unknown";
  const entries = Object.entries(map).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return entries.length === 0 ? "none" : entries.map(([key, count]) => `${key} ${display(count)}`).join(", ");
}

export function age(observedAt: string, now: number): string {
  const then = Date.parse(observedAt);
  if (Number.isNaN(then)) return "time unknown";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes} min ago` : `${Math.floor(minutes / 60)} h ${minutes % 60} min ago`;
}

/** A frozen Core key: absent is "unknown", recorded null is "none". */
export function recorded(container: Json | null, key: string): string {
  if (container === null || !(key in container)) return "unknown";
  return container[key] === null ? "none" : display(container[key]);
}

/** Save exact in-memory text as a local file; nothing is fetched. */
export function saveText(content: string, filename: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: "application/json" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
