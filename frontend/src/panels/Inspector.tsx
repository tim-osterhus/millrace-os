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
import { FailureNote, type Paged } from "./Inventory.tsx";

export type Selection =
  | { kind: "node"; planeId: string; nodeId: string; revisionId: string | null }
  | { kind: "invocation"; invocationId: string };

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
            <dd>{key.endsWith("_id") ? <code className="id">{recorded(row, key)}</code> : recorded(row, key)}</dd>
          </div>
        ))}
      </dl>
      <h3>Writer</h3>
      <dl className="facts">
        <div>
          <dt>Original (completion)</dt>
          <dd>
            <span className="state">{recorded(writer, "original")}</span>
          </dd>
        </div>
        <div>
          <dt>Effective</dt>
          <dd>
            <span className="state">{recorded(writer, "effective")}</span>
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
              <dt>{key.replace("_", " ")}</dt>
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
  pageRows,
  onClear,
}: {
  selection: Selection | null;
  graph: CapturedGraph | null;
  invocation: Paged<CoreView> | null;
  /** Invocation rows already loaded by History, searched before an exact read. */
  pageRows: Json[];
  onClear: () => void;
}) {
  let body: ReactNode;
  if (selection === null) {
    body = <p className="muted">Select a node in Graph or a row in Planes, Status or History.</p>;
  } else if (selection.kind === "node") {
    body = <NodeFacts selection={selection} graph={graph} />;
  } else {
    const loaded = pageRows.find((row) => row.invocation_id === selection.invocationId) ?? null;
    const observation = invocation?.observation ?? null;
    if (loaded !== null) {
      body = <InvocationFacts row={loaded} />;
    } else if (observation === null) {
      body = <p className="muted" role="status">Reading invocation {selection.invocationId}…</p>;
    } else if (!observation.ok) {
      body = <FailureNote failure={observation} what="Invocation" />;
    } else {
      const row = record(observation.value.items[0]);
      // Render only the exact selected identity, never another invocation's row.
      body =
        row === null || row.invocation_id !== selection.invocationId ? (
          <p className="notice notice--warn">No invocation with this exact ID is recorded in this run.</p>
        ) : (
          <InvocationFacts row={row} />
        );
    }
  }
  return (
    <section className="region" aria-labelledby="inspector-heading">
      <div className="region__head">
        <h2 id="inspector-heading">Inspector</h2>
        {selection !== null ? (
          <button type="button" onClick={onClear}>
            Clear selection
          </button>
        ) : null}
      </div>
      {body}
    </section>
  );
}
