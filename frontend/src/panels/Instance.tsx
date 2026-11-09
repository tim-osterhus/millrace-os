// One instance: identity, common settings with provenance, and its runs.

import { display, record, recorded, text, type CoreView, type Target } from "../api.ts";
import { FailureNote, Pager, TargetLink, type Paged } from "./Inventory.tsx";

function settingRows(value: unknown, keys: [string, string][]) {
  const settings = record(value);
  return keys.map(([key, label]) => (
    <div key={key}>
      <dt>{label}</dt>
      <dd>{settings === null ? "unknown" : key in settings ? display(settings[key]) : "not set"}</dd>
    </div>
  ));
}

export function Instance({
  target,
  page,
  onPage,
  navigate,
}: {
  target: Target;
  page: Paged<CoreView>;
  onPage: (direction: 1 | -1) => void;
  navigate: (target: Target) => void;
}) {
  const observation = page.observation;
  if (observation === null) {
    return (
      <p className="muted" role="status">
        {page.loading ? "Reading instance…" : "Not read yet."}
      </p>
    );
  }
  if (!observation.ok) return <FailureNote failure={observation} what="Instance" />;
  const data = observation.value.data;
  const runs = observation.value.items.map((item) => record(item) ?? {});
  return (
    <>
      <section className="region" aria-labelledby="instance-heading">
        <h2 id="instance-heading">Instance settings</h2>
        <dl className="facts">
          <div>
            <dt>Project root</dt>
            <dd className="path">{display(data.project_root)}</dd>
          </div>
          <div>
            <dt>Workflow file</dt>
            <dd className="path">{display(data.workflow_file)}</dd>
          </div>
          <div>
            <dt>Settings version</dt>
            <dd>{display(data.settings_version)}</dd>
          </div>
        </dl>
        <h3>Runtime defaults</h3>
        <dl className="facts">
          {settingRows(data.runtime_defaults, [
            ["binding", "Binding"],
            ["timeout_seconds", "Timeout (s)"],
          ])}
        </dl>
        <h3>Operator policy</h3>
        <dl className="facts">
          {settingRows(data.operator_policy, [
            ["binding_pin", "Binding pin"],
            ["timeout_ceiling_seconds", "Timeout ceiling (s)"],
          ])}
        </dl>
      </section>
      <section className="region" aria-labelledby="runs-heading">
        <h2 id="runs-heading">Runs, newest first</h2>
        {runs.length === 0 && page.page === 0 ? (
          <p className="empty">This instance has no runs yet. A run begins only with an explicit Start request.</p>
        ) : (
          <table>
            <caption className="visually-hidden">Runs of instance {target.instance}</caption>
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Status</th>
                <th scope="col">Edit mode</th>
                <th scope="col">State version</th>
                <th scope="col">Active revision</th>
                <th scope="col">Pause reason</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run, index) => {
                const id = text(run.run_id);
                return (
                  <tr key={id ?? index}>
                    <th scope="row">
                      {id === null || target.home === null ? (
                        "unknown"
                      ) : (
                        <TargetLink target={{ home: target.home, instance: target.instance, run: id }} navigate={navigate}>
                          <code className="id">{id}</code>
                        </TargetLink>
                      )}
                    </th>
                    <td>
                      <span className="state">{recorded(run, "status")}</span>
                    </td>
                    <td>{recorded(run, "mode")}</td>
                    <td>{recorded(run, "state_version")}</td>
                    <td>
                      <code className="id">{recorded(run, "active_revision_id")}</code>
                    </td>
                    <td>{recorded(run, "pause_reason")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <Pager
          label="Runs"
          page={page.page}
          hasNext={observation.value.nextCursor !== null}
          loading={page.loading}
          onPage={onPage}
        />
      </section>
    </>
  );
}
