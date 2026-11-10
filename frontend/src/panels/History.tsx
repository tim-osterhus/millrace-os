// One paged history section at a time (lazy chunk). Nothing loads until a
// section is chosen; each page is one bounded read of at most 50 rows.

import { display, record, recorded, text, type CoreView, type HistorySection, type Json } from "../api.ts";
import { stateClass, FailureNote, Pager, type Paged } from "./Inventory.tsx";

const SECTIONS: [HistorySection, string][] = [
  ["invocations", "Invocations"],
  ["transitions", "Transitions and moves"],
  ["controls", "Control receipts"],
];

function InvocationRows({ rows, onSelect }: { rows: Json[]; onSelect: (id: string) => void }) {
  return (
    <table>
      <caption>Invocations, newest attempt first</caption>
      <thead>
        <tr>
          <th scope="col">Attempt</th>
          <th scope="col">Invocation</th>
          <th scope="col">Plane / node</th>
          <th scope="col">Status</th>
          <th scope="col">Writer (original / effective)</th>
          <th scope="col">Outcome</th>
          <th scope="col">Producing revision</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => {
          const id = text(row.invocation_id);
          const writer = record(row.writer);
          return (
            <tr key={id ?? index}>
              <td>{recorded(row, "attempt")}</td>
              <th scope="row">
                {id === null ? (
                  "unknown"
                ) : (
                  <button type="button" className="link" onClick={() => onSelect(id)}>
                    <code className="id">{id}</code>
                  </button>
                )}
              </th>
              <td>
                <code>{recorded(row, "plane_id")}</code> / <code>{recorded(row, "node_id")}</code>
              </td>
              <td>
                <span className={stateClass(row.status)}>{recorded(row, "status")}</span>
              </td>
              <td>
                <span className="state">{recorded(writer, "original")}</span> /{" "}
                <span className="state">{recorded(writer, "effective")}</span>
              </td>
              <td>{row.completion === null ? "no completion" : recorded(record(row.completion), "outcome")}</td>
              <td>
                <code className="id">{recorded(row, "producing_revision_id")}</code>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function EventRows({ rows }: { rows: Json[] }) {
  return (
    <table>
      <caption>Transitions and moves, newest version first</caption>
      <thead>
        <tr>
          <th scope="col">Version</th>
          <th scope="col">Plane</th>
          <th scope="col">Kind</th>
          <th scope="col">From</th>
          <th scope="col">Outcome or move</th>
          <th scope="col">Action → target</th>
          <th scope="col">Routing revision</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={`${display(row.event_kind)}|${display(row.event_id)}|${index}`}>
            <td>{recorded(row, "version")}</td>
            <td>
              <code>{recorded(row, "plane_id")}</code>
            </td>
            <td>{recorded(row, "event_kind")}</td>
            <td>{row.event_kind === "move" ? "operator request" : <code>{recorded(row, "node_id")}</code>}</td>
            <td>{row.event_kind === "move" ? recorded(row, "move_kind") : recorded(row, "outcome")}</td>
            <td>
              {row.event_kind === "move" ? "move" : recorded(row, "action")} → <code>{recorded(row, "target")}</code>
            </td>
            <td>
              <code className="id">{recorded(row, "routing_revision_id")}</code>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ControlRows({ rows }: { rows: Json[] }) {
  // Receipts are rendered as their recorded fields; nothing is translated.
  return (
    <table>
      <caption>Control receipts, newest first</caption>
      <thead>
        <tr>
          <th scope="col">Request</th>
          <th scope="col">Operation</th>
          <th scope="col">Receipt status</th>
          <th scope="col">Code</th>
          <th scope="col">Plane</th>
          <th scope="col">State version</th>
          <th scope="col">Action</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={text(row.request_id) ?? index}>
            <th scope="row">
              <code className="id">{recorded(row, "request_id")}</code>
            </th>
            <td>{recorded(row, "operation")}</td>
            <td>
              <span className={stateClass(row.status)}>{recorded(row, "status")}</span>
            </td>
            <td>{"code" in row ? recorded(row, "code") : "none"}</td>
            <td>{recorded(row, "plane_id")}</td>
            <td>{recorded(row, "state_version")}</td>
            <td>{"action" in row ? recorded(row, "action") : "none"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function History({
  section,
  page,
  onSection,
  onPage,
  onSelectInvocation,
}: {
  section: HistorySection | null;
  page: Paged<CoreView>;
  onSection: (section: HistorySection) => void;
  onPage: (direction: 1 | -1) => void;
  onSelectInvocation: (invocationId: string) => void;
}) {
  const observation = page.observation;
  const rows = observation?.ok ? observation.value.items.map((item) => record(item) ?? {}) : [];
  return (
    <section className="region history-panel" aria-labelledby="history-heading">
      <h2 id="history-heading" tabIndex={-1}>History</h2>
      <div className="toolbar" role="group" aria-label="History section">
        {SECTIONS.map(([value, label]) => (
          <button key={value} type="button" aria-pressed={section === value} onClick={() => onSection(value)}>
            {label}
          </button>
        ))}
      </div>
      {section === null ? (
        <p className="muted">Choose a section to read one page of up to 50 rows.</p>
      ) : observation === null ? (
        <p className="muted" role="status">
          {page.loading ? "Reading history…" : "Not read yet."}
        </p>
      ) : !observation.ok ? (
        <FailureNote failure={observation} what="History page" />
      ) : rows.length === 0 && page.page === 0 ? (
        <p className="empty">Nothing is recorded in this section yet.</p>
      ) : (
        <><p className="table-scroll-hint">Scroll horizontally when needed for all recorded columns.</p><div className="table-scroll">
          {section === "invocations" ? (
            <InvocationRows rows={rows} onSelect={onSelectInvocation} />
          ) : section === "transitions" ? (
            <EventRows rows={rows} />
          ) : (
            <ControlRows rows={rows} />
          )}
        </div></>
      )}
      {section !== null && observation?.ok ? (
        <Pager
          label="History"
          page={page.page}
          hasNext={observation.value.nextCursor !== null}
          loading={page.loading}
          onPage={onPage}
        />
      ) : null}
    </section>
  );
}
