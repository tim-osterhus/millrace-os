// The Layout menu: everyday appearance first, everything else on demand.
//
// The open menu shows the layout choice, theme and density, then one line that
// says whether the page's settings are saved, with the save, save-as-new and
// discard actions that apply to that state. Panels and the saved-layout
// management actions sit behind two disclosures. Edits change this page only:
// nothing here writes without an explicit action, and every write goes through
// the caller's expected-version commit, one request at a time: while a write or
// a reload of saved layouts is pending, the other saved-layout actions wait. A
// control that cannot be used says why beside it. After a write, focus moves to
// the line that reports its result.

import { Fragment, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";

import { validLabel, type Ref, type Snapshot } from "../preferences.ts";
import {
  ARRANGEMENT_LABELS,
  ARRANGEMENTS,
  DENSITIES,
  PANEL_LABELS,
  PANELS,
  PRESET_LABELS,
  samePayload,
  SPLIT_PANELS,
  THEMES,
  type PanelId,
  type StockPayload,
} from "../presets.ts";

export interface Presentation {
  snapshot: Snapshot | null;
  /** Why the saved store is not in use (read failure or unreadable file). */
  problem: string | null;
  active: { ref: Ref | null; name: string };
  saved: StockPayload;
  working: StockPayload;
  /** `reload` marks a result that only re-reading the saved layouts resolves. */
  message: { tone: "info" | "warn" | "error"; text: string; reload?: boolean } | null;
  busy: boolean;
}

export type Actions = {
  saveAs: (name: string) => void;
  save: () => void;
  rename: (name: string) => void;
  duplicate: (name: string) => void;
  remove: () => void;
  makeDefault: () => void;
  reset: () => void;
  exportActive: () => void;
  importFile: (file: File) => Promise<void>;
  replace: () => Promise<void>;
  reload: () => void;
  revert: () => void;
  adopt: (ref: Ref) => void;
  change: (next: StockPayload) => void;
};

/** The optional panels one run view holds, in display order. */
export interface PanelGroup {
  id: string;
  label: string;
  panels: readonly PanelId[];
}

type Section = "changes" | "manage";
type Step = "save-as" | "rename" | "duplicate" | "reset" | "delete" | "replace";

const title = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

export function PresentationMenu({
  presentation,
  blocked,
  panelGroups,
  actions,
}: {
  presentation: Presentation;
  /** Why this page may not write at all (browser trust or service context); the page notice has the code. */
  blocked: string | null;
  panelGroups: readonly PanelGroup[];
  actions: Actions;
}) {
  const { snapshot, working, active, busy, message } = presentation;
  const [step, setStep] = useState<Step | null>(null);
  const [name, setName] = useState("");
  const [origin, setOrigin] = useState<Section>("changes");
  const [manageOpen, setManageOpen] = useState(false);
  const menu = useRef<HTMLDetailsElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const status = useRef<Record<Section, HTMLDivElement | null>>({ changes: null, manage: null });
  const triggers = useRef<Partial<Record<Step, HTMLButtonElement | null>>>({});

  const unsaved = !samePayload(working, presentation.saved);
  const savedLayouts = snapshot?.stock?.configs ?? [];
  const bundled = snapshot?.bundled ?? [];
  const isUser = active.ref?.kind === "user";
  const unreadable = snapshot !== null && (snapshot.status === "invalid" || snapshot.status === "incompatible");
  // The one reason saved layouts cannot be written now, most fundamental first.
  const block =
    blocked ??
    (snapshot === null
      ? presentation.problem === null
        ? "saved layouts have not been read yet"
        : "saved layouts could not be read"
      : unreadable
        ? `the saved-layout file is unreadable (${snapshot.status})`
        : null);
  const canWrite = block === null && !busy;
  /** The first read has not settled: there is nothing to read again yet. */
  const unread = snapshot === null && presentation.problem === null;
  const defaultRef: Ref = snapshot?.stock?.defaultRef ?? { kind: "bundled", id: "precision" };
  // Saved, built-in and default are store facts: none is claimed without a read store.
  const isDefault = snapshot !== null && active.ref !== null && active.ref.kind === defaultRef.kind && active.ref.id === defaultRef.id;
  const choice = active.ref === null ? "" : `${active.ref.kind}:${active.ref.id}`;
  /** Only choices the read store can resolve are offered. */
  const listed = [...bundled.map((item) => `bundled:${item.id}`), ...savedLayouts.map((config) => `user:${config.id}`)].includes(choice);
  const choiceHint =
    snapshot === null
      ? presentation.problem === null
        ? "Layout choices appear once saved layouts are read."
        : "Layouts cannot be switched: saved layouts could not be read. Theme, density and panels still change this page. Reload saved layouts, under Manage layouts, to retry."
      : unsaved
        ? "Choosing another layout discards this page's unsaved changes."
        : null;
  const hiddenPair = SPLIT_PANELS[working.arrangement].filter((panel) => !working.panels[panel]);
  const storedPreset = (isUser ? savedLayouts.find((config) => config.id === active.ref?.id)?.payload.preset : undefined) ?? working.preset;
  const shownPanels = PANELS.filter((panel) => working.panels[panel]).length;
  const grouped = new Set(panelGroups.flatMap((group) => group.panels));
  const ungrouped = PANELS.filter((panel) => !grouped.has(panel));
  const groups: readonly PanelGroup[] =
    ungrouped.length === 0 ? panelGroups : [...panelGroups, { id: "other", label: "Other", panels: ungrouped }];
  const [first, second] = SPLIT_PANELS[working.arrangement];
  const splitView = panelGroups.find((group) => group.panels.includes(first))?.label ?? null;
  const splitPercent = Math.round(working.split_ratios[working.arrangement] * 100);
  const shown: Section = origin === "manage" && manageOpen ? "manage" : "changes";

  const set = <K extends keyof StockPayload>(key: K, value: StockPayload[K]) => actions.change({ ...working, [key]: value });
  const focusStatus = (section: Section) => status.current[section]?.focus();
  /** One explicit action: its result is reported, and focused, in its own section. */
  const act = (section: Section, run: () => void) => {
    setOrigin(section);
    setStep(null);
    run();
    focusStatus(section);
  };
  const toggle = (next: Step, initial = "") => {
    setName(initial);
    setStep((current) => (current === next ? null : next));
  };
  const cancel = () => {
    const from = step;
    setStep(null);
    if (from !== null) triggers.current[from]?.focus();
  };
  const trigger = (next: Step) => ({
    ref: (node: HTMLButtonElement | null) => {
      triggers.current[next] = node;
    },
    "aria-expanded": step === next,
  });
  const onKeyDown = (event: ReactKeyboardEvent<HTMLDetailsElement>) => {
    if (event.key !== "Escape" || event.target instanceof HTMLSelectElement) return;
    if (step !== null) cancel();
    else if (menu.current?.open) {
      menu.current.open = false;
      menu.current.querySelector<HTMLElement>(":scope > summary")?.focus();
    } else return;
    event.preventDefault();
  };

  /** A blocked management action names the reason line beside it. */
  const why = block !== null ? { "aria-describedby": "layout-manage-block" } : {};

  const stateText =
    snapshot === null
      ? `${active.name} is shown in this page${unsaved ? ", with changes made here" : ""}.`
      : active.ref === null
        ? `${active.name} is shown in this page only; it is not a saved choice.`
        : isUser
        ? unsaved
          ? `“${active.name}” has unsaved changes. They apply to this page only.`
          : `“${active.name}” is saved. Edits apply to this page until you save them.`
        : unsaved
          ? `${active.name} is built in and cannot be overwritten. Your changes apply to this page only; save them as a new layout to keep them.`
          : `${active.name} is a built-in layout. Edits apply to this page until you save them as a new layout.`;
  const feedback = (section: Section): ReactNode =>
    shown !== section ? null : (
      <>
        {busy ? <p className="muted">Working…</p> : null}
        {message !== null ? <p className={`notice notice--${message.tone}`}>{message.text}</p> : null}
      </>
    );
  const nameForm = (kind: Step, label: string, hint: string, submit: string, section: Section, run: (value: string) => void) =>
    step !== kind ? null : (
      <NameForm
        id={`layout-name-${kind}`}
        label={label}
        hint={hint}
        submit={submit}
        value={name}
        disabled={!canWrite}
        onChange={setName}
        onCancel={cancel}
        onSubmit={(value) => act(section, () => run(value))}
      />
    );

  return (
    <details className="layout-menu" ref={menu} onKeyDown={onKeyDown}>
      <summary aria-label={`Layout: ${active.name}${unsaved ? " (unsaved changes)" : ""}`}>
        <span className="layout-summary-wide">Layout: {active.name}{unsaved ? " (unsaved changes)" : ""}</span>
        <span className="layout-summary-narrow" aria-hidden="true">Layout</span>
      </summary>
      <div className="layout-menu__body">
        <div className="layout-rows">
          <label className="layout-rows__label" htmlFor="layout-choice">Layout</label>
          <select
            id="layout-choice"
            value={choice}
            disabled={snapshot === null}
            aria-describedby={choiceHint === null ? undefined : "layout-choice-hint"}
            onChange={(event) => {
              const [kind, id] = event.target.value.split(":");
              if (kind === "bundled" || kind === "user") {
                setStep(null);
                actions.adopt({ kind, id } as Ref);
              }
            }}
          >
            {listed ? null : <option value={choice}>{active.name}{snapshot === null ? "" : " (this page only)"}</option>}
            {bundled.length > 0 ? (
              <optgroup label="Built-in">
                {bundled.map(({ id }) => (
                  <option key={id} value={`bundled:${id}`}>
                    {PRESET_LABELS[id as keyof typeof PRESET_LABELS] ?? id}
                    {defaultRef.kind === "bundled" && defaultRef.id === id ? " (default)" : ""}
                  </option>
                ))}
              </optgroup>
            ) : null}
            {savedLayouts.length > 0 ? (
              <optgroup label="Saved">
                {savedLayouts.map((config) => (
                  <option key={config.id} value={`user:${config.id}`}>
                    {config.name}
                    {defaultRef.kind === "user" && defaultRef.id === config.id ? " (default)" : ""}
                  </option>
                ))}
              </optgroup>
            ) : null}
          </select>
          {choiceHint === null ? null : <p className="layout-hint layout-rows__note" id="layout-choice-hint">{choiceHint}</p>}
          <span className="layout-rows__label" id="layout-theme-label">Theme</span>
          <div className="layout-options" role="radiogroup" aria-labelledby="layout-theme-label">
            {THEMES.map((theme) => (
              <label key={theme}>
                <input type="radio" name="theme" checked={working.theme === theme} onChange={() => set("theme", theme)} />
                {title(theme)}
              </label>
            ))}
          </div>
          <span className="layout-rows__label" id="layout-density-label">Density</span>
          <div className="layout-options" role="radiogroup" aria-labelledby="layout-density-label">
            {DENSITIES.map((density) => (
              <label key={density}>
                <input type="radio" name="density" checked={working.density === density} onChange={() => set("density", density)} />
                {title(density)}
              </label>
            ))}
          </div>
        </div>

        <div className="layout-section">
          <div
            className="layout-state"
            role="status"
            tabIndex={-1}
            ref={(node) => {
              status.current.changes = node;
            }}
          >
            <p id="layout-state-text">{stateText}</p>
            {block !== null ? <p className="muted" id="layout-save-block">Saving is unavailable: {block}. Changes still apply to this page.</p> : null}
            {feedback("changes")}
          </div>
          <div className="layout-menu__actions">
            {isUser && unsaved ? (
              <button
                type="button"
                className="button--primary"
                disabled={!canWrite}
                aria-describedby={block !== null ? "layout-save-block" : "layout-state-text"}
                onClick={() => act("changes", actions.save)}
              >
                Save changes
              </button>
            ) : null}
            <button
              type="button"
              className={unsaved && !isUser ? "button--primary" : undefined}
              disabled={!canWrite}
              aria-describedby={block !== null ? "layout-save-block" : undefined}
              onClick={() => toggle("save-as")}
              {...trigger("save-as")}
            >
              Save as new layout…
            </button>
            {unsaved ? (
              <button type="button" onClick={() => act("changes", actions.revert)}>
                Discard changes
              </button>
            ) : null}
            {message?.reload === true && shown === "changes" && !manageOpen ? (
              <button type="button" disabled={busy || unread} onClick={() => act("changes", actions.reload)}>
                Reload saved layouts
              </button>
            ) : null}
          </div>
          {nameForm(
            "save-as",
            "Name for the new layout",
            unsaved
              ? "Saves this page's settings, including its unsaved changes, as a new layout and switches to it."
              : "Saves this page's settings as a new layout and switches to it.",
            "Save new layout",
            "changes",
            actions.saveAs,
          )}
        </div>

        <details className="layout-more">
          <summary>
            Panels and split <span className="muted">{shownPanels} of {PANELS.length} panels shown</span>
          </summary>
          <div className="layout-more__body">
            <div className="layout-rows">
              {groups.map((group) => (
                <Fragment key={group.id}>
                  <span className="layout-rows__label" id={`layout-panels-${group.id}`}>{group.label} view</span>
                  <div className="layout-options" role="group" aria-labelledby={`layout-panels-${group.id}`}>
                    {group.panels.map((panel) => (
                      <label key={panel}>
                        <input
                          type="checkbox"
                          checked={working.panels[panel]}
                          onChange={(event) => set("panels", { ...working.panels, [panel]: event.target.checked })}
                        />
                        {PANEL_LABELS[panel]}
                      </label>
                    ))}
                  </div>
                </Fragment>
              ))}
            </div>
            <p className="layout-hint">
              Hiding a panel removes it from its view, which then offers Show to bring it back. Overview, Reports and Control are always shown.
            </p>
            <div className="layout-rows">
              <label className="layout-rows__label" htmlFor="layout-split">Side by side</label>
              <select
                id="layout-split"
                aria-describedby="layout-split-hint"
                value={working.arrangement}
                onChange={(event) => set("arrangement", event.target.value as StockPayload["arrangement"])}
              >
                {ARRANGEMENTS.map((arrangement) => (
                  <option key={arrangement} value={arrangement}>
                    {ARRANGEMENT_LABELS[arrangement]}: {PANEL_LABELS[SPLIT_PANELS[arrangement][0]]} beside {PANEL_LABELS[SPLIT_PANELS[arrangement][1]]}
                  </option>
                ))}
              </select>
            </div>
            <p className="layout-hint" id="layout-split-hint">
              When both are shown on a wide screen, {PANEL_LABELS[first]} and {PANEL_LABELS[second]} share one resizable
              row{splitView === null ? "" : ` in the ${splitView} view`}, divided {splitPercent} / {100 - splitPercent}; otherwise they stack.
              {hiddenPair.length === 0 ? "" : ` ${hiddenPair.map((panel) => PANEL_LABELS[panel]).join(" and ")} ${hiddenPair.length === 1 ? "is" : "are"} hidden now.`}
              {" "}Drag the divider to resize; each option keeps its own division.
            </p>
          </div>
        </details>

        <details className="layout-more" open={manageOpen} onToggle={(event) => setManageOpen(event.currentTarget.open)}>
          <summary>Manage layouts</summary>
          <div className="layout-more__body">
            <div
              className="layout-state"
              role="status"
              tabIndex={-1}
              ref={(node) => {
                status.current.manage = node;
              }}
            >
              <p>
                <strong>{active.name}</strong>:{" "}
                {snapshot === null
                  ? "shown in this page; saved layouts are not available"
                  : active.ref === null
                    ? "shown in this page only"
                    : isUser
                      ? "a saved layout"
                      : "a built-in layout"}
                {isDefault ? ", the default" : ""}
                {unsaved ? ", with unsaved changes in this page" : ""}.
                {snapshot !== null && active.ref !== null && !isUser ? " Built-in layouts cannot be renamed, reset or removed; duplicate one to make your own." : ""}
              </p>
              {block !== null ? <p className="muted" id="layout-manage-block">Saved layouts cannot be changed: {block}.</p> : null}
              {feedback("manage")}
            </div>
            {snapshot !== null && unreadable ? (
              <>
                <p className="layout-hint confirm">
                  The unreadable file is kept as it is.
                  <button type="button" disabled={blocked !== null || busy} onClick={() => toggle("replace")} {...trigger("replace")}>
                    Replace unreadable file…
                  </button>
                </p>
                {step === "replace" ? (
                  <Confirm
                    question={`Replace the ${snapshot.status} file with an empty store? Its bytes are lost.`}
                    accept="Replace"
                    disabled={blocked !== null || busy}
                    onAccept={() => act("manage", () => void actions.replace())}
                    onCancel={cancel}
                  >
                    Replace the {snapshot.status} file <code className="id">{snapshot.fileSha256}</code> with an empty store? Its
                    bytes are lost.
                  </Confirm>
                ) : null}
              </>
            ) : null}
            <ul className="layout-actions" aria-label={`Actions for ${active.name}`}>
              {isUser ? (
                <li>
                  <button type="button" disabled={!canWrite} {...why} onClick={() => toggle("rename", active.name)} {...trigger("rename")}>
                    Rename…
                  </button>
                  <span>Change its name.</span>
                  {nameForm("rename", "New name", `Renames “${active.name}”; its settings stay as saved.`, "Rename layout", "manage", actions.rename)}
                </li>
              ) : null}
              <li>
                <button type="button" disabled={!canWrite} {...why} onClick={() => toggle("duplicate", `${active.name} copy`)} {...trigger("duplicate")}>
                  Duplicate…
                </button>
                <span>
                  {isUser ? "Copy its saved settings under a new name" : "Copy its built-in settings as a layout you can change"}
                  {unsaved ? "; this page's unsaved changes are not copied." : "."}
                </span>
                {nameForm(
                  "duplicate",
                  "Name for the copy",
                  unsaved
                    ? `Copies the ${isUser ? "saved" : "built-in"} settings and switches to the copy; this page's unsaved changes are discarded. To keep them, use Save as new layout instead.`
                    : `Copies the ${isUser ? "saved" : "built-in"} settings and switches to the copy.`,
                  "Duplicate layout",
                  "manage",
                  actions.duplicate,
                )}
              </li>
              {isDefault ? null : (
                <li>
                  <button type="button" disabled={!canWrite} {...why} onClick={() => act("manage", actions.makeDefault)}>
                    Make default
                  </button>
                  <span>Open it first on each page load.</span>
                </li>
              )}
              {isUser ? (
                <li>
                  <button type="button" disabled={!canWrite} {...why} onClick={() => toggle("reset")} {...trigger("reset")}>
                    Reset…
                  </button>
                  <span>Replace its saved settings with built-in {PRESET_LABELS[storedPreset]}.</span>
                  {step === "reset" ? (
                    <Confirm
                      question={`Reset “${active.name}” to the built-in ${PRESET_LABELS[storedPreset]} settings? Its saved settings are replaced.`}
                      accept="Reset layout"
                      disabled={!canWrite}
                      onAccept={() => act("manage", actions.reset)}
                      onCancel={cancel}
                    />
                  ) : null}
                </li>
              ) : null}
              {isUser ? (
                <li>
                  <button type="button" disabled={!canWrite} {...why} onClick={() => toggle("delete")} {...trigger("delete")}>
                    Delete…
                  </button>
                  <span>Remove it from saved layouts.</span>
                  {step === "delete" ? (
                    <Confirm
                      question={`Delete “${active.name}”? It is removed for every client of this service. This page keeps its current settings under built-in ${PRESET_LABELS[storedPreset]}.`}
                      accept="Delete layout"
                      disabled={!canWrite}
                      onAccept={() => act("manage", actions.remove)}
                      onCancel={cancel}
                    />
                  ) : null}
                </li>
              ) : null}
              <li>
                <button type="button" disabled={snapshot === null || unreadable} onClick={() => { setOrigin("manage"); actions.exportActive(); }}>
                  Export file
                </button>
                <span>
                  {snapshot === null
                    ? "Unavailable until saved layouts are read."
                    : unreadable
                      ? "Unavailable while the saved-layout file is unreadable."
                      : unsaved
                      ? "Download its saved version as JSON; unsaved changes are not included."
                      : "Download it as a JSON file."}
                </span>
              </li>
            </ul>
            <ul className="layout-actions layout-actions--store" aria-label="All saved layouts">
              <li>
                <input
                  ref={fileInput}
                  type="file"
                  className="visually-hidden"
                  tabIndex={-1}
                  aria-hidden="true"
                  accept="application/json,.json"
                  disabled={!canWrite}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    event.target.value = "";
                    if (file !== undefined) act("manage", () => void actions.importFile(file));
                  }}
                />
                <button type="button" disabled={!canWrite} {...why} onClick={() => fileInput.current?.click()}>
                  Import file…
                </button>
                <span>
                  Add a layout from an exported JSON file and switch to it. No saved layout is overwritten
                  {unsaved ? "; this page's unsaved changes are discarded." : "."}
                </span>
              </li>
              <li>
                <button type="button" disabled={busy || unread} onClick={() => act("manage", actions.reload)}>
                  Reload saved layouts
                </button>
                <span>
                  {unread
                    ? "Unavailable until the first read of saved layouts finishes."
                    : "Read them again and show the default layout. Unsaved changes in this page are discarded."}
                </span>
              </li>
            </ul>
            <p className="layout-hint">
              {snapshot === null ? "Saved-layout store: not read." : `Saved-layout store: ${snapshot.status}${snapshot.version ? `, version ${snapshot.version}` : ""}.`}
              {snapshot?.others.length ? ` ${snapshot.others.length} other frontend namespace(s) are kept unchanged.` : ""}
            </p>
          </div>
        </details>
      </div>
    </details>
  );
}

/** One named step (save as new, duplicate or rename); focus starts in the name. */
function NameForm({
  id,
  label,
  hint,
  submit,
  value,
  disabled,
  onChange,
  onSubmit,
  onCancel,
}: {
  id: string;
  label: string;
  hint: string;
  submit: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  onCancel: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, []);
  // The saved-name contract (1-256 code points, plain text) decides validity, so
  // an existing name is never cut at a UTF-16 length.
  const trimmed = value.trim();
  useEffect(() => {
    input.current?.setCustomValidity(
      trimmed === "" || validLabel(trimmed) ? "" : "Use at most 256 characters, without tabs or line breaks",
    );
  }, [trimmed]);
  return (
    <form
      className="layout-step"
      onSubmit={(event) => {
        event.preventDefault();
        if (!disabled && validLabel(trimmed)) onSubmit(trimmed);
      }}
    >
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        ref={input}
        value={value}
        required
        pattern=".*\S.*"
        title="Enter a name"
        autoComplete="off"
        aria-describedby={`${id}-hint`}
        onChange={(event) => onChange(event.target.value)}
      />
      <p className="layout-hint" id={`${id}-hint`}>{hint}</p>
      <div className="layout-menu__actions">
        <button type="submit" className="button--primary" disabled={disabled}>
          {submit}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** A deliberate second step for an action that replaces or removes saved data. */
function Confirm({
  question,
  accept,
  disabled,
  onAccept,
  onCancel,
  children,
}: {
  question: string;
  accept: string;
  disabled: boolean;
  onAccept: () => void;
  onCancel: () => void;
  /** The visible question when it carries more than the label's text. */
  children?: ReactNode;
}) {
  const group = useRef<HTMLDivElement>(null);
  // Focus the question, not a button: a repeated Enter confirms nothing.
  useEffect(() => group.current?.focus(), []);
  return (
    <div className="layout-step" role="group" aria-label={question} tabIndex={-1} ref={group}>
      <p>{children ?? question}</p>
      <div className="layout-menu__actions">
        <button type="button" disabled={disabled} onClick={onAccept}>
          {accept}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
