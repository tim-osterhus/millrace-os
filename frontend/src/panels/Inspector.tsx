// Safe projected facts for one exact selected identity: a captured graph node
// or an invocation. No prompts, native output or editable files are shown;
// Core's projection already withholds them. The original completion writer,
// the effective writer and any reconciliation stay separate facts.

import type { ReactNode } from "react";

import {
  display,
  record,
  recorded,
  type CapturedAction,
  type CapturedGraph,
  type CoreView,
  type Json,
} from "../api.ts";
import { FailureNote, stateClass, type Paged } from "./Inventory.tsx";

export type Selection =
  | { kind: "node"; planeId: string; nodeId: string; revisionId: string | null }
  | { kind: "invocation"; invocationId: string };

export interface InvocationSnapshot {
  row: Json;
  observedAt: string;
  provenance: "History snapshot" | "Invocation read";
}

function action(value: CapturedAction | null): string {
  if (value === null || value.kind === null) return "none";
  const target = value.target === null ? "" : ` ${value.target}`;
  if (value.kind === "pause") return `pause ${value.reason ?? "unknown"}${target ? ` (resume${target})` : ""}`;
  if (value.kind === "retry") return `retry ≤ ${value.limit ?? "?"}, then ${action(value.exhausted)}`;
  return `${value.kind}${target}`;
}

function NodeFacts({ selection, graph }: { selection: Extract<Selection, { kind: "node" }>; graph: CapturedGraph | null }) {
  const plane = graph?.planes.find((item) => item.planeId === selection.planeId) ?? null;
  const node = plane?.nodes.find((item) => item.id === selection.nodeId) ?? null;
  const seams = graph?.seams.filter(
    (seam) =>
      (seam.producerPlane === selection.planeId && seam.producerNode === selection.nodeId) ||
      (seam.consumerPlane === selection.planeId && seam.consumerNode === selection.nodeId),
  );
  return (
    <>
      <dl className="facts">
        <div>
          <dt>Node</dt>
          <dd>
            <code>{selection.nodeId}</code>
          </dd>
        </div>
        <div>
          <dt>Plane</dt>
          <dd>
            <code>{selection.planeId}</code>
          </dd>
        </div>
        <div>
          <dt>Captured revision</dt>
          <dd>
            <code className="id">{selection.revisionId ?? "unknown"}</code>
          </dd>
        </div>
      </dl>
      {graph === null ? (
        <p className="muted">Open Graph to read this revision's captured node definition.</p>
      ) : node === null ? (
        <p className="notice notice--warn">This node is not in the selected captured revision.</p>
      ) : (
        <>
          <dl className="facts">
            <div>
              <dt>Start node</dt>
              <dd>{plane?.start === node.id ? "yes" : "no"}</dd>
            </div>
            <div>
              <dt>Binding</dt>
              <dd>{node.binding ?? "default"}</dd>
            </div>
            <div>
              <dt>Timeout</dt>
              <dd>{node.timeoutSeconds === null ? "default" : `${node.timeoutSeconds} s`}</dd>
            </div>
            <div>
              <dt>Requires terminal</dt>
              <dd>
                {node.requiresTerminal.length === 0
                  ? "none"
                  : node.requiresTerminal.map((item) => `${item.planeId} ${item.status}`).join(", ")}
              </dd>
            </div>
          </dl>
          <table>
            <caption>Captured routes</caption>
            <thead>
              <tr>
                <th scope="col">Outcome</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {node.routes.map(([outcome, value]) => (
                <tr key={outcome}>
                  <td>{outcome}</td>
                  <td>{action(value)}</td>
                </tr>
              ))}
              <tr>
                <td>error</td>
                <td>{action(node.onError)}</td>
              </tr>
              <tr>
                <td>unknown</td>
                <td>{action(node.onUnknown)}</td>
              </tr>
            </tbody>
          </table>
          {seams !== undefined && seams.length > 0 ? (
            <ul className="fact-list">
              {seams.map((seam) => (
                <li key={seam.id}>
                  Seam <code>{seam.id}</code>: {seam.producerPlane}/{seam.producerNode} on {seam.outcome} →{" "}
                  {seam.consumerPlane}/{seam.consumerNode}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
    </>
  );
}

/** Contract epoch seconds, with raw values retained and no guessed string parsing. */
function EpochTime({ value }: { value: unknown }) {
  const date = typeof value === "number" && Number.isFinite(value) ? new Date(value * 1000) : null;
  const stamp = date !== null && Number.isFinite(date.getTime()) ? date.toISOString() : null;
  return stamp === null ? <>{value === null ? "none" : display(value)}</> : <>
    <time dateTime={stamp}>{stamp.replace("T", " ").replace("Z", " UTC")}</time>
    <details className="epoch-raw"><summary>Epoch seconds</summary><code>{display(value)}</code></details>
  </>;
}

function InvocationFacts({ row }: { row: Json }) {
  const writer = record(row.writer);
  const completion = record(row.completion);
  const reconciliation = record(row.reconciliation);
  const unavailable = record(row.unavailable_reason);
  return (
    <>
      <dl className="facts">
        {(
          [
            ["invocation_id", "Invocation"],
            ["attempt", "Attempt"],
            ["plane_id", "Plane"],
            ["node_id", "Node"],
            ["status", "Status"],
            ["producing_revision_id", "Producing revision"],
            ["created_at", "Created"],
            ["deadline", "Deadline"],
            ["run_deadline", "Run deadline"],
            ["started_version", "Started at version"],
            ["completed_version", "Completed at version"],
            ["parsed_version", "Parsed at version"],
            ["result_contract", "Result contract"],
            ["uncertainty_diagnostic", "Uncertainty diagnostic"],
          ] as const
        ).map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{key === "created_at" || key === "deadline" || key === "run_deadline" ? <EpochTime value={row[key]} /> : key.endsWith("_id") ? <code className="id">{recorded(row, key)}</code> : recorded(row, key)}</dd>
          </div>
        ))}
      </dl>
      <h3>Writer</h3>
      <dl className="facts">
        <div>
          <dt>Original (completion)</dt>
          <dd>
            <span className={stateClass(writer?.original)}>{recorded(writer, "original")}</span>
          </dd>
        </div>
        <div>
          <dt>Effective</dt>
          <dd>
            <span className={stateClass(writer?.effective)}>{recorded(writer, "effective")}</span>
          </dd>
        </div>
      </dl>
      <h3>Completion</h3>
      {completion === null ? (
        <p className="muted">No completion is recorded.</p>
      ) : (
        <dl className="facts">
          {(["transport", "exit_code", "classification", "outcome", "diagnostic"] as const).map((key) => (
            <div key={key}>
              <dt>{key.replace("_", " ").replace(/^./, (letter) => letter.toUpperCase())}</dt>
              <dd>{recorded(completion, key)}</dd>
            </div>
          ))}
        </dl>
      )}
      <h3>Reconciliation</h3>
      {reconciliation === null ? (
        <p className="muted">No operator reconciliation is recorded.</p>
      ) : (
        <dl className="facts">
          <div>
            <dt>Request</dt>
            <dd>
              <code className="id">{recorded(reconciliation, "request_id")}</code>
            </dd>
          </div>
          <div>
            <dt>Version</dt>
            <dd>{recorded(reconciliation, "version")}</dd>
          </div>
          <div>
            <dt>Attested writer</dt>
            <dd>{recorded(reconciliation, "writer")}</dd>
          </div>
        </dl>
      )}
      {unavailable !== null && Object.keys(unavailable).length > 0 ? (
        <p className="muted">
          Withheld fields: {Object.entries(unavailable).map(([key, reason]) => `${key} (${display(reason)})`).join(", ")}
        </p>
      ) : null}
    </>
  );
}

export function Inspector({
  selection,
  graph,
  invocation,
  snapshot,
  onClear,
}: {
  selection: Selection | null;
  graph: CapturedGraph | null;
  invocation: Paged<CoreView> | null;
  /** The exact selected snapshot keeps its successful read time and provenance. */
  snapshot: InvocationSnapshot | null;
  onClear: () => void;
}) {
  const selectedInvocation = selection?.kind === "invocation" && snapshot?.row.invocation_id === selection.invocationId ? snapshot : null;
  let body: ReactNode;
  if (selection === null) {
    body = <div className="inspector-empty"><p className="eyebrow">Nothing selected</p><h3>Inspect one exact object</h3><p className="muted">Select a node in Graph, or a plane’s node or an invocation in Overview or Activity. Its captured definition or recorded invocation appears here.</p></div>;
  } else if (selection.kind === "node") {
    body = <NodeFacts selection={selection} graph={graph} />;
  } else {
    const observation = invocation?.observation ?? null;
    const exact = observation?.ok ? record(observation.value.items[0]) : null;
    body = <>
      {invocation?.loading ? <p className="muted" role="status">Reading selected invocation…</p> : null}
      {observation !== null && !observation.ok ? <>
        <FailureNote failure={observation} what="Invocation" />
        <p className="observation">Read failed at <time dateTime={new Date(observation.receivedAt).toISOString()}>{new Date(observation.receivedAt).toISOString()}</time>. {selectedInvocation === null ? "No invocation snapshot is available." : "The earlier snapshot below is retained; it is not a new observation."}</p>
      </> : null}
      {observation?.ok && (exact === null || exact.invocation_id !== selection.invocationId) ? <p className="notice notice--warn">No invocation with this exact ID was returned. {selectedInvocation === null ? "" : "The earlier snapshot below is retained."}</p> : null}
      {selectedInvocation === null ? observation === null && !invocation?.loading ? <p className="muted">No invocation snapshot observed.</p> : null : <>
        <p className="invocation-observation observation">{selectedInvocation.provenance} · read at <time dateTime={selectedInvocation.observedAt}>{selectedInvocation.observedAt}</time>. The page-header Refresh explicitly re-reads this invocation; summary monitoring does not update this snapshot.</p>
        <InvocationFacts row={selectedInvocation.row} />
      </>}
    </>;
  }
  return (
    <section className="region inspector-panel" aria-labelledby="inspector-heading">
      <div className="region__head">
        <h2 id="inspector-heading" tabIndex={-1}>Inspector</h2>
        {selection !== null ? (
          <button type="button" onClick={onClear}>
            Clear selection
          </button>
        ) : null}
      </div>
      {selection === null ? null : <div className="inspector-identity">
        <strong>{selection.kind === "node" ? "Selected captured node" : "Selected recorded invocation"}</strong>
        {selection.kind === "node" ? <code>{selection.planeId} / {selection.nodeId}</code> : <>
          <code>{selection.invocationId}</code>
          {selectedInvocation === null ? null : <span>Plane <code>{recorded(selectedInvocation.row, "plane_id")}</code> · node <code>{recorded(selectedInvocation.row, "node_id")}</code></span>}
        </>}
      </div>}
      {body}
    </section>
  );
}
