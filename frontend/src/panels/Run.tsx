// Run detail: the unhideable Status region, Planes and Current work/blockers.
//
// Every fact is a recorded Core value or "unknown". Run terminal status, Core
// quiescence and writer states stay separate rows: `may_live`, `unknown`,
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

export function StatusRegion({
  target,
  summary,
  latest,
  loading,
  demo,
  onSelectInvocation,
}: {
  target: Target;
  summary: Summary | null;
  latest: Failure | null;
  loading: boolean;
  demo: DemoMode | null;
  onSelectInvocation: (invocationId: string) => void;
}) {
  const now = useNow();
  const data = summary?.view.data ?? null;
  const inflight = list(data?.inflight_invocations);
  const writers = record(data?.writer_counts);
  const candidate = record(data?.pending_candidate);
  const blockers = data === null ? [] : planeBlockers(data);
  return (
    <section className="region region--status" aria-labelledby="status-heading">
      <h2 id="status-heading">Status</h2>
      <dl className="facts facts--target">
        <div>
          <dt>Home</dt>
          <dd>
            <code>{target.home}</code>
          </dd>
        </div>
        <div>
          <dt>Instance</dt>
          <dd>
            <code className="id">{target.instance}</code>
          </dd>
        </div>
        <div>
          <dt>Run</dt>
          <dd>
            <code className="id">{target.run}</code>
          </dd>
        </div>
      </dl>
      <p className="observation" role="status" aria-live="polite">
        {summary === null
          ? loading
            ? "Reading run summary…"
            : "No run summary observed."
          : `Observed ${summary.observedAt} (${age(summary.observedAt, now)})`}
        {latest !== null && summary !== null ? (
          <span className="state state--stale">
            {" "}
            Stale: the latest read failed ({latest.code}); these facts may have changed.
          </span>
        ) : null}
        {loading && summary !== null ? " · refreshing…" : ""}
      </p>
      {data === null ? null : (
        <dl className="facts">
          <div>
            <dt>Run status</dt>
            <dd>
              <span className="state">{recorded(data, "status")}</span>
            </dd>
          </div>
          <div>
            <dt>Edit mode</dt>
            <dd>{recorded(data, "mode")}</dd>
          </div>
          <div>
            <dt>State version</dt>
            <dd>{recorded(data, "state_version")}</dd>
          </div>
          <div>
            <dt>Run gate</dt>
            <dd>{recorded(data, "run_gate")}</dd>
          </div>
          <div>
            <dt>Pause reason</dt>
            <dd>{recorded(data, "pause_reason")}</dd>
          </div>
          <div>
            <dt>Cancellation</dt>
            <dd>{yesNo(data.cancel_requested, "requested", "not requested")}</dd>
          </div>
          <div>
            <dt>Core quiescence</dt>
            <dd>
              {yesNo(data.quiescent, "quiescent: every invocation's effective writer is stopped", "not quiescent")}
            </dd>
          </div>
          <div>
            <dt>Plane blockers</dt>
            <dd>
              {blockers.length === 0 ? (
                "none recorded"
              ) : (
                <ul className="inline-list">
                  {blockers.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              )}
            </dd>
          </div>
          <div>
            <dt>Recorded writers (completions)</dt>
            <dd>{counts(writers?.recorded)}</dd>
          </div>
          <div>
            <dt>Effective writers</dt>
            <dd>{counts(writers?.effective)}</dd>
          </div>
          <div>
            <dt>In flight</dt>
            <dd>
              {inflight === null ? (
                "unknown"
              ) : inflight.length === 0 ? (
                "none"
              ) : (
                <ul className="inline-list">
                  {inflight.map((value) => {
                    const id = text(value);
                    return (
                      <li key={String(value)}>
                        {id === null ? (
                          display(value)
                        ) : (
                          <button type="button" className="link" onClick={() => onSelectInvocation(id)}>
                            <code className="id">{id}</code>
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </dd>
          </div>
          <div>
            <dt>Invocations recorded</dt>
            <dd>{recorded(data, "invocation_count")}</dd>
          </div>
          <div>
            <dt>Active revision</dt>
            <dd>
              <code className="id">{recorded(data, "active_revision_id")}</code>
            </dd>
          </div>
          <div>
            <dt>Pending candidate</dt>
            <dd>
              {candidate === null
                ? recorded(data, "pending_candidate")
                : `${display(candidate.candidate_id)}${candidate.diagnostic ? ` (diagnostic ${display(candidate.diagnostic)})` : ""}`}
            </dd>
          </div>
          <div>
            <dt>Service</dt>
            <dd>{demoText(demo)}</dd>
          </div>
        </dl>
      )}
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
      <h2 id="planes-heading">Planes</h2>
      {planes.length === 0 ? (
        <p className="empty">No plane state is recorded for this run.</p>
      ) : (
        <div className="table-scroll">
          <table>
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
                    <th scope="row">
                      <code>{planeId}</code>
                    </th>
                    <td>
                      <span className="state">{recorded(plane, "status")}</span>
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
        </div>
      )}
    </section>
  );
}

/** Recorded facts that explain what the run is doing or waiting on. */
export function currentWork(summary: Json): string[] {
  const lines: string[] = [];
  if (text(summary.pause_reason) !== null) lines.push(`Run paused: ${display(summary.pause_reason)}.`);
  if (text(summary.run_gate) !== null) lines.push(`Run gate: ${display(summary.run_gate)}.`);
  if (summary.cancel_requested === true) lines.push("Run cancellation requested.");
  for (const [planeId, plane] of planesOf(summary)) {
    const active = text(plane.active_invocation);
    if (active !== null) {
      lines.push(`Plane ${planeId}: invocation ${active} at node ${display(plane.cursor)}.`);
    }
    if (text(plane.pause_reason) !== null) {
      const resume = text(plane.resume_target);
      lines.push(`Plane ${planeId} paused: ${display(plane.pause_reason)}${resume ? `; resume target ${resume}` : ""}.`);
    }
    const required = list(plane.dependencies) ?? [];
    if (required.length > 0) {
      lines.push(`Plane ${planeId} cursor ${display(plane.cursor)} requires terminal ${dependencies(required)}.`);
    }
    const pending = list(plane.pending_completion_ids) ?? [];
    if (pending.length > 0) lines.push(`Plane ${planeId}: ${pending.length} completion(s) not yet routed.`);
  }
  return lines;
}

export function CurrentWorkRegion({ summary }: { summary: Json }) {
  const lines = currentWork(summary);
  return (
    <section className="region" aria-labelledby="work-heading">
      <h2 id="work-heading">Current work and blockers</h2>
      {lines.length === 0 ? (
        <p className="empty">No active invocation, pause, gate or pending dependency is recorded.</p>
      ) : (
        <ul className="fact-list">
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
