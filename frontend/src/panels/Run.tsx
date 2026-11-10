// Run detail: the Overview Status region, and Activity's Planes and Current work.
//
// Every fact is a recorded Core value or "unknown". Run terminal status, Core
// quiescence and writer states stay separate facts: `may_live`, `unknown`,
// `running` and `stopped` are never merged, and quiescence never claims an OS
// caller or holder closure. A failed refresh keeps the last observation with
// its time and labels it stale.

import { useEffect, useState } from "react";

import {
  age,
  counts,
  display,
  list,
  record,
  recorded,
  text,
  yesNo,
  type CoreView,
  type DemoMode,
  type Failure,
  type Json,
  type Target,
} from "../api.ts";

import { ExactValue, shortId, stateClass } from "./Inventory.tsx";

export interface Summary {
  /** The last successful summary, possibly older than `latest`. */
  view: CoreView;
  observedAt: string;
}

function eventText(event: unknown): string {
  const item = record(event);
  if (item === null) return "none";
  if (item.event_kind === "transition") {
    return `${display(item.node_id)} · ${display(item.outcome)} → ${display(item.action)} ${display(item.target)} (v${display(item.version)})`;
  }
  if (item.event_kind === "move") {
    return `${display(item.move_kind)} → ${display(item.target)} (v${display(item.version)})`;
  }
  return display(event);
}

function dependencies(value: unknown): string {
  const items = list(value);
  if (items === null) return "unknown";
  if (items.length === 0) return "none";
  return items.map((item) => `${display(record(item)?.plane_id)} ${display(record(item)?.status)}`).join(", ");
}

/**
 * Each plane's recorded blocker codes and their targets, from public summary
 * fields only: a pause reason with its cursor and resume target, and terminal
 * dependencies of the cursor node. Status shows these whatever panels hide.
 */
export function planeBlockers(summary: Json): string[] {
  const lines: string[] = [];
  for (const [planeId, plane] of planesOf(summary)) {
    if (text(plane.pause_reason) !== null) {
      const resume = text(plane.resume_target);
      lines.push(
        `${planeId}: paused ${display(plane.pause_reason)} at ${recorded(plane, "cursor")}${resume ? `, resume ${resume}` : ""}`,
      );
    }
    const required = list(plane.dependencies) ?? [];
    if (required.length > 0) {
      lines.push(`${planeId}: ${recorded(plane, "cursor")} requires terminal ${dependencies(required)}`);
    }
  }
  return lines;
}

export function planesOf(summary: Json): [string, Json][] {
  return Object.entries(record(summary.planes) ?? {})
    .map(([id, value]): [string, Json] => [id, record(value) ?? {}])
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

function demoText(demo: DemoMode | null): string {
  if (demo === null) return "not yet observed";
  if (demo.kind === "ordinary") return "ordinary service";
  if (demo.kind === "demo") return `owned demo service for instance ${demo.instanceId}, run ${demo.runId}`;
  if (demo.kind === "pending") return "demo context pending";
  return `unknown (${demo.code})`;
}

/** A display clock for the "n s ago" label only; it never triggers a read. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") setNow(Date.now());
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/**
 * The one run announcement: the recorded status, the recorded run-level hold,
 * gate and cancellation facts, and each plane's blocker; or a failed read and
 * whether stale facts remain. The run screen renders it once, outside every
 * view, so it stays exposed whichever view is open. No clock, age or read
 * timestamp enters it: it changes only with a recorded fact or the outcome of
 * the latest read. A requested cancellation is announced as requested, never
 * as stopped work.
 */
export function RunAnnouncement({ summary, latest }: { summary: Summary | null; latest: Failure | null }) {
  const data = summary?.view.data ?? null;
  const message = latest !== null ? `Run observation failed: ${latest.code}. ${summary === null ? "No facts observed." : "Showing stale facts."}`
    : data === null ? "No run summary observed."
    : [`Observed run ${recorded(data, "status")}.`, ...runHoldFacts(data), ...planeBlockers(data).map((line) => `${line}.`)].join(" ");
  return <p className="visually-hidden run-announcement" role="status" aria-live="polite">{message}</p>;
}

export function StatusRegion({ target, summary, latest, loading, demo, onSelectInvocation, onSelectNode, onReviewPlane, onOpenReports, controlBusy, controlBusyReason }: {
  target: Target;
  summary: Summary | null;
  latest: Failure | null;
  loading: boolean;
  demo: DemoMode | null;
  onSelectInvocation: (invocationId: string) => void;
  onSelectNode: (planeId: string, nodeId: string) => void;
  onReviewPlane: (planeId: string) => void;
  onOpenReports: () => void;
  controlBusy: boolean;
  controlBusyReason: string | null;
}) {
  const now = useNow();
  const data = summary?.view.data ?? null;
  const writers = record(data?.writer_counts);
  const candidate = record(data?.pending_candidate);
  const inflight = list(data?.inflight_invocations);
  const status = data === null ? "Not observed" : recorded(data, "status");
  const planes = data === null ? [] : planesOf(data);
  const blockers = data === null ? [] : planeBlockers(data);
  const needsAttention = (plane: Json) => text(plane.active_invocation) !== null
    || plane.status === "running" || text(plane.cursor) === null || text(plane.pause_reason) !== null
    || (list(plane.dependencies)?.length ?? 0) > 0 || (list(plane.pending_completion_ids)?.length ?? 0) > 0;
  const orderedPlanes = [...planes.filter(([, plane]) => needsAttention(plane)), ...planes.filter(([, plane]) => !needsAttention(plane))];
  const attentionCount = planes.filter(([, plane]) => needsAttention(plane)).length;
  const stoppedCounts = (value: unknown) => {
    const values = record(value);
    return values !== null && Object.entries(values).every(([state, count]) => state === "stopped"
      && typeof count === "number" && Number.isInteger(count) && count >= 0);
  };
  const quiet = data?.quiescent === true && stoppedCounts(writers?.recorded)
    && stoppedCounts(writers?.effective) && inflight !== null && inflight.length === 0;
  const next = data === null ? null : nextStep(data, quiet, inflight);
  // Presentation only, shared with the lead: review emphasis is drawn when this
  // fresh observation's lead recommends a plane resume. Otherwise the same
  // button is an ordinary secondary one. It is never disabled or hidden by
  // this, and it claims nothing about whether Core will accept the request.
  const recommendResume = next?.resume === true && latest === null;
  const planeRow = ([planeId, plane]: [string, Json]) => {
    const node = text(plane.cursor) ?? text(plane.resume_target);
    const reason = text(plane.pause_reason);
    const active = text(plane.active_invocation);
    return <div className="attention__item" key={planeId}>
      <div className="attention__identity"><code>{planeId}</code><span className={stateClass(plane.status)}>{recorded(plane, "status")}</span></div>
      <div className="attention__reason"><span className="field-label">{reason === null ? "Recorded cursor" : "Recorded pause reason"}</span><code>{reason ?? recorded(plane, "cursor")}</code>
        {(list(plane.dependencies)?.length ?? 0) > 0 ? <span className="muted">Requires terminal: {dependencies(plane.dependencies)}</span> : null}
        {(list(plane.pending_completion_ids)?.length ?? 0) > 0 ? <span className="muted">Awaiting routing: {list(plane.pending_completion_ids)?.length} completion(s)</span> : null}
        {reason === null ? null : <span className="muted">Inspection target: <code>{node ?? "unknown"}</code>{text(plane.cursor) === null && node !== null ? " (resume target)" : ""}</span>}
        {active !== null ? <span className="muted">Active invocation: <button type="button" className="link" onClick={() => onSelectInvocation(active)}><code>{active}</code></button></span> : null}
        {text(plane.resume_target) !== null ? <span className="muted">Resume target: <code>{text(plane.resume_target)}</code></span> : null}
      </div>
      <div className="toolbar attention__actions">
        {node === null ? null : <button type="button" onClick={() => onSelectNode(planeId, node)}>Inspect node <span className="visually-hidden">{planeId} / {node}</span></button>}
        {reason === null ? null : <button type="button" className={recommendResume ? "button--review" : "button--review button--plain"} disabled={controlBusy} onClick={() => onReviewPlane(planeId)}>Review resume <span className="visually-hidden">plane {planeId}</span></button>}
        {reason !== null && controlBusy && controlBusyReason !== null ? <span className="muted review-block-reason">{controlBusyReason}</span> : null}
      </div>
    </div>;
  };
  const runFacts = data === null ? [] : runLevelFacts(data);
  return (
    <section className="region region--status" aria-labelledby="status-heading">
      <div className="status-lead">
        <h2 id="status-heading" tabIndex={-1}>Status</h2>
        <div className="status-state"><span className={`${stateClass(data === null ? null : status)} state--large`}>{status}</span>
          {latest !== null && summary !== null ? <strong className="observation-qualifier">Stale observation</strong> : null}
        </div>
        {latest !== null && summary !== null ? <p className="observation-failure">Stale: the latest read failed ({latest.code}); these facts may have changed. This is the last successful observation, not the current state: refresh before acting on it.</p> : null}
        {next === null ? null : <p className="status-next">{next.text}</p>}
        {data !== null && next?.reports === true ? <div className="status-actions"><button type="button" onClick={onOpenReports}>Run reports</button></div> : null}
        <p className="observation">
          {summary === null ? loading ? "Reading run summary…" : "No run summary observed."
            : <>Observed <time dateTime={summary.observedAt}>{summary.observedAt}</time> <span className="age">({age(summary.observedAt, now)})</span></>}
          {loading && summary !== null ? " · refreshing…" : ""}
          <span className="service-context">{demoText(demo)}</span>
          {data !== null && next?.reports !== true ? <button type="button" className="link status-reports-link" onClick={onOpenReports}>Run reports</button> : null}
        </p>
      </div>
      {data === null ? null : <>
        {runFacts.length === 0 ? null : <ul className="run-level-facts">{runFacts.map((line) => <li key={line}>{line}</li>)}</ul>}
        {quiet ? null : <dl className="facts safety-facts safety-facts--attention">
          <div><dt>Run status</dt><dd>{status}</dd></div>
          <div><dt>Core quiescence</dt><dd>{yesNo(data.quiescent, "quiescent: every invocation's effective writer is stopped", "not quiescent")}</dd></div>
          <div><dt>Recorded writers (completions)</dt><dd>{counts(writers?.recorded)}</dd></div>
          <div><dt>Effective writers</dt><dd>{counts(writers?.effective)}</dd></div>
          <div><dt>In flight</dt><dd>{inflight === null ? "unknown" : inflight.length === 0 ? "none" : <><span>{inflight.length} invocation(s)</span><details className="inflight-details"><summary>Exact in-flight invocations</summary>{inflight.map((value) => {
            const id = text(value);return id === null ? display(value) : <button type="button" className="link" key={id} onClick={() => onSelectInvocation(id)}><code className="id">{id}</code></button>;
          })}</details></>}</dd></div>
        </dl>}
        <div className="attention" aria-label="Plane activity">
          <div className="attention__heading"><h3>Plane activity</h3></div>
          {planes.length === 0 ? <p className="empty">No plane state recorded.</p> : <>
            {(attentionCount === 0 ? planes : orderedPlanes.slice(0, attentionCount)).map(planeRow)}
            {attentionCount > 0 && attentionCount < planes.length ? <details className="other-planes">
              <summary>Other plane activity ({planes.length - attentionCount})</summary>
              {orderedPlanes.slice(attentionCount).map(planeRow)}
            </details> : null}
          </>}
        </div>
        <details className="technical-details"><summary>Exact target and run details</summary>
          {quiet ? <p className="safety-summary">Core quiescent · recorded writers {counts(writers?.recorded)} · effective writers {counts(writers?.effective)} · in flight none</p> : null}
          <dl className="facts facts--target">
          <div><dt>Home</dt><dd><code>{target.home}</code></dd></div>
          <div><dt>Instance</dt><dd><code className="id">{target.instance}</code></dd></div>
          <div><dt>Run</dt><dd><code className="id">{target.run}</code></dd></div>
          <div><dt>Run status</dt><dd>{status}</dd></div>
          <div><dt>Core quiescence</dt><dd>{yesNo(data.quiescent, "quiescent: every invocation's effective writer is stopped", "not quiescent")}</dd></div>
          <div><dt>Recorded writers (completions)</dt><dd>{counts(writers?.recorded)}</dd></div>
          <div><dt>Effective writers</dt><dd>{counts(writers?.effective)}</dd></div>
          <div><dt>In flight</dt><dd>{inflight === null ? "unknown" : inflight.length === 0 ? "none" : inflight.map(display).join(", ")}</dd></div>
          <div><dt>Core quiescence meaning</dt><dd>Quiescent means every invocation’s effective writer is stopped; it does not establish OS caller or holder closure.</dd></div>
          <div><dt>Edit mode</dt><dd>{recorded(data, "mode")}</dd></div>
          <div><dt>State version</dt><dd>{recorded(data, "state_version")}</dd></div>
          <div><dt>Run gate</dt><dd>{recorded(data, "run_gate")}</dd></div>
          <div><dt>Pause reason</dt><dd>{recorded(data, "pause_reason")}</dd></div>
          <div><dt>Cancellation</dt><dd>{yesNo(data.cancel_requested, "requested", "not requested")}</dd></div>
          <div><dt>Plane blockers</dt><dd>{blockers.length === 0 ? "none recorded" : <ul className="inline-list">{blockers.map(line => <li key={line}>{line}</li>)}</ul>}</dd></div>
          <div><dt>Invocations recorded</dt><dd>{recorded(data, "invocation_count")}</dd></div>
          <div><dt>Active revision</dt><dd><ExactValue value={recorded(data, "active_revision_id")} label="Active revision" /></dd></div>
          <div><dt>Pending candidate</dt><dd>{candidate === null ? recorded(data, "pending_candidate") : `${display(candidate.candidate_id)}${candidate.diagnostic ? ` (diagnostic ${display(candidate.diagnostic)})` : ""}`}</dd></div>
        </dl></details>
      </>}
    </section>
  );
}

export function PlanesRegion({
  summary,
  onSelectNode,
  onSelectInvocation,
}: {
  summary: Json;
  onSelectNode: (planeId: string, nodeId: string) => void;
  onSelectInvocation: (invocationId: string) => void;
}) {
  const planes = planesOf(summary);
  return (
    <section className="region" aria-labelledby="planes-heading">
      <h2 id="planes-heading" tabIndex={-1}>Planes</h2>
      {planes.length === 0 ? (
        <p className="empty">No plane state is recorded for this run.</p>
      ) : (
        <><p className="table-scroll-hint">Scroll horizontally when needed · plane identity stays on the left.</p><div className="table-scroll">
          <table className="planes-table identity-table">
            <caption className="visually-hidden">Recorded plane state</caption>
            <thead>
              <tr>
                <th scope="col">Plane</th>
                <th scope="col">Status</th>
                <th scope="col">Cursor</th>
                <th scope="col">Active invocation</th>
                <th scope="col">Pause reason</th>
                <th scope="col">Resume target</th>
                <th scope="col">Requires terminal</th>
                <th scope="col">Awaiting routing</th>
                <th scope="col">Last recorded event</th>
              </tr>
            </thead>
            <tbody>
              {planes.map(([planeId, plane]) => {
                const cursor = text(plane.cursor);
                const active = text(plane.active_invocation);
                const pending = list(plane.pending_completion_ids);
                return (
                  <tr key={planeId}>
                    <th scope="row" aria-label={planeId}>
                      <ExactValue value={planeId} label={`Plane ${planeId}`} />
                    </th>
                    <td>
                      <span className={stateClass(plane.status)}>{recorded(plane, "status")}</span>
                    </td>
                    <td>
                      {cursor === null ? (
                        recorded(plane, "cursor")
                      ) : (
                        <button type="button" className="link" onClick={() => onSelectNode(planeId, cursor)}>
                          <code>{cursor}</code>
                        </button>
                      )}
                    </td>
                    <td>
                      {active === null ? (
                        recorded(plane, "active_invocation")
                      ) : (
                        <button type="button" className="link" onClick={() => onSelectInvocation(active)}>
                          <code className="id">{active}</code>
                        </button>
                      )}
                    </td>
                    <td>{recorded(plane, "pause_reason")}</td>
                    <td>{recorded(plane, "resume_target")}</td>
                    <td>{dependencies(plane.dependencies)}</td>
                    <td>{pending === null ? "unknown" : pending.length === 0 ? "none" : `${pending.length} completion(s)`}</td>
                    <td>{eventText(plane.last_event)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div></>
      )}
    </section>
  );
}

/**
 * The recorded run-level hold, gate and cancellation facts. Status shows them
 * and the shared run announcement speaks them in these same words.
 */
function runHoldFacts(summary: Json): string[] {
  const lines: string[] = [];
  if (text(summary.pause_reason) !== null) lines.push(`Recorded run pause reason: ${display(summary.pause_reason)}.`);
  else if (summary.pause_reason !== null) lines.push("Run pause reason: unknown.");
  if (text(summary.run_gate) !== null) lines.push(`Run gate: ${display(summary.run_gate)}.`);
  else if (summary.run_gate !== null) lines.push("Run gate: unknown.");
  if (summary.cancel_requested === true) lines.push("Run cancellation requested.");
  else if (summary.cancel_requested !== false) lines.push("Run cancellation: unknown.");
  return lines;
}

/** Run-level facts remain visible even when secondary panels are hidden. */
function runLevelFacts(summary: Json): string[] {
  const lines = runHoldFacts(summary);
  const candidate = record(summary.pending_candidate);
  if (candidate !== null) lines.push(`Pending candidate${candidate.diagnostic === undefined || candidate.diagnostic === null ? "" : ` (diagnostic ${display(candidate.diagnostic)})`}.`);
  else if (summary.pending_candidate !== null) lines.push(`Pending candidate: ${recorded(summary, "pending_candidate")}.`);
  return lines;
}

const TERMINAL_STATUS = new Set(["succeeded", "failed", "cancelled"]);

/**
 * One plain sentence for the first screen, built only from recorded summary
 * facts: what is finished, uncertain, held, paused or working, and where to
 * look next. Run-level facts decide first: an unknown or terminal run status,
 * then a pending cancellation, then a run-level hold. Only an otherwise
 * ordinary run reads its paused planes as the next action, so a failed or
 * uncertain run never recommends a resume. Paused planes are counted, never
 * listed: every exact ID stays in its row under Plane activity. This is
 * presentation only: it never replaces the status, writer or staleness facts
 * beside it, and it enables or disables no operation. `reports` marks the one
 * lead whose next place is the Reports view; `resume` marks the leads that
 * recommend reviewing a plane resume, which is also the only time a row's
 * Review resume button is drawn with review emphasis. Some paused planes record
 * no node to inspect, so the lead asks for it only where one is shown.
 */
function nextStep(summary: Json, quiet: boolean, inflight: unknown[] | null): { text: string; reports: boolean; resume: boolean } | null {
  const status = text(summary.status);
  const paused = planesOf(summary).filter(([, plane]) => text(plane.pause_reason) !== null);
  const one = paused.length === 1;
  // A recorded fact for leads that must not turn it into a recommendation.
  const pausedFact = paused.length === 0 ? ""
    : ` ${one ? "1 plane still records" : `${paused.length} planes still record`} a pause reason; see ${one ? "its row" : "their rows"} under Plane activity.`;
  const writersOpen = "Writer closure is not confirmed; check the writer facts below.";
  const lead = (line: string, reports = false) => ({ text: line, reports, resume: false });
  const reviewResume = `inspect ${one ? "its" : "each"} recorded node where one is shown, then review a resume in ${one ? "its row" : "the rows"} under Plane activity.`;
  if (status === null || status === "unknown") {
    return lead(`The overall run state is not established by this observation, so no next step is inferred. Check the recorded writer and plane facts below.${pausedFact}`);
  }
  if (TERMINAL_STATUS.has(status)) {
    return quiet
      ? lead(`Finished: ${status}. Core records every writer stopped. Published outputs are under Reports.${pausedFact}`, true)
      : lead(`Recorded ${status}. ${writersOpen}${pausedFact}`);
  }
  if (summary.cancel_requested === true) {
    return lead(`Cancellation is requested but not recorded complete: the run is still recorded ${status}.${quiet ? "" : ` Work may still be running. ${writersOpen}`}${pausedFact}`);
  }
  if (text(summary.pause_reason) !== null || text(summary.run_gate) !== null) {
    return paused.length === 0
      ? lead("Held at run level. The recorded reason is below; requests are reviewed under Control.")
      : { ...lead(`Held at run level; the recorded reason is below. ${one ? "1 plane is" : `${paused.length} planes are`} also paused: ${reviewResume}`), resume: true };
  }
  if (paused.length > 0) {
    return { ...lead(`${one ? "1 plane is" : `${paused.length} planes are`} paused and waiting on a resume decision. I${reviewResume.slice(1)}`), resume: true };
  }
  if (status === "running") {
    if (inflight === null) return lead("Running. In-flight work is unknown in this observation.");
    return lead(inflight.length === 0
      ? "Running. No invocation is in flight in this observation."
      : `Working: ${inflight.length} invocation${inflight.length === 1 ? "" : "s"} in flight. No pause or gate is recorded.`);
  }
  return null;
}

/**
 * Recorded facts that explain what the run is doing or waiting on, grouped
 * under the run and under each plane that records one. Every fact is kept; a
 * plane's ID is stated once, by its group, rather than on every line.
 */
export function currentWork(summary: Json): { run: string[]; planes: { planeId: string; facts: string[] }[] } {
  const planes: { planeId: string; facts: string[] }[] = [];
  for (const [planeId, plane] of planesOf(summary)) {
    const facts: string[] = [];
    const active = text(plane.active_invocation);
    if (active !== null) facts.push(`Invocation ${active} at node ${display(plane.cursor)}.`);
    if (text(plane.pause_reason) !== null) {
      const resume = text(plane.resume_target);
      facts.push(`Paused: ${display(plane.pause_reason)}${resume ? `; resume target ${resume}` : ""}.`);
    }
    const required = list(plane.dependencies) ?? [];
    if (required.length > 0) facts.push(`Cursor ${display(plane.cursor)} requires terminal ${dependencies(required)}.`);
    const pending = list(plane.pending_completion_ids) ?? [];
    if (pending.length > 0) facts.push(`${pending.length} completion(s) not yet routed.`);
    if (facts.length > 0) planes.push({ planeId, facts });
  }
  return { run: [...runLevelFacts(summary), `Edit mode: ${recorded(summary, "mode")}.`], planes };
}

export function CurrentWorkRegion({ summary }: { summary: Json }) {
  const work = currentWork(summary);
  return (
    <section className="region" aria-labelledby="work-heading">
      <h2 id="work-heading" tabIndex={-1}>Current work and blockers</h2>
      <div className="work-groups">
        <div className="work-group" role="group" aria-label="Run">
          <div className="work-group__identity"><span className="field-label">Run</span></div>
          <ul className="fact-list">
            {work.run.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
        {work.planes.length === 0 ? (
          <p className="empty">No plane records an active invocation, pause or pending dependency.</p>
        ) : (
          work.planes.map(({ planeId, facts }) => (
            <div className="work-group" role="group" aria-label={`Plane ${planeId}`} key={planeId}>
              <div className="work-group__identity">
                <span className="field-label">Plane</span>
                {/* A long ID is shortened with its full value one disclosure away. */}
                {shortId(planeId) === planeId ? <code>{planeId}</code> : <ExactValue value={planeId} label={`Plane ${planeId}`} />}
              </div>
              <ul className="fact-list">
                {facts.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
