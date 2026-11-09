// Configured state homes and the selected home's instances, plus the small
// shared panel primitives (target links, typed failures, explicit paging).

import type { MouseEvent, ReactNode } from "react";

import { display, record, targetHref, text, type CoreView, type Failure, type HomesPage, type Observed, type Target } from "../api.ts";

/** An ordinary link to a fixed shell target; plain clicks stay in-page. */
export function TargetLink({
  target,
  navigate,
  children,
}: {
  target: Target;
  navigate: (target: Target) => void;
  children: ReactNode;
}) {
  const follow = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(target);
  };
  return (
    <a href={targetHref(target)} onClick={follow}>
      {children}
    </a>
  );
}

const MEANINGS: Record<string, string> = {
  client_deadline: "No response within 5 seconds. Not observed: the service may still be working.",
  network_unavailable: "The service could not be reached. Not observed: nothing is known about its work.",
  response_too_large: "The response exceeded the 1,049,600-byte read limit. Use the headless CLI or API.",
  router_read_busy: "Eight reads are already in progress on the service. Retry shortly.",
  router_inspection_unavailable: "Router inspection is not installed on this service (router extra).",
  workflow_views_unavailable: "Generic workflow outputs are not available on this service.",
  core_inspection_too_large: "This view exceeds Core's bounded inspection budget. Use the headless CLI or API.",
  inspection_changed: "The data changed between pages. Go back to the first page.",
  trust_unavailable: "Browser trust is not established. Run millrace-os open again.",
  authentication_required: "The browser session ended. Run millrace-os open again.",
  storage_unavailable: "OS storage is unavailable. Use millrace-os open --recovery for diagnostics.",
};

export function FailureNote({ failure, what }: { failure: Failure; what: string }) {
  const meaning = MEANINGS[failure.code] ?? failure.summary;
  return (
    <p className="notice notice--error" role="status">
      <strong>{what} not read</strong> <code>{failure.code}</code>
      {failure.status > 0 ? ` (HTTP ${failure.status})` : ""}. {meaning}
    </p>
  );
}

export function Pager({
  label,
  page,
  hasNext,
  loading,
  onPage,
}: {
  label: string;
  page: number;
  hasNext: boolean;
  loading: boolean;
  onPage: (direction: 1 | -1) => void;
}) {
  return (
    <nav className="pager" aria-label={`${label} pages`}>
      <button type="button" disabled={loading || page === 0} onClick={() => onPage(-1)}>
        Previous
      </button>
      <span>Page {page + 1}</span>
      <button type="button" disabled={loading || !hasNext} onClick={() => onPage(1)}>
        Next
      </button>
    </nav>
  );
}

export interface Paged<T> {
  observation: Observed<T> | null;
  loading: boolean;
  page: number;
}

export interface InventoryProps {
  target: Target;
  homes: Paged<HomesPage>;
  instances: Paged<CoreView> | null;
  onPage: (table: "homes" | "instances", direction: 1 | -1) => void;
  navigate: (target: Target) => void;
}

export function Inventory({ target, homes, instances, onPage, navigate }: InventoryProps) {
  return (
    <>
      <section className="region" aria-labelledby="homes-heading">
        <h2 id="homes-heading">Configured state homes</h2>
        {homes.observation === null ? (
          <p className="muted" role="status">
            {homes.loading ? "Reading configured homes…" : "Not read yet."}
          </p>
        ) : !homes.observation.ok ? (
          <FailureNote failure={homes.observation} what="Configured homes" />
        ) : homes.observation.value.names.length === 0 && homes.page === 0 ? (
          <p className="empty">
            This service has no configured state homes. Start it with <code>--router-state-home NAME=PATH</code>.
          </p>
        ) : (
          <table>
            <caption className="visually-hidden">State homes configured for this service</caption>
            <thead>
              <tr>
                <th scope="col">Home</th>
                <th scope="col">Selection</th>
              </tr>
            </thead>
            <tbody>
              {homes.observation.value.names.map((name) => (
                <tr key={name} aria-current={name === target.home ? "true" : undefined}>
                  <th scope="row">
                    <TargetLink target={{ home: name, instance: null, run: null }} navigate={navigate}>
                      <code>{name}</code>
                    </TargetLink>
                  </th>
                  <td>{name === target.home ? "selected" : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {homes.observation?.ok ? (
          <Pager
            label="Homes"
            page={homes.page}
            hasNext={homes.observation.value.nextCursor !== null}
            loading={homes.loading}
            onPage={(direction) => onPage("homes", direction)}
          />
        ) : null}
      </section>
      {target.home !== null && instances !== null ? (
        <section className="region" aria-labelledby="instances-heading">
          <h2 id="instances-heading">
            Instances in <code>{target.home}</code>
          </h2>
          <InstanceTable home={target.home} instances={instances} onPage={onPage} navigate={navigate} />
        </section>
      ) : null}
    </>
  );
}

function InstanceTable({
  home,
  instances,
  onPage,
  navigate,
}: {
  home: string;
  instances: Paged<CoreView>;
  onPage: InventoryProps["onPage"];
  navigate: (target: Target) => void;
}) {
  const observation = instances.observation;
  if (observation === null) {
    return (
      <p className="muted" role="status">
        {instances.loading ? "Reading instances…" : "Not read yet."}
      </p>
    );
  }
  if (!observation.ok) return <FailureNote failure={observation} what="Instances" />;
  const rows = observation.value.items.map((item) => record(item) ?? {});
  return (
    <>
      {rows.length === 0 && instances.page === 0 ? (
        <p className="empty">No instances in this home.</p>
      ) : (
        <table>
          <caption className="visually-hidden">Instances in {home}</caption>
          <thead>
            <tr>
              <th scope="col">Instance</th>
              <th scope="col">Project root</th>
              <th scope="col">Workflow file</th>
              <th scope="col">Settings version</th>
              <th scope="col">Store format</th>
              <th scope="col">Read</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const id = text(row.instance_id);
              const error = text(row.error_code);
              return (
                <tr key={id ?? index}>
                  <th scope="row">
                    {id === null ? (
                      "unknown"
                    ) : (
                      <TargetLink target={{ home, instance: id, run: null }} navigate={navigate}>
                        <code className="id">{id}</code>
                      </TargetLink>
                    )}
                  </th>
                  <td className="path">{display(row.project_root)}</td>
                  <td className="path">{display(row.workflow_file)}</td>
                  <td>{display(row.settings_version)}</td>
                  <td>{display(row.store_format)}</td>
                  <td>{error === null ? "available" : <span className="state state--refused">refused {error}</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <Pager
        label="Instances"
        page={instances.page}
        hasNext={observation.value.nextCursor !== null}
        loading={instances.loading}
        onPage={(direction) => onPage("instances", direction)}
      />
    </>
  );
}
