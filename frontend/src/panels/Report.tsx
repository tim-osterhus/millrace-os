// Generic workflow outputs for one run (lazy chunk; nothing loads until opened).
//
// Reports are workflow-produced evidence, displayed beside, never instead of,
// execution facts: a report proves no task success, writer closure, ownership
// or join. Only `millrace-report-display/v1` renders, through fixed escaped
// text, table and link elements. No HTML, Markdown or producer component runs.

import { useEffect, useRef, useState } from "react";

import { classifySource } from "../links.ts";
import {
  output,
  outputs,
  saveText,
  targetHref,
  type Failure,
  type DemoMode,
  type Observed,
  type OutputItem,
  type OutputRead,
  type OutputsPage,
  type Target,
} from "../api.ts";
import { FailureNote, Pager, type Paged } from "./Inventory.tsx";

const REPORT_FORMAT = "millrace-report-display/v1";

interface ReportDisplay {
  title: string;
  paragraphs: string[];
  facts: { label: string; value: string }[];
  tables: { caption: string; columns: string[]; rows: string[][] }[];
  sources: { label: string; url: string }[];
}

const isText = (value: unknown): value is string => typeof value === "string";

function exactObject(value: unknown, keys: string[]): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => own.includes(key)) ? (value as Record<string, unknown>) : null;
}

/** The fixed display shape; anything else is refused, not partially shown. */
function reportDisplay(data: unknown): ReportDisplay | null {
  const report = exactObject(data, ["title", "paragraphs", "facts", "tables", "sources"]);
  if (report === null || !isText(report.title)) return null;
  const { paragraphs, facts, tables, sources } = report;
  if (!Array.isArray(paragraphs) || !paragraphs.every(isText)) return null;
  if (!Array.isArray(facts) || !Array.isArray(tables) || !Array.isArray(sources)) return null;
  const parsedFacts = facts.map((item) => exactObject(item, ["label", "value"]));
  if (parsedFacts.some((item) => item === null || !isText(item.label) || !isText(item.value))) return null;
  const parsedTables = tables.map((item) => exactObject(item, ["caption", "columns", "rows"]));
  for (const table of parsedTables) {
    if (table === null || !isText(table.caption) || !Array.isArray(table.columns) || !table.columns.every(isText)) {
      return null;
    }
    const width = table.columns.length;
    if (!Array.isArray(table.rows) || !table.rows.every((row) => Array.isArray(row) && row.length === width && row.every(isText))) {
      return null;
    }
  }
  const parsedSources = sources.map((item) => exactObject(item, ["label", "url"]));
  if (parsedSources.some((item) => item === null || !isText(item.label) || !isText(item.url))) return null;
  return {
    title: report.title,
    paragraphs: paragraphs as string[],
    facts: parsedFacts as { label: string; value: string }[],
    tables: parsedTables as { caption: string; columns: string[]; rows: string[][] }[],
    sources: parsedSources as { label: string; url: string }[],
  };
}

function RenderedReport({ report }: { report: ReportDisplay }) {
  return (
    <div className="report">
      <h3>{report.title}</h3>
      {report.paragraphs.map((paragraph, index) => (
        <p key={index}>{paragraph}</p>
      ))}
      {report.facts.length > 0 ? (
        <dl className="facts">
          {report.facts.map((fact, index) => (
            <div key={index}>
              <dt>{fact.label}</dt>
              <dd>{fact.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {report.tables.map((table, index) => (
        <div key={index} className="table-scroll">
          <table>
            <caption>{table.caption}</caption>
            <thead>
              <tr>
                {table.columns.map((column, columnIndex) => (
                  <th key={columnIndex} scope="col">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {report.sources.length > 0 ? (
        <>
          <h4>Sources</h4>
          <ul className="sources">
            {report.sources.map((source, index) => {
              const link = classifySource(source.url);
              return (
                <li key={index}>
                  {link.kind === "link" ? (
                    <>
                      <a href={link.href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
                        {source.label}
                      </a>{" "}
                      <span className="muted">({link.host})</span>
                    </>
                  ) : (
                    <>
                      {source.label}: <code className="path">{source.url}</code>{" "}
                      <span className="muted">(not a link: {link.reason})</span>
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
    </div>
  );
}

function Evidence({ value }: { value: OutputRead }) {
  return (
    <dl className="facts facts--evidence">
      <div>
        <dt>Producer (claimed)</dt>
        <dd>
          <code>{value.producer.id}</code> {value.producer.version}
        </dd>
      </div>
      <div>
        <dt>Producing invocation</dt>
        <dd>
          <code>{value.invocation.planeId}</code> / <code>{value.invocation.nodeId}</code> /{" "}
          <code className="id">{value.invocation.invocationId}</code>
        </dd>
      </div>
      <div>
        <dt>Producing revision</dt>
        <dd>
          <code className="id">{value.invocation.producingRevisionId}</code>
        </dd>
      </div>
      <div>
        <dt>Published</dt>
        <dd>{value.publishedAt}</dd>
      </div>
      <div>
        <dt>Record SHA-256</dt>
        <dd>
          <code className="id">{value.reference.recordSha256}</code>
        </dd>
      </div>
      <div>
        <dt>Representation SHA-256</dt>
        <dd>
          <code className="id">{value.representation.sha256}</code>
        </dd>
      </div>
    </dl>
  );
}

/**
 * The outputs listing with the exact target and page it was read for. `stale`
 * is a failed re-read of that same page: its last successful listing stays,
 * labelled, until a later read succeeds.
 */
type Listing = Paged<OutputsPage> & { key: string | null; stale: Failure | null };

export default function Report({ target, demo, reportContext }: { target: Target; demo: DemoMode | null; reportContext: DemoMode | null }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [listing, setListing] = useState<Listing>({ observation: null, loading: true, page: 0, key: null, stale: null });
  // Bumped only by the operator's Refresh outputs: nothing re-reads on a timer.
  const [listingRead, setListingRead] = useState(0);
  const [selected, setSelected] = useState<OutputItem | null>(null);
  // A report read is kept with the exact output it was issued for.
  const [read, setRead] = useState<{ key: string; result: Observed<OutputRead> } | null>(null);
  const chosen = useRef<string | null>(null);
  const [download, setDownload] = useState<{ reading: boolean; failure: Failure | null }>({ reading: false, failure: null });
  const cursor = cursors[cursors.length - 1];
  const listingKey = `${targetHref(target)}|${cursors.length - 1}|${cursor ?? ""}`;
  const contextStale = demo === null || demo.kind === "pending" || demo.kind === "unknown";
  const context = contextStale ? reportContext : demo;
  const owned = context?.kind === "demo";
  const matching = owned && context.home === target.home && context.instanceId === target.instance && context.runId === target.run;
  const accepted = matching ? context.acceptedOutputRefs : [];
  const pinsKey = JSON.stringify(accepted);

  const checkPinned = (result: Observed<OutputRead>, item: OutputItem, representation: "report" | "json"): Observed<OutputRead> => {
    if (!result.ok || !owned) return result;
    const pin = accepted.find((entry) => entry.reference.outputId === item.outputId && entry.reference.recordSha256 === item.recordSha256);
    const expected = representation === "report" ? pin?.reportSha256 : pin?.jsonSha256;
    if (pin !== undefined && expected === result.value.representation.sha256 &&
        result.value.reference.instanceId === pin.reference.instanceId && result.value.reference.runId === pin.reference.runId) return result;
    return { ok: false, status: 0, code: "demo_output_mismatch", summary: "The output does not match the coordinator's accepted reference and digest.", retryable: false, receivedAt: Date.now() };
  };

  // Context pins select the main report directly, even before discovery lists
  // it. A conflicting discovery record never replaces this selection.
  useEffect(() => {
    if (context?.kind === "ordinary") return;
    const pin = accepted[0];
    if (pin === undefined) {
      chosen.current = null;
      setSelected(null);
      setRead(null);
      return;
    }
    const item: OutputItem = { outputId: pin.reference.outputId, recordSha256: pin.reference.recordSha256,
      available: true, reason: null, producer: null, label: null, publishedAt: null, invocation: null,
      report: { format: REPORT_FORMAT, sha256: pin.reportSha256 },
      json: pin.jsonSha256 === null ? null : { format: "unknown", sha256: pin.jsonSha256 } };
    void choose(item);
    // The serialized accepted refs are the immutable selection identity.
  }, [pinsKey, target.home, target.instance, target.run, context?.kind]);

  // One listing read per target, page and explicit Refresh outputs. A reply is
  // shown only while its own read is still the current one, so a superseded
  // target, page or refresh never replaces what is shown. The selected output
  // and its report read are separate state and are left untouched.
  useEffect(() => {
    let current = true;
    setListing((previous) => ({ ...previous, loading: true, stale: previous.key === listingKey ? previous.stale : null }));
    void outputs(target, cursor).then((observation) => {
      if (!current) return;
      setListing((previous) =>
        !observation.ok && previous.observation?.ok === true && previous.key === listingKey
          ? { ...previous, loading: false, stale: observation }
          : { observation, loading: false, page: cursors.length - 1, key: listingKey, stale: null },
      );
    });
    return () => {
      current = false;
    };
  }, [target, cursor, cursors.length, listingKey, listingRead]);

  /** Re-read this page of the listing; a read already in flight is not repeated. */
  const refreshOutputs = () => {
    if (!listing.loading) setListingRead((count) => count + 1);
  };

  const outputKey = (item: OutputItem) => `${item.outputId}|${item.recordSha256}`;

  const choose = async (item: OutputItem) => {
    if (context?.kind !== "ordinary" && (!matching || !accepted.some((pin) => pin.reference.outputId === item.outputId && pin.reference.recordSha256 === item.recordSha256))) return;
    const key = outputKey(item);
    chosen.current = key;
    setSelected(item);
    setRead(null);
    setDownload({ reading: false, failure: null });
    if (!item.available || item.recordSha256 === null) return;
    const result = checkPinned(await output(target, item.outputId, item.recordSha256, "report"), item, "report");
    // Selecting A then B: A's late reply never replaces B's.
    if (chosen.current === key) setRead({ key, result });
  };

  const saveEnvelope = async (item: OutputItem, representation: "report" | "json") => {
    if (item.recordSha256 === null) return;
    setDownload({ reading: true, failure: null });
    const result = checkPinned(await output(target, item.outputId, item.recordSha256, representation), item, representation);
    // The file is named for the output it was requested for; a failure is shown
    // only while that output is still the selected one.
    if (result.ok) saveText(result.text, `${item.outputId}-${representation}-api-response.json`);
    const failure = !result.ok && chosen.current === outputKey(item) ? result : null;
    setDownload({ reading: false, failure });
  };

  const page = (direction: 1 | -1) => {
    const next = listing.observation?.ok ? listing.observation.value.nextCursor : null;
    if (direction === 1 && next !== null) setCursors([...cursors, next]);
    if (direction === -1 && cursors.length > 1) setCursors(cursors.slice(0, -1));
  };

  const observation = listing.observation;
  const report = read !== null && selected !== null && read.key === outputKey(selected) ? read.result : null;
  const rendered = report?.ok
    ? report.value.representation.format === REPORT_FORMAT
      ? reportDisplay(report.value.representation.data)
      : null
    : null;
  return (
    <section className="report-panel" aria-labelledby="reports-heading">
      <h2 id="reports-heading" tabIndex={-1}>Reports</h2>
      <p className="muted">
        Workflow-produced evidence. A report proves no task success, writer closure, ownership or join; read execution
        facts in Overview and Activity.
      </p>
      {owned && !matching ? <p className="notice notice--warn">This run is inspect-only; it is not the terminal's bound demo target.</p> : null}
      {matching && context.outputError !== null ? <p className="notice notice--warn">The coordinator reported an output error: <code>{context.outputError}</code>.</p> : null}
      {matching && accepted.length === 0 ? <p className="muted">Waiting for a coordinator-accepted report reference.</p> : null}
      <div className="toolbar report-listing-actions">
        <button type="button" onClick={refreshOutputs}>
          Refresh outputs
        </button>
        {listing.loading && observation !== null ? (
          <span className="muted" role="status">
            Reading published outputs…
          </span>
        ) : null}
      </div>
      {listing.stale !== null && observation?.ok === true ? (
        <>
          <FailureNote failure={listing.stale} what="Published outputs" />
          <p className="observation">
            The listing below is the last successful read, at <time dateTime={observation.observedAt}>{observation.observedAt}</time>; it is
            not a new observation.
          </p>
        </>
      ) : null}
      {observation === null ? (
        <p className="muted" role="status">
          Reading published outputs…
        </p>
      ) : !observation.ok ? (
        <FailureNote failure={observation} what="Published outputs" />
      ) : observation.value.items.length === 0 && listing.page === 0 ? (
        <p className="empty">No outputs are published for this run.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <caption className="visually-hidden">Published outputs, by output ID</caption>
            <thead>
              <tr>
                <th scope="col">Output</th>
                <th scope="col">Label</th>
                <th scope="col">Producer (claimed)</th>
                <th scope="col">Node</th>
                <th scope="col">Published</th>
                <th scope="col">Availability</th>
              </tr>
            </thead>
            <tbody>
              {observation.value.items.map((item) => (
                <tr key={item.outputId} aria-current={selected !== null && outputKey(selected) === outputKey(item) ? "true" : undefined}>
                  <th scope="row">
                    <button type="button" className="link" disabled={context?.kind !== "ordinary" && !accepted.some((pin) => pin.reference.outputId === item.outputId && pin.reference.recordSha256 === item.recordSha256)} onClick={() => void choose(item)}>
                      <code>{item.outputId}</code>
                    </button>
                  </th>
                  <td>{item.label ?? "unknown"}</td>
                  <td>{item.producer ? `${item.producer.id} ${item.producer.version}` : "unknown"}</td>
                  <td>{item.invocation ? `${item.invocation.planeId} / ${item.invocation.nodeId}` : "unknown"}</td>
                  <td>{item.publishedAt ?? "unknown"}</td>
                  <td>{item.available ? "available" : `unavailable (${item.reason ?? "unknown"})`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {observation?.ok ? (
        <Pager
          label="Outputs"
          page={listing.page}
          hasNext={observation.value.nextCursor !== null}
          loading={listing.loading}
          onPage={page}
        />
      ) : null}
      {selected === null ? null : (
        <article className="report-view" aria-label={`Output ${selected.outputId}`}>
          {!selected.available ? (
            <p className="notice notice--warn">
              Output <code>{selected.outputId}</code> is unavailable ({selected.reason ?? "unknown"}); no content is shown.
            </p>
          ) : report === null ? (
            <p className="muted" role="status">
              Reading report…
            </p>
          ) : !report.ok ? (
            <FailureNote failure={report} what="Report" />
          ) : (
            <>
              {contextStale ? <p className="notice notice--warn" role="status">Service context is unavailable. Showing the last successfully read report for this target; it is stale.</p> : null}
              <p className="report-byline">Producer (claimed): <strong>{report.value.producer.id}</strong> {report.value.producer.version}<span>Published {report.value.publishedAt}</span></p>
              {report.value.representation.format !== REPORT_FORMAT ? (
                <p className="notice notice--warn">
                  Report format <code>{report.value.representation.format}</code> is not {REPORT_FORMAT}; it is not rendered.
                </p>
              ) : rendered === null ? (
                <p className="notice notice--warn">The report data does not match {REPORT_FORMAT}; it is not rendered.</p>
              ) : (
                <RenderedReport report={rendered} />
              )}
              <details className="report-evidence"><summary>Exact output provenance</summary><Evidence value={report.value} /></details>
              <div className="toolbar">
                <button type="button" disabled={download.reading} onClick={() => void saveEnvelope(selected, "report")}>
                  Download report (API response)
                </button>
                {selected.json !== null ? (
                  <button type="button" disabled={download.reading} onClick={() => void saveEnvelope(selected, "json")}>
                    Download JSON (API response)
                  </button>
                ) : null}
              </div>
              <p className="muted">
                Downloads are the exact API response envelope. Their file bytes are not the canonical bytes behind the
                representation SHA-256 in Exact output provenance.
              </p>
              {download.failure !== null ? <FailureNote failure={download.failure} what="Download" /> : null}
            </>
          )}
        </article>
      )}
    </section>
  );
}
