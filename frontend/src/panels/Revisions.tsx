// Startup, active routing and pending candidate revisions (lazy chunk).
// A candidate's diagnostic and changed paths never establish acceptance, and
// an invalid candidate shows its diagnostic instead of a plausible graph.
// Validating the current editable files is an explicit bounded read: it
// publishes nothing, captures no candidate and never replaces the Graph.

import { useState } from "react";

import {
  display,
  list,
  record,
  recorded,
  text,
  validateCurrent,
  type CoreView,
  type Json,
  type Observed,
  type Target,
} from "../api.ts";
import { FailureNote } from "./Inventory.tsx";

function Validation({ value }: { value: CoreView }) {
  const data = value.data;
  const candidate = record(data.candidate);
  const lists: [string, unknown][] = [
    ["Diagnostics", data.diagnostics],
    ["Diagnostic details", data.diagnostic_details],
    ["Warnings", data.warnings],
    ["Changed relative paths", candidate?.changed_paths],
  ];
  return (
    <>
      <dl className="facts">
        <div>
          <dt>Current files valid</dt>
          <dd>{recorded(data, "valid")}</dd>
        </div>
        <div>
          <dt>Revision they would form</dt>
          <dd>
            <code className="id">{recorded(data, "revision_id")}</code>
          </dd>
        </div>
        <div>
          <dt>Candidate diagnostic</dt>
          <dd>{candidate === null ? "no candidate" : recorded(candidate, "diagnostic")}</dd>
        </div>
      </dl>
      {lists.map(([label, raw]) => {
        const items = list(raw);
        return items === null || items.length === 0 ? null : (
          <div key={label}>
            <h4>{label}</h4>
            <ul className="path-list">
              {items.map((item, index) => (
                <li key={`${display(item)}|${index}`}>
                  <code>{display(item)}</code>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </>
  );
}

export default function Revisions({
  summary,
  shownRevision,
  onShowGraph,
  target,
  canValidate,
}: {
  summary: Json;
  shownRevision: string | null;
  onShowGraph: (revisionId: string) => void;
  target: Target;
  canValidate: boolean;
}) {
  const [validation, setValidation] = useState<Observed<CoreView> | "reading" | null>(null);
  const validate = async () => {
    setValidation("reading");
    setValidation(await validateCurrent(target));
  };
  const candidate = record(summary.pending_candidate);
  const candidateRevision = text(candidate?.revision_id);
  const diagnostic = candidate === null ? null : candidate.diagnostic;
  const changed = list(candidate?.changed_paths);
  const rows: [string, string | null][] = [
    ["Startup", text(summary.startup_revision_id)],
    ["Active routing", text(summary.active_revision_id)],
  ];
  return (
    <section className="region" aria-labelledby="revisions-heading">
      <h2 id="revisions-heading">Revisions</h2>
      <table>
        <caption className="visually-hidden">Recorded revisions of this run</caption>
        <thead>
          <tr>
            <th scope="col">Role</th>
            <th scope="col">Revision</th>
            <th scope="col">Graph</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([role, id]) => (
            <tr key={role}>
              <th scope="row">{role}</th>
              <td>
                <code className="id">{id ?? "unknown"}</code>
              </td>
              <td>
                {id === null ? (
                  "—"
                ) : (
                  <button type="button" aria-pressed={shownRevision === id} onClick={() => onShowGraph(id)}>
                    {shownRevision === id ? "Shown in Graph" : "Show in Graph"}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Pending candidate</h3>
      {candidate === null ? (
        <p className="muted">{recorded(summary, "pending_candidate") === "none" ? "No pending candidate." : "Unknown."}</p>
      ) : (
        <>
          <dl className="facts">
            <div>
              <dt>Candidate</dt>
              <dd>
                <code className="id">{recorded(candidate, "candidate_id")}</code>
              </dd>
            </div>
            <div>
              <dt>Revision</dt>
              <dd>
                <code className="id">{recorded(candidate, "revision_id")}</code>
              </dd>
            </div>
            <div>
              <dt>Diagnostic</dt>
              <dd>{recorded(candidate, "diagnostic")}</dd>
            </div>
            <div>
              <dt>Withdrawn</dt>
              <dd>{recorded(candidate, "withdrawn")}</dd>
            </div>
            <div>
              <dt>Supersedes</dt>
              <dd>{recorded(candidate, "supersedes")}</dd>
            </div>
            <div>
              <dt>Submitted at version</dt>
              <dd>{recorded(candidate, "version")}</dd>
            </div>
            <div>
              <dt>Submitting request</dt>
              <dd>
                <code className="id">{recorded(candidate, "request_id")}</code>
              </dd>
            </div>
          </dl>
          <p className="muted">A pending candidate is not accepted until an explicit accept request applies.</p>
          {diagnostic === null && candidateRevision !== null ? (
            <button
              type="button"
              aria-pressed={shownRevision === candidateRevision}
              onClick={() => onShowGraph(candidateRevision)}
            >
              {shownRevision === candidateRevision ? "Candidate shown in Graph" : "Show candidate in Graph"}
            </button>
          ) : diagnostic !== null ? (
            <p className="notice notice--warn">
              The candidate has diagnostic <code>{display(diagnostic)}</code>; no graph is shown for it.
            </p>
          ) : null}
          <h4>Changed relative paths</h4>
          {changed === null ? (
            <p className="muted">Unknown.</p>
          ) : changed.length === 0 ? (
            <p className="muted">None recorded.</p>
          ) : (
            <ul className="path-list">
              {changed.map((path, index) => (
                <li key={`${display(path)}|${index}`}>
                  <code>{display(path)}</code>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      <h3>Current editable files</h3>
      <p className="muted">
        Validation reads the files as they are now. It publishes nothing and does not change the captured Graph, the active routing
        revision or a pending candidate. Submit and accept stay explicit requests.
      </p>
      <button type="button" disabled={!canValidate || validation === "reading"} onClick={() => void validate()}>
        Validate current editable files
      </button>
      {validation === null ? null : validation === "reading" ? (
        <p className="muted" role="status">
          Validating current files…
        </p>
      ) : !validation.ok ? (
        <FailureNote failure={validation} what="Validation" />
      ) : (
        <Validation value={validation.value} />
      )}
    </section>
  );
}
