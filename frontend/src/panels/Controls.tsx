// Exact control forms, the unhideable request area and monitoring status (UI3).
//
// A form shows the exact target, operation, payload, expected version and new
// request ID before Send; changing any input discards the review. Reasons and
// the reconcile attestation are deliberate inputs, never preselected. Disabled
// controls are hints only: Core and Plus enforce applicability. The request
// area lists every request of this tab, with exact Observe, Replay, Export and
// Retire; nothing leaves it except by an explicit Retire. A form tells its
// parent while it holds an unsent review, so leaving the page waits for it.

import { useEffect, useState } from "react";

import { display, saveText } from "../api.ts";
import {
  buildRequest,
  envelopeOf,
  exportText,
  keyOf,
  newEntry,
  newRequestId,
  OPERATION_LABELS,
  OPERATIONS,
  receiptStatus,
  resolved,
  STOPS,
  unreadableText,
  type Built,
  type Entry,
  type Operation,
} from "../requests.ts";

export interface ControlTarget {
  home: string;
  instanceId: string;
  /** Null on the instance screen, where only Start applies. */
  runId: string | null;
  /** The observed version a reviewed request carries, and when it was observed. */
  version: { value: number; observedAt: string } | null;
  planes: string[];
  candidateId: string | null;
  /** Captured nodes of the active revision by plane; null until that graph is read. */
  activeNodes: Record<string, string[]> | null;
  activeGraph: "absent" | "loading" | "loaded" | "failed";
  selectedInvocation: { invocationId: string; planeId: string | null } | null;
}

export type SendResult = { sent: true } | { sent: false; reason: string; unprotected?: boolean };

type Reviewed = Extract<Built, { ok: true }>;

/** An unsent review in the request export format: exact bytes, never sent. */
function reviewedText(home: string, reviewed: Reviewed): string {
  // `exportText` reads only the intent and its conflicting bodies.
  const intent = { home, requestId: reviewed.envelope.request_id, body: reviewed.body, sent: false, binding: null };
  return exportText(newEntry(intent, "sending"));
}

function initialOperation(target: ControlTarget): Operation | "" {
  return target.runId === null ? "start_run" : "";
}

const EFFECTS: Record<Operation, string> = {
  start_run: "Create a new run of this instance using the selected edit mode.",
  pause_run: "Request a pause of the whole run. Core decides when routing can pause.",
  cancel_run: "Request cancellation of the whole run. Cancellation is not proof that every writer stopped.",
  resume_run: "Ask Core to resume this run under its recorded control state.",
  recover_run: "Ask Core to recover this run. Review the observed state and uncertainty first.",
  pause_plane: "Request a pause of one exact plane; other planes keep their own state.",
  cancel_plane: "Request cancellation of one exact plane; this does not prove writer closure.",
  resume_plane: "Ask Core to resume one exact plane at its recorded resume target.",
  retry_invocation: "Request another attempt of the exact invocation you identify.",
  redirect_plane: "Request a move to a node of this plane's active captured revision.",
  submit_revision: "Submit the current workflow as a revision candidate. This does not accept it.",
  accept_revision: "Accept the observed pending candidate; Core may resume routing under its existing control.",
  reconcile_invocation: "Record your explicit attestation that every writer of this exact invocation has stopped.",
};

export function ControlForm({
  target,
  choice,
  disabled,
  outstanding,
  onNeedGraph,
  onSend,
  onReviewChange,
}: {
  target: ControlTarget;
  choice: { planeId: string; serial: number } | null;
  /** Why no request may be sent now (trust, context, availability, capacity), or null. */
  disabled: string | null;
  /** Why a new request of this kind must wait for an unresolved one, or null. */
  outstanding: (operation: Operation) => string | null;
  onNeedGraph: () => void;
  onSend: (reviewed: Reviewed, unprotected: boolean) => Promise<SendResult>;
  /** Whether this form holds an unsent reviewed request (a stable callback). */
  onReviewChange: (pending: boolean) => void;
}) {
  const operations = OPERATIONS.filter((operation) => (target.runId === null) === (operation === "start_run"));
  const [operation, setOperation] = useState<Operation | "">(() => initialOperation(target));
  const [planeId, setPlaneId] = useState("");
  const [invocationId, setInvocationId] = useState("");
  // A node choice and the reconcile attestation hold only for the exact identity
  // they were made for: another plane, node set, invocation or request clears them.
  const [nodeChoice, setNodeChoice] = useState<{ key: string; node: string } | null>(null);
  const [reason, setReason] = useState("");
  const [attested, setAttested] = useState<string | null>(null);
  const [editMode, setEditMode] = useState<"locked" | "live">("locked");
  const [review, setReview] = useState<Reviewed | null>(null);
  const [message, setMessage] = useState<{ text: string; unprotected?: boolean } | null>(null);
  const [sending, setSending] = useState(false);

  const plane = planeId || target.planes[0] || "";
  const planeOperation = ["pause_plane", "cancel_plane", "resume_plane", "retry_invocation", "redirect_plane", "reconcile_invocation"].includes(
    operation,
  );
  const nodes = target.activeNodes?.[plane] ?? null;
  const nodeKey = `${plane}\n${nodes === null ? "" : nodes.join("\n")}`;
  const nodeId = nodeChoice !== null && nodeChoice.key === nodeKey ? nodeChoice.node : "";
  const attestation = `${operation}\n${plane}\n${invocationId.trim()}`;
  const stopped = attested === attestation;
  // The parent keeps full-document exits while a review is unsent; an unmounted
  // form holds none.
  useEffect(() => {
    onReviewChange(review !== null);
    return () => onReviewChange(false);
  }, [review, onReviewChange]);

  // Redirect needs the active revision's captured nodes: read that graph once,
  // through the same coordinator, only when Redirect is chosen.
  useEffect(() => {
    if (operation === "redirect_plane" && target.activeGraph === "absent") onNeedGraph();
  }, [operation, target.activeGraph, onNeedGraph]);

  // Any change to an input invalidates a reviewed request; a new review gets a new ID.
  const edit = <T,>(set: (value: T) => void) => (value: T) => {
    set(value);
    setReview(null);
    setMessage(null);
  };
  // Changing what a request is about clears the choices made for the old identity.
  const reidentify = <T,>(set: (value: T) => void, clearNode: boolean) => (value: T) => {
    edit(set)(value);
    setAttested(null);
    if (clearNode) setNodeChoice(null);
  };

  // A contextual link only fills presentation inputs; review and Send remain deliberate.
  useEffect(() => {
    if (choice === null || review !== null || sending) return;
    reidentify(setOperation, true)("resume_plane");
    setPlaneId(choice.planeId);
  }, [choice]);

  const versionNeeded = operation !== "" && !STOPS.has(operation);
  const waiting = operation === "" ? null : outstanding(operation);
  const blocked = disabled ?? waiting ?? (operation === "" ? "Choose a request to see its scope and review the exact target." : null) ?? (versionNeeded && target.version === null ? "No fresh observed version: refresh first." : null);

  const doReview = () => {
    if (operation === "") return;
    if (operation === "redirect_plane" && (nodes === null || !nodes.includes(nodeId))) {
      setReview(null);
      setMessage({ text: "Choose a node of this plane's active captured revision." });
      return;
    }
    const built = buildRequest(
      {
        home: target.home,
        operation,
        instanceId: target.instanceId,
        runId: target.runId,
        planeId: planeOperation ? plane : null,
        version: target.version?.value ?? null,
        editMode,
        invocationId: invocationId.trim(),
        nodeId,
        candidateId: target.candidateId ?? undefined,
        reason,
        stopped,
      },
      newRequestId(),
    );
    setMessage(built.ok ? null : { text: built.reason });
    setReview(built.ok ? built : null);
  };

  const send = async (unprotected: boolean) => {
    if (review === null) return;
    setSending(true);
    const result = await onSend(review, unprotected);
    setSending(false);
    if (result.sent) {
      setReview(null);
      setReason("");
      setAttested(null);
      setMessage(null);
    } else {
      setMessage({ text: result.reason, unprotected: result.unprotected });
    }
  };

  return (
    <section className="region control-region" aria-labelledby="control-heading">
      <p className="eyebrow">Deliberate action</p>
      <h2 id="control-heading" tabIndex={-1}>{target.runId === null ? "Start a run" : "Control this run"}</h2>
      <p className="section-intro">{operation === "" ? "Choose a request to see its scope, then review the exact target and bytes before sending." : "Review the exact target and bytes before sending. The service decides applicability."}</p>
      <div className="control-form">
        {operations.length > 1 ? (
          <label>
            Request{" "}
            <select value={operation} onChange={(event) => reidentify(setOperation, true)(event.target.value as Operation | "")}>
              <option value="">Choose a request…</option>
              {operations.map((item) => (
                <option key={item} value={item}>
                  {OPERATION_LABELS[item]}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {operation === "" ? null : <p className="operation-effect">{EFFECTS[operation]}</p>}
        {planeOperation ? (
          <label>
            Plane{" "}
            <select value={plane} onChange={(event) => reidentify(setPlaneId, true)(event.target.value)}>
              {target.planes.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {operation === "start_run" ? (
          <fieldset>
            <legend>Edit mode</legend>
            <label>
              <input type="radio" name="edit-mode" checked={editMode === "locked"} onChange={() => edit(setEditMode)("locked")} /> locked
            </label>
            <label>
              <input type="radio" name="edit-mode" checked={editMode === "live"} onChange={() => edit(setEditMode)("live")} /> live
            </label>
          </fieldset>
        ) : null}
        {operation === "retry_invocation" || operation === "reconcile_invocation" ? (
          <div className="control-form__row">
            <label>
              Invocation{" "}
              <input
                value={invocationId}
                spellCheck={false}
                autoComplete="off"
                maxLength={32}
                onChange={(event) => reidentify(setInvocationId, false)(event.target.value)}
              />
            </label>
            {target.selectedInvocation !== null ? (
              <button
                type="button"
                onClick={() => {
                  const selected = target.selectedInvocation;
                  if (selected === null) return;
                  reidentify(setInvocationId, false)(selected.invocationId);
                  if (selected.planeId !== null && target.planes.includes(selected.planeId)) setPlaneId(selected.planeId);
                }}
              >
                Use selected invocation
              </button>
            ) : null}
          </div>
        ) : null}
        {operation === "redirect_plane" ? (
          nodes === null ? (
            <p className="muted" role="status">
              {target.activeGraph === "failed"
                ? "The active revision's captured graph could not be read; no node can be chosen."
                : "Reading the active revision's captured nodes…"}
            </p>
          ) : (
            <label>
              Node (active revision){" "}
              <select
                value={nodeId}
                onChange={(event) => edit(setNodeChoice)(event.target.value === "" ? null : { key: nodeKey, node: event.target.value })}
              >
                <option value="">Choose a node</option>
                {nodes.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
          )
        ) : null}
        {operation === "retry_invocation" || operation === "redirect_plane" || operation === "reconcile_invocation" ? (
          <label className="control-form__wide">
            Reason (required){" "}
            <textarea rows={2} value={reason} onChange={(event) => edit(setReason)(event.target.value)} />
          </label>
        ) : null}
        {operation === "accept_revision" ? (
          <p>
            Pending candidate:{" "}
            {target.candidateId === null ? "none observed" : <code className="id">{target.candidateId}</code>}. Accepting may
            resume routing under Core's existing control.
          </p>
        ) : null}
        {operation === "reconcile_invocation" ? (
          <>
            <p className="notice notice--warn">
              Reconciling records your attestation that every writer of this invocation has stopped outside Millrace. A timeout, lost
              connection, terminal status or returned call is not that evidence.
            </p>
            <label>
              <input
                type="checkbox"
                checked={stopped}
                onChange={(event) => edit(setAttested)(event.target.checked ? attestation : null)}
              />{" "}
              I attest that every writer of this invocation has stopped
            </label>
          </>
        ) : null}
        {operation === "" ? null : <p className="muted">
          {versionNeeded
            ? target.version === null
              ? "Expected state version: not observed."
              : `Expected state version: ${target.version.value} (${target.runId === null ? "instance settings" : "run"}, observed ${target.version.observedAt}).`
            : "Stop requests carry no expected version."}
        </p>}
        <div className="toolbar">
          <button type="button" className="button--review" disabled={blocked !== null} onClick={doReview}>
            Review request
          </button>
          {blocked !== null ? <p className={operation === "" && disabled === null ? "muted review-block-reason" : "notice notice--warn review-block-reason"}>{blocked}</p> : null}
        </div>
        {message !== null ? (
          <p className="notice notice--error" role="alert">
            {message.text}
          </p>
        ) : null}
        {review !== null ? (
          <div className="review" aria-label="Reviewed request">
            <div className="review__heading"><p className="eyebrow">Ready for your decision</p><h3>{OPERATION_LABELS[review.envelope.operation]}</h3><p>{EFFECTS[review.envelope.operation]}</p></div>
            <dl className="facts review-target">
              <div><dt>Home</dt><dd><code>{target.home}</code></dd></div>
              <div><dt>Instance</dt><dd><code className="id">{review.envelope.instance_id}</code></dd></div>
              <div><dt>Run</dt><dd><code className="id">{review.envelope.run_id ?? "New run (not created yet)"}</code></dd></div>
              <div><dt>Plane</dt><dd><code>{review.envelope.plane_id ?? "Whole run / instance"}</code></dd></div>
            </dl>
            <dl className="facts">
              <div>
                <dt>Operation</dt>
                <dd>
                  <code>{review.envelope.operation}</code>
                </dd>
              </div>
              <div>
                <dt>Request ID</dt>
                <dd>
                  <code className="id">{review.envelope.request_id}</code>
                </dd>
              </div>
              <div>
                <dt>Expected state version</dt>
                <dd>{review.envelope.expected_state_version ?? "none (null)"}</dd>
              </div>
            </dl>
            <p className="muted">These exact bytes are sent, retained for replay and never changed:</p>
            <pre className="request-body">{review.body}</pre>
            <div className="toolbar">
              <button type="button" className="button--primary" disabled={sending || blocked !== null} onClick={() => void send(false)}>
                {sending ? "Sending…" : `Send ${OPERATION_LABELS[review.envelope.operation].toLowerCase()} request`}
              </button>
              {message?.unprotected ? (
                <button type="button" disabled={sending || blocked !== null} onClick={() => void send(true)}>
                  Send without reload protection
                </button>
              ) : null}
              <button
                type="button"
                onClick={() => saveText(reviewedText(target.home, review), `${review.envelope.request_id}-request.json`)}
              >
                Export reviewed request JSON
              </button>
              <button type="button" disabled={sending} onClick={() => setReview(null)}>
                Discard
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}

// ---- the request area ------------------------------------------------------------

export interface RequestFlags {
  /** history.state could not be written: reload loses replay bytes. */
  historyUnavailable: boolean;
  /** The merged set exceeds one history entry: sends wait for export/retire. */
  overCapacity: boolean;
  /** A history entry held request data that could not be restored. */
  invalidRestore: boolean;
}

export interface RequestActions {
  observe: (key: string) => void;
  replay: (key: string) => void;
  retire: (key: string) => void;
  openRun: (home: string, instanceId: string, runId: string) => void;
}

function deliveryText(entry: Entry): string {
  switch (entry.delivery) {
    case "sending":
      return "Sending…";
    case "unknown":
      return "Delivery unknown: the request may or may not have arrived.";
    case "restored":
      return "Restored from this tab's history (observe only until you replay).";
    default:
      return entry.answer !== null
        ? `The service answered with ${entry.answer.code} (HTTP ${entry.answer.status}).`
        : "The service answered with an observation.";
  }
}

function RequestItem({
  entry,
  busy,
  writable,
  actions,
}: {
  entry: Entry;
  busy: boolean;
  writable: boolean;
  actions: RequestActions;
}) {
  const [replaying, setReplaying] = useState(false);
  const [retiring, setRetiring] = useState(false);
  const envelope = envelopeOf(entry.intent);
  const key = keyOf(entry.intent);
  const observation = entry.observation;
  const receipt = observation?.coreReceipt ?? null;
  const status = receiptStatus(entry);
  const done = resolved(entry);
  const startRun = envelope.operation === "start_run" ? (entry.intent.binding?.runId ?? null) : null;
  const replayBlocked = entry.conflicts.length > 0 || entry.integrity !== null || !writable;
  return (
    <li className={`request${done ? " request--resolved" : ""}`}>
      <div className="request__head">
        <strong>{OPERATION_LABELS[envelope.operation]}</strong> <code className="id">{entry.intent.requestId}</code>{" "}
        <span className="state">{status ?? (done ? "answered" : "unresolved")}</span>
      </div>
      <dl className="facts">
        <div>
          <dt>Target</dt>
          <dd>
            <code>{entry.intent.home}</code> / <code className="id">{envelope.instance_id}</code>
            {envelope.run_id !== null ? (
              <>
                {" "}
                / <code className="id">{envelope.run_id}</code>
              </>
            ) : null}
            {envelope.plane_id !== null ? (
              <>
                {" "}
                / plane <code>{envelope.plane_id}</code>
              </>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Expected state version</dt>
          <dd>{envelope.expected_state_version ?? "none (null)"}</dd>
        </div>
        <div>
          <dt>Delivery</dt>
          <dd>{deliveryText(entry)}</dd>
        </div>
        <div>
          <dt>OS call phase</dt>
          <dd>{observation === null ? "not observed" : observation.phase}</dd>
        </div>
        <div>
          <dt>Core receipt</dt>
          <dd>
            {receipt === null
              ? "none observed"
              : `${display(receipt.status)}${receipt.code ? ` (${display(receipt.code)})` : ""}${
                  "state_version" in receipt ? ` · state version ${display(receipt.state_version)}` : ""
                }`}
          </dd>
        </div>
        <div>
          <dt>Holder join</dt>
          <dd>not attested (call/v1 cannot show a holder join)</dd>
        </div>
        {envelope.operation === "start_run" ? (
          <div>
            <dt>Started run</dt>
            <dd>
              {startRun === null ? (
                "not yet observed"
              ) : (
                <button type="button" className="link" onClick={() => actions.openRun(entry.intent.home, envelope.instance_id, startRun)}>
                  <code className="id">{startRun}</code>
                </button>
              )}
            </dd>
          </div>
        ) : null}
        {observation !== null ? (
          <div>
            <dt>Observed</dt>
            <dd>
              {observation.observedAt} ({observation.source === "core_receipt" ? "Core receipt" : "OS-owned call"})
            </dd>
          </div>
        ) : null}
      </dl>
      {entry.uncertain && !done && entry.delivery === "answered" ? (
        <p className="notice notice--warn">
          An earlier send of this request may have arrived. This answer describes only the latest attempt; the request stays unresolved
          until Core's receipt for it is observed or you retire it.
        </p>
      ) : null}
      {entry.readError !== null ? (
        <p className="notice notice--warn" role="status">
          {entry.readError.status === 404
            ? "No observation is available (HTTP 404). That is not proof the request did not run; it stays unknown."
            : `The latest observation read failed: ${entry.readError.code}${entry.readError.status ? ` (HTTP ${entry.readError.status})` : ""}.`}
        </p>
      ) : null}
      {entry.halted ? (
        <p className="notice notice--warn">
          Automatic observation stopped: the bounded receipt view refused this lookup. Observe again by hand, or use the headless CLI:{" "}
          <code>millrace-os router request --request-id {entry.intent.requestId}</code>.
        </p>
      ) : null}
      {entry.integrity !== null ? <p className="notice notice--error">Integrity: {entry.integrity} Replay is disabled.</p> : null}
      {entry.conflicts.length > 0 ? (
        <p className="notice notice--error">
          Integrity: another history entry holds different bytes under this request ID. Replay is disabled; export keeps both.
        </p>
      ) : null}
      <div className="toolbar">
        <button type="button" disabled={busy || entry.integrity !== null} onClick={() => actions.observe(key)}>
          Observe
        </button>
        <button type="button" disabled={busy || replayBlocked} onClick={() => setReplaying(true)}>
          Replay exact request…
        </button>
        <button type="button" onClick={() => saveText(exportText(entry), `${entry.intent.requestId}-request.json`)}>
          Export request JSON
        </button>
        <button type="button" disabled={busy} onClick={() => (done ? actions.retire(key) : setRetiring(true))}>
          Retire
        </button>
      </div>
      {replaying ? (
        <div className="review" aria-label={`Replay ${entry.intent.requestId}`}>
          <p className="muted">Replay sends these exact retained bytes again. The ID, target, payload and version do not change.</p>
          <pre className="request-body">{entry.intent.body}</pre>
          <div className="toolbar">
            <button
              type="button"
              disabled={busy || replayBlocked}
              onClick={() => {
                setReplaying(false);
                actions.replay(key);
              }}
            >
              Send exact bytes again
            </button>
            <button type="button" onClick={() => setReplaying(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {retiring ? (
        <div className="confirm">
          This request is unresolved. Retiring forgets it in this tab; export it first if you may need it.{" "}
          {/* Like Retire itself, the confirmation waits for a send or read in flight. */}
          <button type="button" disabled={busy} onClick={() => actions.retire(key)}>
            Retire unresolved request
          </button>{" "}
          <button type="button" onClick={() => setRetiring(false)}>
            Cancel
          </button>
        </div>
      ) : null}
    </li>
  );
}

export function RequestArea({
  entries,
  flags,
  busy,
  writable,
  actions,
  unreadable,
}: {
  entries: Entry[];
  flags: RequestFlags;
  busy: ReadonlySet<string>;
  writable: boolean;
  actions: RequestActions;
  /** Raw history data that could not be restored, kept for explicit export. */
  unreadable: string[];
}) {
  if (entries.length === 0 && !flags.historyUnavailable && !flags.invalidRestore) return null;
  return (
    <section className="region region--requests" aria-labelledby="requests-heading">
      <h2 id="requests-heading">Requests in this tab</h2>
      <p className="muted">
        An answer or receipt is not run completion, writer closure or a holder join. Run status, quiescence and writers stay in Overview.
      </p>
      {flags.overCapacity ? (
        <p className="notice notice--error" role="alert">
          These requests exceed one history entry's 20 requests or 64 KiB. New sends wait until you export and retire some; this page
          still keeps them all, but a reload would restore only what each history entry holds.
        </p>
      ) : null}
      {flags.historyUnavailable ? (
        <p className="notice notice--warn" role="alert">
          This tab's history could not hold the requests: a reload would lose their replay bytes. Export any you may need.
        </p>
      ) : null}
      {flags.invalidRestore ? (
        <p className="notice notice--warn">
          A history entry held request data that could not be read; it was not restored.{" "}
          {unreadable.length > 0 ? (
            <button type="button" onClick={() => saveText(unreadableText(unreadable), "unreadable-request-history.json")}>
              Export unreadable history data
            </button>
          ) : null}
        </p>
      ) : null}
      <ul className="request-list">
        {entries.map((entry) => (
          <RequestItem key={keyOf(entry.intent)} entry={entry} busy={busy.has(keyOf(entry.intent))} writable={writable} actions={actions} />
        ))}
      </ul>
    </section>
  );
}

// ---- monitoring status ------------------------------------------------------------

export type MonitorState =
  | { kind: "idle" }
  | { kind: "watching"; until: number; nextIn: number | null; failures: number }
  | { kind: "hidden" }
  | { kind: "stopped"; why: "window" | "errors" | "budget" | "terminal" };

export function MonitorStatus({ state, onContinue }: { state: MonitorState; onContinue: () => void }) {
  if (state.kind === "idle") return null;
  let message: string;
  let action: string | null = null;
  if (state.kind === "watching") {
    message = `Monitoring this target: automatic reads continue until ${new Date(state.until).toLocaleTimeString().replace(/\s/g, "\u00a0")}.`;
    if (state.nextIn !== null) {
      const failed = state.failures === 0 ? "" : ` after ${state.failures} failed read${state.failures === 1 ? "" : "s"}`;
      message += ` Next automatic read in ${Math.round(state.nextIn / 1000)} s${failed}.`;
    }
  } else if (state.kind === "hidden") {
    message = "Monitoring is paused while this tab is hidden.";
  } else if (state.why === "window") {
    message = "The five-minute observation window ended; nothing is read automatically.";
    action = "Continue monitoring";
  } else if (state.why === "errors") {
    message = "Automatic reads stopped after repeated failures.";
    action = "Retry";
  } else if (state.why === "budget") {
    message = "Automatic reads stopped: the bounded view refused this target. Use the headless CLI for a full read.";
    action = "Retry";
  } else {
    message = "The run is terminal and quiescent; fast refresh stopped. Holder closure is not established.";
  }
  return (
    <div className="monitor-region">
    <p className="visually-hidden" role="status">{state.kind === "watching" ? `Monitoring this target${state.failures > 0 ? ": read failures recorded" : ""}.` : state.kind === "hidden" ? "Monitoring paused while hidden." : message}</p>
    <p className="observation monitor">
      {message}{" "}
      {action !== null ? (
        <button type="button" onClick={onContinue}>
          {action}
        </button>
      ) : null}
    </p></div>
  );
}
