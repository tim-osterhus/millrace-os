// The stock operator client: inventory -> instance -> run, one target at a time.
//
// Targets live only in the validated `home`/`instance`/`run` query and the
// History API. Reads are sequential and bounded (homes or the selected page,
// then the selected summary); optional panels read only when opened, and a
// hidden panel never reads. Presentation changes alter layout only: never the
// target, selection, trust or any request. Saved presentations are written
// only by an explicit action, with the service's expected-version check.
// Control requests are reviewed exact Core envelopes kept, with their bytes, in
// one lexical intent set carried by this tab's history entries; one finite
// selected-target coordinator observes the summary and unresolved requests.

import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Group, Panel, Separator, type GroupImperativeHandle } from "react-resizable-panels";

import {
  capabilities,
  demoContext,
  homes,
  instance,
  instances,
  integer,
  invocation,
  NO_TARGET,
  observeRequest,
  parseCapturedGraph,
  parseTarget,
  presentationState,
  putPresentation,
  record,
  revisionGraph,
  runHistory,
  runSummary,
  saveText,
  submitRequest,
  targetHref,
  text,
  type Capability,
  type CapturedGraph,
  type CoreView,
  type DemoMode,
  type Failure,
  type HistorySection,
  type HomesPage,
  type Observed,
  type Target,
} from "./api.ts";
import { Inspector, type Selection } from "./panels/Inspector.tsx";
import { Instance } from "./panels/Instance.tsx";
import { FailureNote, Inventory, TargetLink, type Paged } from "./panels/Inventory.tsx";
import { CurrentWorkRegion, planesOf, PlanesRegion, StatusRegion, type Summary } from "./panels/Run.tsx";
import {
  ControlForm,
  MonitorStatus,
  RequestArea,
  type ControlTarget,
  type MonitorState,
  type RequestFlags,
  type SendResult,
} from "./panels/Controls.tsx";
import {
  applyReply,
  envelopeOf,
  fits,
  intentFromBuilt,
  keyOf,
  merge,
  newEntry,
  nextDelay,
  outstanding,
  readHistory,
  RECEIPT_FIRST_MS,
  restoredEntry,
  RECEIPT_MS,
  resolved,
  sameTarget,
  stateWith,
  SUMMARY_MS,
  WINDOW_MS,
  type Built,
  type Entry,
  type Operation,
} from "./requests.ts";
import {
  createConfig,
  deleteConfig,
  duplicateConfig,
  effectiveDefault,
  exportConfig,
  IMPORT_LIMIT,
  importConfig,
  PreferenceError,
  readSnapshot,
  renameConfig,
  replaceUnreadable,
  resetConfig,
  resolveRef,
  setDefaultConfig,
  updateConfig,
  writeVisible,
  type Ref,
  type Snapshot,
  type WriteBody,
} from "./preferences.ts";
import {
  ARRANGEMENT_LABELS,
  ARRANGEMENTS,
  copyPayload,
  DENSITIES,
  PANEL_LABELS,
  PANELS,
  PRECISION_FALLBACK,
  PRESET_LABELS,
  PRESETS,
  samePayload,
  SPLIT_PANELS,
  THEMES,
  type PanelId,
  type StockPayload,
} from "./presets.ts";
import { currentTrust, onTrust, type Trust } from "./session.ts";

const GraphView = lazy(() => import("./graph.tsx"));
const History = lazy(() => import("./panels/History.tsx"));
const Revisions = lazy(() => import("./panels/Revisions.tsx"));
const Report = lazy(() => import("./panels/Report.tsx"));

// ---- small hooks -------------------------------------------------------------

function useTrust(): Trust {
  const [trust, setTrust] = useState(currentTrust);
  useEffect(() => {
    const stop = onTrust(setTrust);
    setTrust(currentTrust()); // a change before subscription is not missed
    return stop;
  }, []);
  return trust;
}

function useNarrow(): boolean {
  const query = "(max-width: 56rem)";
  const [narrow, setNarrow] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const change = () => setNarrow(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  return narrow;
}

function safeSnapshot(source: string): Snapshot | null {
  try {
    return readSnapshot(source);
  } catch {
    return null;
  }
}

function idle<T>(): Paged<T> {
  return { observation: null, loading: false, page: 0 };
}

/** A read bound to the exact target (and History section) it was issued for. */
type Keyed<T> = Paged<T> & { key: string | null };

function unkeyed<T>(): Keyed<T> {
  return { observation: null, loading: false, page: 0, key: null };
}

/**
 * Only an observation issued for `key` may render; anything else is a pending
 * read. Paging keeps the key: the shown rows carry their own page number.
 */
function bound<T>(page: Keyed<T>, key: string): Paged<T> {
  return page.key === key ? page : { observation: null, loading: true, page: 0 };
}

function sameRef(a: Ref | null, b: Ref | null): boolean {
  return a === null || b === null ? a === b : a.kind === b.kind && a.id === b.id;
}

// ---- control requests ----------------------------------------------------------

/** Optional router features this service may lack; each is named when absent. */
const OPTIONAL_FEATURES: Record<string, string> = {
  "router.views.run": "Router inspection",
  "router.outputs.list": "Workflow outputs",
  "router.request.submit": "Control requests",
  "router.views.request": "Request observation",
  "router.views.validate": "Current-file validation",
};

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

type Reviewed = Extract<Built, { ok: true }>;
type ReadOutcome = "ok" | "failed" | "budget";

interface Monitor {
  since: number;
  errors: number;
  halted: "window" | "errors" | "budget" | null;
  /** The first receipt read after a new request comes after one second. */
  fast: boolean;
  beat: number;
}

const freshMonitor = (): Monitor => ({ since: Date.now(), errors: 0, halted: null, fast: true, beat: 0 });

/** What one presentation write was issued for, captured when it is sent. */
interface Intent {
  ref: Ref | null;
  sent: StockPayload;
}

// ---- presentation state ------------------------------------------------------

interface Presentation {
  snapshot: Snapshot | null;
  /** Why the saved store is not in use (read failure or unreadable file). */
  problem: string | null;
  active: { ref: Ref | null; name: string };
  saved: StockPayload;
  working: StockPayload;
  message: { tone: "info" | "warn" | "error"; text: string } | null;
  busy: boolean;
}

const INITIAL_PRESENTATION: Presentation = {
  snapshot: null,
  problem: null,
  active: { ref: null, name: "Precision" },
  saved: PRECISION_FALLBACK,
  working: PRECISION_FALLBACK,
  message: null,
  busy: false,
};

function adoptDefault(snapshot: Snapshot): Pick<Presentation, "active" | "saved" | "working" | "problem"> {
  const effective = effectiveDefault(snapshot, PRECISION_FALLBACK);
  return {
    active: { ref: effective.ref, name: effective.name },
    saved: copyPayload(effective.payload),
    working: copyPayload(effective.payload),
    problem: effective.reason,
  };
}

// ---- application -------------------------------------------------------------

export function App() {
  const trust = useTrust();
  const narrow = useNarrow();
  const [target, setTarget] = useState<Target>(() => parseTarget(window.location.search) ?? NO_TARGET);
  const [targetValid] = useState(() => parseTarget(window.location.search) !== null);
  const [demo, setDemo] = useState<DemoMode | null>(null);
  const [unavailable, setUnavailable] = useState<Capability[]>([]);
  const [booted, setBooted] = useState(false);
  const [refreshEpoch, setRefreshEpoch] = useState(0);

  const [homeCursors, setHomeCursors] = useState<(string | null)[]>([null]);
  const [instanceCursors, setInstanceCursors] = useState<(string | null)[]>([null]);
  const [runCursors, setRunCursors] = useState<(string | null)[]>([null]);
  const [homesPage, setHomesPage] = useState<Paged<HomesPage>>(idle);
  const [instancesPage, setInstancesPage] = useState<Keyed<CoreView>>(unkeyed);
  const [instancePage, setInstancePage] = useState<Keyed<CoreView>>(unkeyed);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryFailure, setSummaryFailure] = useState<Failure | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);

  const [selection, setSelection] = useState<Selection | null>(null);
  const [graphs, setGraphs] = useState<Map<string, Observed<CoreView> | "loading">>(() => new Map());
  const [graphRevision, setGraphRevision] = useState<string | null>(null);
  const [graphPlane, setGraphPlane] = useState<string | null>(null);
  const [historySection, setHistorySection] = useState<HistorySection | null>(null);
  const [historyCursors, setHistoryCursors] = useState<(string | null)[]>([null]);
  const [historyPage, setHistoryPage] = useState<Keyed<CoreView>>(unkeyed);
  const [invocationPage, setInvocationPage] = useState<Keyed<CoreView> | null>(null);
  const [reportsOpen, setReportsOpen] = useState(false);
  const [presentation, setPresentation] = useState<Presentation>(INITIAL_PRESENTATION);
  const headingRef = useRef<HTMLHeadingElement>(null);

  // One lexical intent set for this tab, restored observe-only from this entry.
  const [restored] = useState(() => readHistory(window.history.state));
  const intentsRef = useRef<Entry[]>(restored.kept.map(restoredEntry));
  const [entries, setEntries] = useState<Entry[]>(intentsRef.current);
  const [flags, setFlags] = useState<RequestFlags>({ historyUnavailable: false, overCapacity: false, invalidRestore: restored.invalid });
  // History data that could not be restored stays exportable, never silently lost.
  const [unreadable, setUnreadable] = useState<string[]>(() => (restored.raw === null ? [] : [restored.raw]));
  /** The delay of the coordinator's armed next read, set where it is armed. */
  const [armed, setArmed] = useState<number | null>(null);
  const [busy, setBusy] = useState<ReadonlySet<string>>(() => new Set());
  const [monitor, setMonitor] = useState<Monitor>(freshMonitor);
  const [hidden, setHidden] = useState(() => document.visibilityState !== "visible");
  // In-flight ownership shared by automatic and manual reads, held until each
  // read settles: one summary read per target, whose result every interested
  // caller adopts, and one observation read per request.
  const summaryReads = useRef(new Map<string, Promise<Observed<CoreView>>>());
  const observing = useRef(new Set<string>());

  const screen = target.run !== null ? "run" : target.instance !== null ? "instance" : "inventory";
  const payload = presentation.working;
  // Writes need trust and a positively observed context: an ordinary service
  // (its typed 404) or a bound demo context. Pending, failed, unreadable or
  // unreachable context is never treated as ordinary.
  const contextObserved = demo?.kind === "ordinary" || demo?.kind === "demo";
  const writable = trust.state === "ready" && contextObserved;
  // Async completions check this before committing, so a late reply for an
  // earlier target can never be shown under the current one.
  const targetKey = targetHref(target);
  const targetKeyRef = useRef(targetKey);
  targetKeyRef.current = targetKey;
  const targetRef = useRef(target);
  targetRef.current = target;
  const instancesKey = `${target.home}`;
  const instanceKey = `${target.home}|${target.instance}`;
  const historyKey = `${targetKey}|${historySection}`;

  // Theme and density are document-level presentation only.
  useEffect(() => {
    document.documentElement.dataset.theme = payload.theme;
    document.documentElement.dataset.density = payload.density;
  }, [payload.theme, payload.density]);

  const resetTargetState = () => {
    setHomeCursors([null]);
    setInstanceCursors([null]);
    setRunCursors([null]);
    setSummary(null);
    setSummaryFailure(null);
    setSelection(null);
    setGraphs(new Map());
    setGraphRevision(null);
    setGraphPlane(null);
    setHistorySection(null);
    setHistoryCursors([null]);
    setHistoryPage(unkeyed());
    setInvocationPage(null);
    setInstancesPage(unkeyed());
    setInstancePage(unkeyed());
    setReportsOpen(false);
    setMonitor(freshMonitor());
  };

  // ---- the lexical intent set and this tab's history entries ---------------------

  /** Keep the set and write it into the current history entry when it fits. */
  const commitIntents = useCallback((next: Entry[]): boolean => {
    intentsRef.current = next;
    setEntries(next);
    if (!fits(next)) {
      setFlags((current) => ({ ...current, overCapacity: true }));
      return false;
    }
    try {
      window.history.replaceState(stateWith(window.history.state, next), "");
      setFlags((current) =>
        current.overCapacity || current.historyUnavailable ? { ...current, overCapacity: false, historyUnavailable: false } : current,
      );
      return true;
    } catch {
      setFlags((current) => ({ ...current, overCapacity: false, historyUnavailable: true }));
      return false;
    }
  }, []);

  const updateEntry = useCallback(
    (key: string, change: (entry: Entry) => Entry) =>
      commitIntents(intentsRef.current.map((entry) => (keyOf(entry.intent) === key ? change(entry) : entry))),
    [commitIntents],
  );

  const setBusyKey = useCallback((key: string, on: boolean) => {
    setBusy((current) => {
      const next = new Set(current);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);

  /** Every pushed entry carries the whole set (unless it no longer fits one entry). */
  const pushTarget = useCallback((href: string) => {
    const items = intentsRef.current;
    try {
      window.history.pushState(fits(items) ? stateWith(window.history.state, items) : window.history.state, "", href);
    } catch {
      setFlags((current) => ({ ...current, historyUnavailable: true }));
      window.history.pushState(null, "", href);
    }
  }, []);

  const navigate = useCallback((next: Target) => {
    pushTarget(targetHref(next));
    resetTargetState();
    setTarget(next);
    window.requestAnimationFrame(() => headingRef.current?.focus());
  }, [pushTarget]);

  useEffect(() => {
    const restore = (event: PopStateEvent) => {
      // Merge the visited entry into the set and write the merged set back to
      // it, so Back/Forward never replaces newer unresolved requests.
      const incoming = readHistory(event.state);
      if (incoming.invalid) setFlags((current) => ({ ...current, invalidRestore: true }));
      const raw = incoming.raw;
      if (raw !== null) setUnreadable((current) => (current.includes(raw) ? current : [...current, raw]));
      commitIntents(merge(intentsRef.current, incoming.kept));
      resetTargetState();
      setTarget(parseTarget(window.location.search) ?? NO_TARGET);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [commitIntents]);

  useEffect(() => {
    const change = () => setHidden(document.visibilityState !== "visible");
    document.addEventListener("visibilitychange", change);
    return () => document.removeEventListener("visibilitychange", change);
  }, []);

  // Boot: after trust, observe demo context, then shared presentation.
  const loadPresentation = useCallback(async (message: Presentation["message"] = null) => {
    const observed = await presentationState();
    if (!observed.ok) {
      setPresentation((current) => ({
        ...current,
        snapshot: null,
        problem: `saved presentations unavailable (${observed.code})`,
        message,
      }));
      return null;
    }
    try {
      const snapshot = readSnapshot(observed.text);
      setPresentation((current) => ({ ...current, snapshot, ...adoptDefault(snapshot), message }));
      return snapshot;
    } catch (error) {
      const code = error instanceof PreferenceError ? error.code : "invalid_response";
      setPresentation((current) => ({ ...current, snapshot: null, problem: `saved presentations unreadable (${code})`, message }));
      return null;
    }
  }, []);

  useEffect(() => {
    if (trust.state !== "ready" || booted) return;
    let current = true;
    void (async () => {
      const mode = await demoContext();
      if (!current) return;
      setDemo(mode);
      await loadPresentation();
      const operations = await capabilities();
      if (!current) return;
      // Name optional features this service cannot serve, before any panel reads.
      setUnavailable(operations.ok ? operations.value.filter((item) => !item.available && item.operationId in OPTIONAL_FEATURES) : []);
      setBooted(true);
    })();
    return () => {
      current = false;
    };
  }, [trust.state, booted, loadPresentation]);

  /** The target's one in-flight summary read, started only if none is in flight. */
  const readSummary = useCallback((selected: Target): Promise<Observed<CoreView>> => {
    const key = targetHref(selected);
    const pending = summaryReads.current.get(key);
    if (pending !== undefined) return pending;
    const read = runSummary(selected).finally(() => summaryReads.current.delete(key));
    summaryReads.current.set(key, read);
    return read;
  }, []);

  /** Show a summary result only while its target is still selected. */
  const applySummary = useCallback((key: string, observed: Observed<CoreView>): ReadOutcome => {
    if (key !== targetKeyRef.current) return "ok";
    setSummaryLoading(false);
    if (observed.ok) {
      setSummary({ view: observed.value, observedAt: observed.observedAt });
      setSummaryFailure(null);
      return "ok";
    }
    setSummaryFailure(observed);
    return observed.code === "core_inspection_too_large" ? "budget" : "failed";
  }, []);

  // One sequential read sequence for the selected screen.
  useEffect(() => {
    if (!booted || (trust.state !== "ready" && trust.state !== "refused")) return;
    let current = true;
    void (async () => {
      if (screen === "inventory") {
        setHomesPage((page) => ({ ...page, loading: true }));
        const observed = await homes(homeCursors[homeCursors.length - 1]);
        if (!current) return;
        setHomesPage({ observation: observed, loading: false, page: homeCursors.length - 1 });
        if (target.home !== null) {
          const key = instancesKey;
          setInstancesPage((page) => ({ ...page, loading: true }));
          const page = await instances(target.home, instanceCursors[instanceCursors.length - 1]);
          if (current) setInstancesPage({ observation: page, loading: false, page: instanceCursors.length - 1, key });
        }
      } else if (screen === "instance" && target.home !== null && target.instance !== null) {
        const key = instanceKey;
        setInstancePage((page) => ({ ...page, loading: true }));
        const page = await instance(target.home, target.instance, runCursors[runCursors.length - 1]);
        if (current) setInstancePage({ observation: page, loading: false, page: runCursors.length - 1, key });
      } else if (screen === "run") {
        // Refresh or a return to this target shares a read already in flight;
        // its result is shown if this target is still selected, whichever
        // effect started it.
        setSummaryLoading(true);
        applySummary(targetHref(target), await readSummary(target));
      }
    })();
    return () => {
      current = false;
    };
  }, [booted, trust.state, screen, target, homeCursors, instanceCursors, runCursors, refreshEpoch, instancesKey, instanceKey, readSummary, applySummary]);

  const data = summary?.view.data ?? null;
  const activeRevision = text(data?.active_revision_id);
  const shownRevision = graphRevision ?? activeRevision;
  const graphEntry = shownRevision === null ? undefined : graphs.get(shownRevision);
  // Parsed once per loaded revision, so layout never reruns on unrelated renders.
  const graph: CapturedGraph | null = useMemo(
    () =>
      graphEntry !== undefined && graphEntry !== "loading" && graphEntry.ok ? parseCapturedGraph(graphEntry.value.data) : null,
    [graphEntry],
  );

  const loadGraph = useCallback(
    async (revisionId: string) => {
      const key = targetKeyRef.current;
      setGraphs((current) => new Map(current).set(revisionId, "loading"));
      const observed = await revisionGraph(target, revisionId);
      // The same revision ID can belong to another run; keep only this target's read.
      if (targetKeyRef.current === key) setGraphs((current) => new Map(current).set(revisionId, observed));
    },
    [target],
  );

  // Opening Graph (or changing its revision) reads that captured revision once.
  const graphOpen = screen === "run" && payload.panels.graph;
  useEffect(() => {
    if (graphOpen && shownRevision !== null && !graphs.has(shownRevision)) void loadGraph(shownRevision);
  }, [graphOpen, shownRevision, graphs, loadGraph]);

  // A history page loads only after a section is chosen and while History is open.
  useEffect(() => {
    if (screen !== "run" || historySection === null || !payload.panels.history) return;
    let current = true;
    const key = historyKey;
    setHistoryPage((page) => ({ ...page, loading: true }));
    void runHistory(target, historySection, historyCursors[historyCursors.length - 1]).then((observed) => {
      if (current) setHistoryPage({ observation: observed, loading: false, page: historyCursors.length - 1, key });
    });
    return () => {
      current = false;
    };
  }, [screen, target, historySection, historyCursors, payload.panels.history, refreshEpoch, historyKey]);

  // Rows of another section or target never render as this section.
  const shownHistory = bound(historyPage, historyKey);
  const historyRows =
    historySection === "invocations" && shownHistory.observation?.ok
      ? shownHistory.observation.value.items.map((item) => record(item) ?? {})
      : [];

  const reveal = (panel: PanelId) => {
    // An in-memory presentation change: nothing is saved or sent.
    if (!payload.panels[panel]) {
      setPresentation((current) => ({
        ...current,
        working: { ...current.working, panels: { ...current.working.panels, [panel]: true } },
      }));
    }
  };

  const selectNode = (planeId: string, nodeId: string) => {
    setSelection({ kind: "node", planeId, nodeId, revisionId: shownRevision });
    setGraphPlane(planeId);
    reveal("inspector");
  };

  const selectInvocation = async (invocationId: string) => {
    setSelection({ kind: "invocation", invocationId });
    reveal("inspector");
    if (historyRows.some((row) => row.invocation_id === invocationId)) {
      setInvocationPage(null);
      return;
    }
    const key = `${targetKey}|${invocationId}`;
    setInvocationPage({ observation: null, loading: true, page: 0, key });
    const observed = await invocation(target, invocationId);
    // A reply for an earlier selection (A then B) never replaces the latest one.
    setInvocationPage((current) => (current?.key === key ? { ...current, observation: observed, loading: false } : current));
  };
  const invocationKey = selection?.kind === "invocation" ? `${targetKey}|${selection.invocationId}` : null;
  const shownInvocation = invocationPage !== null && invocationPage.key === invocationKey ? invocationPage : null;
  const shownInstances = bound(instancesPage, instancesKey);
  const shownInstance = bound(instancePage, instanceKey);

  const page = (setCursors: (update: (cursors: (string | null)[]) => (string | null)[]) => void, next: string | null) =>
    (direction: 1 | -1) =>
      setCursors((cursors) =>
        direction === 1 && next !== null ? [...cursors, next] : direction === -1 && cursors.length > 1 ? cursors.slice(0, -1) : cursors,
      );

  // ---- control requests --------------------------------------------------------

  const controlsUnavailable = unavailable.some(
    (item) => item.operationId === "router.request.submit" || item.operationId === "router.views.request",
  );

  /** One summary read for the coordinator; skipped while another is in flight. */
  const refreshSummary = useCallback(async (): Promise<ReadOutcome> => {
    const selected = targetRef.current;
    if (selected.run === null) return "ok";
    setSummaryLoading(true);
    return applySummary(targetHref(selected), await readSummary(selected));
  }, [readSummary, applySummary]);

  /** Observe one request by exact ID and its known target, through the strict view only. */
  const observe = useCallback(
    async (key: string): Promise<ReadOutcome> => {
      const entry = intentsRef.current.find((item) => keyOf(item.intent) === key);
      if (entry === undefined || entry.integrity !== null || observing.current.has(key)) return "ok";
      const envelope = envelopeOf(entry.intent);
      const run = envelope.operation === "start_run" ? (entry.intent.binding?.runId ?? null) : envelope.run_id;
      observing.current.add(key);
      setBusyKey(key, true);
      const result = await observeRequest(entry.intent.home, entry.intent.requestId, envelope.instance_id, run);
      observing.current.delete(key);
      setBusyKey(key, false);
      updateEntry(key, (current) => applyReply(current, result, false));
      if (result.ok || result.status === 404) return "ok";
      return result.code === "core_inspection_too_large" ? "budget" : "failed";
    },
    [setBusyKey, updateEntry],
  );

  /** Send exactly these retained bytes. An unreadable answer leaves delivery unknown. */
  const dispatch = useCallback(
    async (key: string, body: string) => {
      setBusyKey(key, true);
      const result = await submitRequest(body);
      setBusyKey(key, false);
      updateEntry(key, (entry) => applyReply(entry, result, true));
      // The keyed ledger always takes the reply. Monitoring is the selected
      // target's own: a reply never restarts its window, backoff or halt, and
      // only a request for the target still selected gets its first read after 1 s.
      const entry = intentsRef.current.find((item) => keyOf(item.intent) === key);
      const selected = targetRef.current;
      if (
        entry !== undefined &&
        selected.home !== null &&
        selected.instance !== null &&
        sameTarget(entry, selected.home, selected.instance, selected.run)
      ) {
        setMonitor((current) => (current.halted === null ? { ...current, fast: true } : current));
      }
    },
    [setBusyKey, updateEntry],
  );

  const sendReviewed = useCallback(
    async (reviewed: Reviewed, unprotected: boolean): Promise<SendResult> => {
      const home = targetRef.current.home;
      const envelope = reviewed.envelope;
      if (home === null) return { sent: false, reason: "No home is selected." };
      const waiting = outstanding(intentsRef.current, envelope.operation, home, envelope.instance_id, envelope.run_id);
      if (waiting !== null) return { sent: false, reason: waiting };
      const intent = intentFromBuilt(home, reviewed);
      const before = intentsRef.current;
      const next = [...before, newEntry(intent, "sending")];
      if (!fits(next)) {
        return {
          sent: false,
          reason: "This tab already holds as many requests as one history entry keeps (20 or 64 KiB). Export and retire some first.",
        };
      }
      // The exact bytes enter this tab's history before any network dispatch.
      if (!commitIntents(next) && !unprotected) {
        commitIntents(before);
        return {
          sent: false,
          reason: "This tab's history cannot hold the request, so a reload would lose its replay bytes. Send anyway only if you accept that.",
          unprotected: true,
        };
      }
      await dispatch(keyOf(intent), intent.body);
      return { sent: true };
    },
    [commitIntents, dispatch],
  );

  const requestActions = useMemo(
    () => ({
      observe: (key: string) => void observe(key),
      replay: (key: string) => {
        const entry = intentsRef.current.find((item) => keyOf(item.intent) === key);
        if (entry === undefined || entry.conflicts.length > 0 || entry.integrity !== null) return;
        updateEntry(key, (current) => ({ ...current, delivery: "sending", answer: null, intent: { ...current.intent, sent: true } }));
        void dispatch(key, entry.intent.body);
      },
      retire: (key: string) => commitIntents(intentsRef.current.filter((entry) => keyOf(entry.intent) !== key)),
      openRun: (home: string, instanceId: string, runId: string) => navigate({ home, instance: instanceId, run: runId }),
    }),
    [observe, updateEntry, dispatch, commitIntents, navigate],
  );

  // ---- the selected-target coordinator --------------------------------------------

  const runData = summary?.view.data ?? null;
  const terminalQuiet = runData !== null && TERMINAL.has(text(runData.status) ?? "") && runData.quiescent === true;
  const watchSummary = booted && screen === "run" && summary !== null && !terminalQuiet;
  const watchedKeys =
    target.home === null || target.instance === null
      ? []
      : entries
          .filter(
            (entry) =>
              !resolved(entry) &&
              !entry.halted &&
              entry.integrity === null &&
              entry.delivery !== "sending" &&
              sameTarget(entry, target.home as string, target.instance as string, target.run),
          )
          .map((entry) => keyOf(entry.intent));
  const watchedKey = watchedKeys.join(",");
  const watching = watchSummary || watchedKeys.length > 0;

  // Finite and sequential: one read in flight, summary every 2 s and receipts
  // at 1 s then 2 s, five-minute windows, no reads while hidden, failures back
  // off 5/15/30 s then stop with Retry, and a budget refusal stops at once.
  useEffect(() => {
    if (!booted || trust.state !== "ready" || monitor.halted !== null || hidden || !watching) {
      setArmed(null);
      return;
    }
    if (Date.now() - monitor.since >= WINDOW_MS) {
      setArmed(null);
      setMonitor((current) => ({ ...current, halted: "window" }));
      return;
    }
    const receipts = watchedKey === "" ? [] : watchedKey.split(",");
    const delay = nextDelay(monitor.errors, receipts.length > 0 ? (monitor.fast ? RECEIPT_FIRST_MS : RECEIPT_MS) : SUMMARY_MS);
    if (delay === null) {
      setArmed(null);
      setMonitor((current) => ({ ...current, halted: "errors" }));
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (document.visibilityState !== "visible") return; // the hidden-tab effect rearms on return
      if (Date.now() - monitor.since >= WINDOW_MS) {
        setMonitor((current) => ({ ...current, halted: "window" }));
        return;
      }
      void (async () => {
        // A summary budget refusal (only reported while its target is selected)
        // stops this target's reads at once: no receipt read follows, and a
        // cleaned-up batch still records it. A receipt's refusal stops that request.
        const summaryOutcome = watchSummary ? await refreshSummary() : "ok";
        if (summaryOutcome === "budget") {
          setMonitor((current) => ({ ...current, halted: "budget" }));
          return;
        }
        const outcomes: ReadOutcome[] = [summaryOutcome];
        for (const key of receipts) {
          if (cancelled) return;
          outcomes.push(await observe(key));
        }
        if (cancelled) return;
        setMonitor((current) => ({
          ...current,
          errors: outcomes.includes("failed") ? current.errors + 1 : 0,
          fast: false,
          beat: current.beat + 1,
        }));
      })();
    }, delay);
    setArmed(delay);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [booted, trust.state, monitor, hidden, watching, watchSummary, watchedKey, refreshSummary, observe]);

  const monitorState: MonitorState = !booted
    ? { kind: "idle" }
    : monitor.halted !== null
      ? { kind: "stopped", why: monitor.halted }
      : watching && hidden
        ? { kind: "hidden" }
        : watching
          ? { kind: "watching", until: monitor.since + WINDOW_MS, nextIn: armed, failures: monitor.errors }
          : screen === "run" && terminalQuiet
            ? { kind: "stopped", why: "terminal" }
            : { kind: "idle" };

  // ---- control forms --------------------------------------------------------------

  const activeEntry = activeRevision === null ? undefined : graphs.get(activeRevision);
  const activeGraph = useMemo(
    () =>
      activeEntry !== undefined && activeEntry !== "loading" && activeEntry.ok ? parseCapturedGraph(activeEntry.value.data) : null,
    [activeEntry],
  );
  const activeNodes = useMemo(
    () =>
      activeGraph === null
        ? null
        : Object.fromEntries(activeGraph.planes.map((item): [string, string[]] => [item.planeId, item.nodes.map((node) => node.id)])),
    [activeGraph],
  );
  const needActiveGraph = useCallback(() => {
    if (activeRevision !== null && !graphs.has(activeRevision)) void loadGraph(activeRevision);
  }, [activeRevision, graphs, loadGraph]);

  const formDisabled = !writable
    ? "Sending needs browser trust and an observed service context."
    : controlsUnavailable
      ? "Control requests or their observation are not available on this service."
      : flags.overCapacity
        ? "Export and retire requests first: this tab holds more than one history entry keeps."
        : null;
  const outstandingFor = (operation: Operation) =>
    target.home === null || target.instance === null
      ? null
      : outstanding(entries, operation, target.home, target.instance, operation === "start_run" ? null : target.run);

  let controlTarget: ControlTarget | null = null;
  if (target.home !== null && target.instance !== null && screen === "run" && runData !== null) {
    const version = integer(runData.state_version);
    const pending = record(runData.pending_candidate);
    const chosen = selection?.kind === "invocation" ? selection.invocationId : null;
    const row =
      chosen === null
        ? null
        : (historyRows.find((item) => item.invocation_id === chosen) ??
          (shownInvocation?.observation?.ok ? record(shownInvocation.observation.value.items[0]) : null));
    controlTarget = {
      home: target.home,
      instanceId: target.instance,
      runId: target.run,
      // A failed latest read is not a fresh observation; a reviewed request keeps its own version.
      version: version !== null && version >= 1 && summaryFailure === null && summary !== null ? { value: version, observedAt: summary.observedAt } : null,
      planes: planesOf(runData).map(([id]) => id),
      candidateId: pending !== null && pending.diagnostic === null ? text(pending.candidate_id) : null,
      activeNodes,
      activeGraph: activeEntry === undefined ? "absent" : activeEntry === "loading" ? "loading" : activeNodes !== null ? "loaded" : "failed",
      selectedInvocation: chosen === null ? null : { invocationId: chosen, planeId: text(row?.plane_id) },
    };
  } else if (target.home !== null && target.instance !== null && screen === "instance" && shownInstance.observation?.ok) {
    const settings = integer(shownInstance.observation.value.data.settings_version);
    controlTarget = {
      home: target.home,
      instanceId: target.instance,
      runId: null,
      version: settings !== null && settings >= 1 ? { value: settings, observedAt: shownInstance.observation.observedAt } : null,
      planes: [],
      candidateId: null,
      activeNodes: null,
      activeGraph: "absent",
      selectedInvocation: null,
    };
  }
  const controls =
    controlTarget === null ? null : (
      <ControlForm
        key={targetKey}
        target={controlTarget}
        disabled={formDisabled}
        outstanding={outstandingFor}
        onNeedGraph={needActiveGraph}
        onSend={sendReviewed}
      />
    );

  // ---- presentation actions --------------------------------------------------

  type Adopt = (snapshot: Snapshot, before: Snapshot, intent: Intent) => void;

  const commit = async (make: (snapshot: Snapshot) => WriteBody, done: string, adopt?: Adopt) => {
    const before = presentation.snapshot;
    if (before === null || !writable) return;
    // The exact choice and layout this write was issued for. Its reply updates
    // only what was sent: edits made while it is pending stay unsaved, and a
    // different choice made meanwhile is never replaced.
    const intent: Intent = { ref: presentation.active.ref, sent: copyPayload(presentation.working) };
    let body: WriteBody;
    try {
      body = make(before);
    } catch (error) {
      const code = error instanceof PreferenceError ? error.message : "invalid_request";
      setPresentation((current) => ({ ...current, message: { tone: "error", text: `Not saved: ${code}.` } }));
      return;
    }
    setPresentation((current) => ({ ...current, busy: true, message: null }));
    const observed = await putPresentation(body.text);
    if (observed.ok) {
      const snapshot = safeSnapshot(observed.text);
      if (snapshot === null) {
        await loadPresentation({ tone: "warn", text: `${done} The returned state was unreadable; reloaded.` });
        setPresentation((current) => ({ ...current, busy: false }));
        return;
      }
      setPresentation((current) => ({ ...current, snapshot, busy: false, message: { tone: "info", text: done } }));
      adopt?.(snapshot, before, intent);
      return;
    }
    if (observed.code === "presentation_conflict") {
      setPresentation((current) => ({
        ...current,
        busy: false,
        message: {
          tone: "warn",
          text: "Not saved: saved presentations changed in another client. Reload them to review; your unsaved layout is kept.",
        },
      }));
      return;
    }
    if (observed.code === "presentation_write_uncertain" || observed.status === 0) {
      // The write may be visible. Re-read and compare before saying anything.
      const fresh = await presentationState();
      const snapshot = fresh.ok ? safeSnapshot(fresh.text) : null;
      const visible = snapshot !== null && writeVisible(body, before, snapshot);
      setPresentation((current) => ({
        ...current,
        snapshot: snapshot ?? current.snapshot,
        busy: false,
        message: visible
          ? { tone: "info", text: `${done} (confirmed by re-reading after an uncertain response).` }
          : { tone: "warn", text: `Save outcome unknown (${observed.code}); the re-read does not show it. Reload to review.` },
      }));
      if (visible && snapshot !== null) adopt?.(snapshot, before, intent);
      return;
    }
    setPresentation((current) => ({
      ...current,
      busy: false,
      message: { tone: "error", text: `Not saved: ${observed.code}.` },
    }));
  };

  const adoptRef = (ref: Ref) => {
    const snapshot = presentation.snapshot;
    if (snapshot === null) return;
    try {
      const chosen = resolveRef(snapshot, ref);
      setPresentation((current) => ({
        ...current,
        active: { ref, name: chosen.name },
        saved: copyPayload(chosen.payload),
        working: copyPayload(chosen.payload),
        message: null,
      }));
    } catch {
      setPresentation((current) => ({ ...current, message: { tone: "error", text: "That saved presentation no longer exists." } }));
    }
  };

  /** Apply a reply's adoption only while its initiating choice is still current. */
  const whileCurrent = (intent: Intent, change: (current: Presentation) => Presentation) =>
    setPresentation((current) => (sameRef(current.active.ref, intent.ref) ? change(current) : current));

  const adoptCreated: Adopt = (snapshot, before, intent) => {
    const known = new Set(before.stock?.configs.map((config) => config.id) ?? []);
    const created = snapshot.stock?.configs.find((config) => !known.has(config.id));
    if (created !== undefined) {
      whileCurrent(intent, (current) => ({
        ...current,
        active: { ref: { kind: "user", id: created.id }, name: created.name },
        saved: copyPayload(created.payload),
      }));
    }
  };

  /** The server's stored payload for one saved record, if the reply has it. */
  const storedPayload = (snapshot: Snapshot, id: string) =>
    snapshot.stock?.configs.find((config) => config.id === id)?.payload ?? null;

  const actions = {
    saveAs: (name: string) =>
      void commit((snapshot) => createConfig(snapshot, name, payload), `Saved “${name}”.`, adoptCreated),
    save: () => {
      const ref = presentation.active.ref;
      if (ref?.kind !== "user") return;
      void commit((snapshot) => updateConfig(snapshot, ref.id, payload), `Saved “${presentation.active.name}”.`, (snapshot, _, intent) => {
        // The saved baseline is what the service stored, never later local edits.
        const stored = storedPayload(snapshot, ref.id) ?? intent.sent;
        whileCurrent(intent, (current) => ({ ...current, saved: copyPayload(stored) }));
      });
    },
    rename: (name: string) => {
      const ref = presentation.active.ref;
      if (ref?.kind !== "user") return;
      void commit((snapshot) => renameConfig(snapshot, ref.id, name), `Renamed to “${name}”.`, (_, __, intent) =>
        whileCurrent(intent, (current) => ({ ...current, active: { ...current.active, name } })),
      );
    },
    duplicate: (name: string) => {
      const ref: Ref = presentation.active.ref ?? { kind: "bundled", id: payload.preset };
      void commit((snapshot) => duplicateConfig(snapshot, ref, name), `Duplicated as “${name}”.`, adoptCreated);
    },
    remove: () => {
      const ref = presentation.active.ref;
      if (ref?.kind !== "user") return;
      void commit((snapshot) => deleteConfig(snapshot, ref.id), `Deleted “${presentation.active.name}”.`, (snapshot, before, intent) => {
        // The deleted record's stored preset becomes the active bundled base.
        const preset = storedPayload(before, ref.id)?.preset ?? intent.sent.preset;
        const base = snapshot.bundled.find((item) => item.id === preset);
        whileCurrent(intent, (current) => ({
          ...current,
          active: { ref: { kind: "bundled", id: preset }, name: base?.name ?? PRESET_LABELS[preset] },
          saved: base === undefined ? current.saved : copyPayload(base.payload),
        }));
      });
    },
    makeDefault: () => {
      const ref: Ref = presentation.active.ref ?? { kind: "bundled", id: payload.preset };
      void commit((snapshot) => setDefaultConfig(snapshot, ref), `“${presentation.active.name}” is the default for the next page load.`);
    },
    reset: () => {
      const ref = presentation.active.ref;
      if (ref?.kind !== "user") return;
      void commit((snapshot) => resetConfig(snapshot, ref.id), `Reset “${presentation.active.name}” to its preset.`, (snapshot, _, intent) => {
        const stored = storedPayload(snapshot, ref.id);
        if (stored === null) return;
        // The reset layout replaces the working layout only if nothing changed while pending.
        whileCurrent(intent, (current) => ({
          ...current,
          saved: copyPayload(stored),
          working: samePayload(current.working, intent.sent) ? copyPayload(stored) : current.working,
        }));
      });
    },
    exportActive: () => {
      const snapshot = presentation.snapshot;
      if (snapshot === null) return;
      const ref: Ref = presentation.active.ref ?? { kind: "bundled", id: payload.preset };
      try {
        saveText(exportConfig(snapshot, ref), `millrace-presentation-${presentation.active.name.replace(/[^A-Za-z0-9._-]+/g, "-")}.json`);
      } catch (error) {
        setPresentation((current) => ({
          ...current,
          message: { tone: "error", text: `Not exported: ${error instanceof PreferenceError ? error.code : "invalid_request"}.` },
        }));
      }
    },
    importFile: async (file: File) => {
      if (file.size > IMPORT_LIMIT) {
        setPresentation((current) => ({ ...current, message: { tone: "error", text: "Not imported: the file exceeds 1 MiB." } }));
        return;
      }
      const source = await file.text();
      void commit((snapshot) => importConfig(snapshot, source), `Imported “${file.name}” as a new saved presentation.`, adoptCreated);
    },
    replace: async () => {
      const snapshot = presentation.snapshot;
      if (snapshot === null || !writable) return;
      setPresentation((current) => ({ ...current, busy: true, message: null }));
      const observed = await putPresentation(replaceUnreadable(snapshot));
      await loadPresentation(
        observed.ok
          ? { tone: "info", text: "Replaced the unreadable file with an empty store." }
          : { tone: "error", text: `Not replaced: ${observed.code}.` },
      );
      setPresentation((current) => ({ ...current, busy: false }));
    },
    reload: () => void loadPresentation({ tone: "info", text: "Reloaded saved presentations." }),
    revert: () => setPresentation((current) => ({ ...current, working: copyPayload(current.saved), message: null })),
    adopt: adoptRef,
    change: (next: StockPayload) => setPresentation((current) => ({ ...current, working: next })),
  };

  // ---- render ----------------------------------------------------------------

  const title =
    screen === "run" ? "Run" : screen === "instance" ? "Instance" : target.home !== null ? "Home inventory" : "State homes";
  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">Millrace OS</div>
        <nav aria-label="Target" className="breadcrumbs">
          <TargetLink target={NO_TARGET} navigate={navigate}>
            Homes
          </TargetLink>
          {target.home !== null ? (
            <>
              <span aria-hidden="true">/</span>
              <TargetLink target={{ home: target.home, instance: null, run: null }} navigate={navigate}>
                <code>{target.home}</code>
              </TargetLink>
            </>
          ) : null}
          {target.instance !== null ? (
            <>
              <span aria-hidden="true">/</span>
              <TargetLink target={{ ...target, run: null }} navigate={navigate}>
                <code className="id">{target.instance}</code>
              </TargetLink>
            </>
          ) : null}
          {target.run !== null ? (
            <>
              <span aria-hidden="true">/</span>
              <code className="id" aria-current="page">
                {target.run}
              </code>
            </>
          ) : null}
        </nav>
        <PresentationMenu presentation={presentation} writable={writable} actions={actions} />
      </header>
      <main className="app__main">
        <div className="region__head">
          <h1 ref={headingRef} tabIndex={-1}>
            {title}
          </h1>
          <button type="button" onClick={() => setRefreshEpoch((epoch) => epoch + 1)} disabled={!booted}>
            Refresh
          </button>
        </div>
        <Notices
          trust={trust}
          demo={demo}
          targetValid={targetValid}
          presentation={presentation}
          target={target}
          unavailable={unavailable}
        />
        <MonitorStatus state={monitorState} onContinue={() => setMonitor(freshMonitor())} />
        <RequestArea
          entries={entries}
          flags={flags}
          busy={busy}
          writable={writable}
          actions={requestActions}
          unreadable={unreadable}
        />
        {!booted ? (
          <p className="muted" role="status">
            {trust.state === "pending" ? "Establishing browser trust…" : trust.state === "ready" ? "Reading service context…" : ""}
          </p>
        ) : screen === "inventory" ? (
          <Inventory
            target={target}
            homes={homesPage}
            instances={target.home === null ? null : shownInstances}
            onPage={(table, direction) => {
              const observed = table === "homes" ? homesPage.observation : shownInstances.observation;
              const next = observed?.ok ? observed.value.nextCursor : null;
              page(table === "homes" ? setHomeCursors : setInstanceCursors, next)(direction);
            }}
            navigate={navigate}
          />
        ) : screen === "instance" ? (
          <>
            <Instance
              target={target}
              page={shownInstance}
              onPage={page(setRunCursors, shownInstance.observation?.ok ? shownInstance.observation.value.nextCursor : null)}
              navigate={navigate}
            />
            {controls}
          </>
        ) : (
          <RunScreen
            target={target}
            summary={summary}
            summaryFailure={summaryFailure}
            summaryLoading={summaryLoading}
            demo={demo}
            narrow={narrow}
            payload={payload}
            onSplit={(ratio) => actions.change({ ...payload, split_ratios: { ...payload.split_ratios, [payload.arrangement]: ratio } })}
            reveal={reveal}
            selection={selection}
            onClearSelection={() => setSelection(null)}
            onSelectNode={selectNode}
            onSelectInvocation={(id) => void selectInvocation(id)}
            graph={graph}
            graphEntry={graphEntry}
            shownRevision={shownRevision}
            onShowRevision={(revisionId) => {
              setGraphRevision(revisionId);
              reveal("graph");
            }}
            onLoadGraph={() => shownRevision !== null && void loadGraph(shownRevision)}
            graphPlane={graphPlane}
            onPlane={setGraphPlane}
            historySection={historySection}
            onHistorySection={(section) => {
              setHistorySection(section);
              setHistoryCursors([null]);
            }}
            historyPage={shownHistory}
            onHistoryPage={page(setHistoryCursors, shownHistory.observation?.ok ? shownHistory.observation.value.nextCursor : null)}
            historyRows={historyRows}
            invocationPage={shownInvocation}
            reportsOpen={reportsOpen}
            onOpenReports={() => setReportsOpen(true)}
            controls={controls}
            canValidate={writable && !unavailable.some((item) => item.operationId === "router.views.validate")}
          />
        )}
      </main>
      <footer className="app__footer">
        <a href="/ui/stock/THIRD_PARTY_NOTICES.txt" target="_blank" rel="noopener noreferrer">
          Third-party notices
        </a>
        <span>Recovery diagnostics: run <code>millrace-os open --recovery</code>.</span>
      </footer>
    </div>
  );
}

// ---- notices -------------------------------------------------------------------

function openCommand(target: Target): string {
  const parts = ["millrace-os open"];
  if (target.home !== null) parts.push(`--home ${target.home}`);
  if (target.instance !== null) parts.push(`--instance ${target.instance}`);
  if (target.run !== null) parts.push(`--run ${target.run}`);
  return parts.join(" ");
}

function Notices({
  trust,
  demo,
  targetValid,
  presentation,
  target,
  unavailable,
}: {
  trust: Trust;
  demo: DemoMode | null;
  targetValid: boolean;
  presentation: Presentation;
  target: Target;
  unavailable: Capability[];
}) {
  const items: { tone: "info" | "warn" | "error"; content: ReactNode }[] = [];
  for (const item of unavailable) {
    items.push({
      tone: "warn",
      content: `${OPTIONAL_FEATURES[item.operationId] ?? item.operationId} unavailable on this service (${item.reason ?? "unknown"}).`,
    });
  }
  if (!targetValid) items.push({ tone: "error", content: "The address had an invalid target; showing state homes instead." });
  if (trust.state === "expired") {
    items.push({
      tone: "error",
      content: (
        <>
          {trust.reason === "nonce"
            ? "This sign-in link was already used or has expired."
            : "The browser session ended (expired, credential rotated or service restarted)."}{" "}
          Reads and writes are stopped. Run <code>{openCommand(target)}</code> for a new session.
        </>
      ),
    });
  }
  if (trust.state === "refused") {
    items.push({
      tone: "error",
      content: (
        <>
          Browser trust refused: <code>{trust.code}</code>. Writes are disabled. Run <code>{openCommand(target)}</code> again.
        </>
      ),
    });
  }
  if (demo?.kind === "pending" || demo?.kind === "unknown") {
    items.push({
      tone: "warn",
      content: `Service context is ${demo.kind === "pending" ? "pending" : `unknown (${demo.code})`}; saving presentations and every control stay disabled until it is observed.`,
    });
  }
  if (demo?.kind === "demo") {
    items.push({ tone: "info", content: "This is an owned demo service; its coordinator owns the run lifecycle." });
  }
  if (presentation.problem !== null) {
    items.push({ tone: "warn", content: `Using Precision in memory: ${presentation.problem}. Nothing is written automatically.` });
  }
  if (items.length === 0) return null;
  return (
    <div className="notices" role="status" aria-live="polite">
      {items.map((item, index) => (
        <p key={index} className={`notice notice--${item.tone}`}>
          {item.content}
        </p>
      ))}
    </div>
  );
}

// ---- run screen -----------------------------------------------------------------

interface RunScreenProps {
  target: Target;
  summary: Summary | null;
  summaryFailure: Failure | null;
  summaryLoading: boolean;
  demo: DemoMode | null;
  narrow: boolean;
  payload: StockPayload;
  onSplit: (ratio: number) => void;
  reveal: (panel: PanelId) => void;
  selection: Selection | null;
  onClearSelection: () => void;
  onSelectNode: (planeId: string, nodeId: string) => void;
  onSelectInvocation: (invocationId: string) => void;
  graph: CapturedGraph | null;
  graphEntry: Observed<CoreView> | "loading" | undefined;
  shownRevision: string | null;
  onShowRevision: (revisionId: string) => void;
  onLoadGraph: () => void;
  graphPlane: string | null;
  onPlane: (planeId: string) => void;
  historySection: HistorySection | null;
  onHistorySection: (section: HistorySection) => void;
  historyPage: Paged<CoreView>;
  onHistoryPage: (direction: 1 | -1) => void;
  historyRows: Record<string, unknown>[];
  invocationPage: Paged<CoreView> | null;
  reportsOpen: boolean;
  onOpenReports: () => void;
  /** The exact control form, after Status: never a hideable panel. */
  controls: ReactNode;
  canValidate: boolean;
}

const LAZY_FALLBACK = (
  <p className="muted" role="status">
    Loading view…
  </p>
);

function RunScreen(props: RunScreenProps) {
  const { payload, summary } = props;
  const data = summary?.view.data ?? null;
  const panels: Record<PanelId, ReactNode> = {
    planes: data === null ? null : <PlanesRegion summary={data} onSelectNode={props.onSelectNode} onSelectInvocation={props.onSelectInvocation} />,
    "current-work": data === null ? null : <CurrentWorkRegion summary={data} />,
    graph: <GraphRegion {...props} />,
    inspector: (
      <Inspector
        selection={props.selection}
        graph={props.selection?.kind === "node" && props.selection.revisionId === props.shownRevision ? props.graph : null}
        invocation={props.invocationPage}
        pageRows={props.historyRows}
        onClear={props.onClearSelection}
      />
    ),
    history: (
      <Suspense fallback={LAZY_FALLBACK}>
        <History
          section={props.historySection}
          page={props.historyPage}
          onSection={props.onHistorySection}
          onPage={props.onHistoryPage}
          onSelectInvocation={props.onSelectInvocation}
        />
      </Suspense>
    ),
    revisions:
      data === null ? null : (
        <Suspense fallback={LAZY_FALLBACK}>
          <Revisions
            summary={data}
            shownRevision={props.shownRevision}
            onShowGraph={props.onShowRevision}
            target={props.target}
            canValidate={props.canValidate}
          />
        </Suspense>
      ),
  };
  const visible = (panel: PanelId) => payload.panels[panel] && panels[panel] !== null;
  const [first, second] = SPLIT_PANELS[payload.arrangement];
  const order: Record<typeof payload.arrangement, PanelId[]> = {
    "detail-split": ["current-work", "planes", "graph", "inspector", "history", "revisions"],
    "status-stack": ["current-work", "planes", "history", "graph", "inspector", "revisions"],
    "graph-split": ["graph", "inspector", "planes", "current-work", "history", "revisions"],
  };
  const slots: ReactNode[] = [];
  for (const panel of order[payload.arrangement]) {
    if (panel === second) continue;
    if (panel === first) {
      slots.push(
        <SplitSlot
          key="split"
          narrow={props.narrow}
          ratio={payload.split_ratios[payload.arrangement]}
          arrangement={payload.arrangement}
          left={visible(first) ? panels[first] : null}
          right={visible(second) ? panels[second] : null}
          onSplit={props.onSplit}
        />,
      );
    } else if (visible(panel)) {
      slots.push(<div key={panel}>{panels[panel]}</div>);
    }
  }
  return (
    <div className={`run run--${payload.arrangement}`}>
      <StatusRegion
        target={props.target}
        summary={summary}
        latest={props.summaryFailure}
        loading={props.summaryLoading}
        demo={props.demo}
        onSelectInvocation={props.onSelectInvocation}
      />
      {summary === null && props.summaryFailure !== null ? <FailureNote failure={props.summaryFailure} what="Run summary" /> : null}
      <ActionsNav payload={payload} reveal={props.reveal} reportsOpen={props.reportsOpen} onOpenReports={props.onOpenReports} />
      {props.controls}
      {summary === null ? null : (
        <>
          {slots}
          <section className="region" id="reports" aria-labelledby="reports-entry">
            {props.reportsOpen ? (
              <Suspense fallback={LAZY_FALLBACK}>
                <Report target={props.target} />
              </Suspense>
            ) : (
              <div className="region__head">
                <h2 id="reports-entry">Reports</h2>
                <button type="button" onClick={props.onOpenReports}>
                  Read published outputs
                </button>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function ActionsNav({
  payload,
  reveal,
  reportsOpen,
  onOpenReports,
}: {
  payload: StockPayload;
  reveal: (panel: PanelId) => void;
  reportsOpen: boolean;
  onOpenReports: () => void;
}) {
  // Every inspect section stays reachable here, whatever the preset hides.
  const go = (id: string) => window.requestAnimationFrame(() => document.getElementById(id)?.focus());
  return (
    <nav className="actions" aria-label="Actions">
      <span className="actions__label">Inspect</span>
      {PANELS.map((panel) => (
        <button
          key={panel}
          type="button"
          onClick={() => {
            reveal(panel);
            go(`${panel}-heading`);
          }}
        >
          {PANEL_LABELS[panel]}
          {payload.panels[panel] ? "" : " (hidden)"}
        </button>
      ))}
      <button
        type="button"
        onClick={() => {
          onOpenReports();
          go(reportsOpen ? "reports-heading" : "reports-entry");
        }}
      >
        Reports
      </button>
    </nav>
  );
}

function SplitSlot({
  narrow,
  ratio,
  arrangement,
  left,
  right,
  onSplit,
}: {
  narrow: boolean;
  ratio: number;
  arrangement: string;
  left: ReactNode;
  right: ReactNode;
  onSplit: (ratio: number) => void;
}) {
  if (left === null || right === null || narrow) {
    return (
      <>
        {left !== null ? <div>{left}</div> : null}
        {right !== null ? <div>{right}</div> : null}
      </>
    );
  }
  return <ResizableSplit ratio={ratio} arrangement={arrangement} left={left} right={right} onSplit={onSplit} />;
}

function ResizableSplit({
  ratio,
  arrangement,
  left,
  right,
  onSplit,
}: {
  ratio: number;
  arrangement: string;
  left: ReactNode;
  right: ReactNode;
  onSplit: (ratio: number) => void;
}) {
  const group = useRef<GroupImperativeHandle | null>(null);
  const percent = Math.round(ratio * 1000) / 10;
  // Adopting another presentation moves the split; a user's own resize already
  // matches, so the group is never remounted (and keyboard focus is kept).
  useEffect(() => {
    const current = group.current?.getLayout().primary;
    if (current !== undefined && Math.abs(current - percent) > 0.05) {
      group.current?.setLayout({ primary: percent, secondary: 100 - percent });
    }
  }, [percent]);
  return (
    <Group
      key={arrangement}
      groupRef={group}
      orientation="horizontal"
      className="split"
      defaultLayout={{ primary: percent, secondary: 100 - percent }}
      onLayoutChanged={(layout, meta) => {
        // Only a user's own resize becomes the working ratio; it is saved only by Save.
        const size = layout.primary;
        if (meta.isUserInteraction && typeof size === "number" && size > 0 && size < 100) {
          onSplit(Math.round(size * 10) / 1000);
        }
      }}
    >
      <Panel id="primary" minSize="20%">
        {left}
      </Panel>
      <Separator className="split__handle" aria-label="Resize panels" />
      <Panel id="secondary" minSize="20%">
        {right}
      </Panel>
    </Group>
  );
}

function GraphRegion(props: RunScreenProps) {
  const entry = props.graphEntry;
  const data = props.summary?.view.data ?? null;
  const planes = data === null ? [] : planesOf(data);
  const planeId = props.graphPlane ?? planes[0]?.[0] ?? props.graph?.planes[0]?.planeId ?? "";
  const plane = planes.find(([id]) => id === planeId)?.[1] ?? null;
  const isActive = props.shownRevision !== null && props.shownRevision === text(data?.active_revision_id);
  const last = record(plane?.last_event);
  const lastEvent =
    isActive && last?.event_kind === "transition" && text(last.node_id) !== null && text(last.outcome) !== null
      ? { nodeId: last.node_id as string, outcome: last.outcome as string }
      : null;
  return (
    <section className="region" aria-labelledby="graph-heading">
      <div className="region__head">
        <h2 id="graph-heading" tabIndex={-1}>
          Graph
        </h2>
        <span className="muted">
          Captured revision <code className="id">{props.shownRevision ?? "unknown"}</code>
          {isActive ? " (active routing)" : ""}
        </span>
      </div>
      {entry === undefined || entry === "loading" ? (
        <p className="muted" role="status">
          {entry === "loading" ? "Reading captured graph…" : "Not read yet."}
        </p>
      ) : !entry.ok ? (
        <>
          <FailureNote failure={entry} what="Captured graph" />
          <button type="button" onClick={props.onLoadGraph}>
            Read again
          </button>
        </>
      ) : props.graph === null ? (
        <p className="notice notice--error">The captured graph does not match its frozen projection; no graph is drawn.</p>
      ) : (
        <Suspense fallback={LAZY_FALLBACK}>
          <GraphView
            graph={props.graph}
            planeId={planeId}
            onPlane={props.onPlane}
            cursor={isActive ? text(plane?.cursor) : null}
            lastEvent={lastEvent}
            selectedNode={props.selection?.kind === "node" ? props.selection.nodeId : null}
            onSelectNode={props.onSelectNode}
            colorMode={props.payload.theme}
          />
        </Suspense>
      )}
    </section>
  );
}

// ---- presentation menu ------------------------------------------------------------

type Actions = {
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

function PresentationMenu({ presentation, writable, actions }: { presentation: Presentation; writable: boolean; actions: Actions }) {
  const [name, setName] = useState("");
  const { snapshot, working, active, busy } = presentation;
  const unsaved = !samePayload(working, presentation.saved);
  const saved = snapshot?.stock?.configs ?? [];
  const isUser = active.ref?.kind === "user";
  const canWrite = writable && snapshot !== null && !busy && (snapshot.status === "ready" || snapshot.status === "absent");
  const defaultRef: Ref = snapshot?.stock?.defaultRef ?? { kind: "bundled", id: "precision" };
  const choice = active.ref === null ? "" : `${active.ref.kind}:${active.ref.id}`;
  const bundled = snapshot?.bundled ?? [];
  const named = (run: (value: string) => void) => () => {
    run(name.trim());
    setName("");
  };
  const set = <K extends keyof StockPayload>(key: K, value: StockPayload[K]) => actions.change({ ...working, [key]: value });
  return (
    <details className="layout-menu">
      <summary>
        Layout: {active.name}
        {unsaved ? " (unsaved changes)" : ""}
      </summary>
      <div className="layout-menu__body">
        <label>
          Presentation{" "}
          <select
            value={choice}
            onChange={(event) => {
              const [kind, id] = event.target.value.split(":");
              if (kind === "bundled" || kind === "user") actions.adopt({ kind, id } as Ref);
            }}
          >
            {choice === "" ? <option value="">Precision (built-in)</option> : null}
            <optgroup label="Presets">
              {(bundled.length > 0 ? bundled.map((item) => item.id) : PRESETS).map((id) => (
                <option key={id} value={`bundled:${id}`}>
                  {PRESET_LABELS[id as keyof typeof PRESET_LABELS] ?? id}
                  {defaultRef.kind === "bundled" && defaultRef.id === id ? " — default" : ""}
                </option>
              ))}
            </optgroup>
            {saved.length > 0 ? (
              <optgroup label="Saved">
                {saved.map((config) => (
                  <option key={config.id} value={`user:${config.id}`}>
                    {config.name}
                    {defaultRef.kind === "user" && defaultRef.id === config.id ? " — default" : ""}
                  </option>
                ))}
              </optgroup>
            ) : null}
          </select>
        </label>
        <fieldset>
          <legend>Theme</legend>
          {THEMES.map((theme) => (
            <label key={theme}>
              <input type="radio" name="theme" checked={working.theme === theme} onChange={() => set("theme", theme)} /> {theme}
            </label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Density</legend>
          {DENSITIES.map((density) => (
            <label key={density}>
              <input type="radio" name="density" checked={working.density === density} onChange={() => set("density", density)} />{" "}
              {density}
            </label>
          ))}
        </fieldset>
        <label>
          Arrangement{" "}
          <select
            value={working.arrangement}
            onChange={(event) => set("arrangement", event.target.value as StockPayload["arrangement"])}
          >
            {ARRANGEMENTS.map((arrangement) => (
              <option key={arrangement} value={arrangement}>
                {ARRANGEMENT_LABELS[arrangement]}
              </option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend>Optional panels</legend>
          {PANELS.map((panel) => (
            <label key={panel}>
              <input
                type="checkbox"
                checked={working.panels[panel]}
                onChange={(event) => set("panels", { ...working.panels, [panel]: event.target.checked })}
              />{" "}
              {PANEL_LABELS[panel]}
            </label>
          ))}
          <p className="muted">Status, notices and actions always stay visible.</p>
        </fieldset>
        <div className="layout-menu__actions">
          <button type="button" onClick={actions.revert} disabled={!unsaved}>
            Revert unsaved changes
          </button>
          <button type="button" onClick={actions.save} disabled={!canWrite || !isUser || !unsaved}>
            Save
          </button>
          <button type="button" onClick={actions.makeDefault} disabled={!canWrite}>
            Make default
          </button>
          <button type="button" onClick={actions.reset} disabled={!canWrite || !isUser}>
            Reset to preset
          </button>
          <button type="button" onClick={actions.remove} disabled={!canWrite || !isUser}>
            Delete
          </button>
          <button type="button" onClick={actions.exportActive} disabled={snapshot === null}>
            Export
          </button>
        </div>
        <label>
          Name{" "}
          <input value={name} maxLength={256} onChange={(event) => setName(event.target.value)} />
        </label>
        <div className="layout-menu__actions">
          <button type="button" onClick={named(actions.saveAs)} disabled={!canWrite || name.trim() === ""}>
            Save as new
          </button>
          <button type="button" onClick={named(actions.duplicate)} disabled={!canWrite || name.trim() === ""}>
            Duplicate as
          </button>
          <button type="button" onClick={named(actions.rename)} disabled={!canWrite || !isUser || name.trim() === ""}>
            Rename to
          </button>
        </div>
        <label>
          Import a saved presentation{" "}
          <input
            type="file"
            accept="application/json,.json"
            disabled={!canWrite}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file !== undefined) void actions.importFile(file);
            }}
          />
        </label>
        <div className="layout-menu__actions">
          <button type="button" onClick={actions.reload} disabled={busy}>
            Reload saved presentations
          </button>
          {snapshot !== null && (snapshot.status === "invalid" || snapshot.status === "incompatible") ? (
            <ReplaceUnreadable snapshot={snapshot} disabled={!writable || busy} onReplace={actions.replace} />
          ) : null}
        </div>
        <p className="muted" role="status">
          {snapshot === null
            ? "Saved presentations are not available; layout changes stay in this page."
            : `Saved store: ${snapshot.status}${snapshot.version ? `, version ${snapshot.version}` : ""}.`}
          {snapshot?.others.length ? ` ${snapshot.others.length} other frontend namespace(s) are kept unchanged.` : ""}
        </p>
        {presentation.message !== null ? (
          <p className={`notice notice--${presentation.message.tone}`} role="status">
            {presentation.message.text}
          </p>
        ) : null}
      </div>
    </details>
  );
}

function ReplaceUnreadable({ snapshot, disabled, onReplace }: { snapshot: Snapshot; disabled: boolean; onReplace: () => Promise<void> }) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <button type="button" disabled={disabled} onClick={() => setConfirming(true)}>
        Replace unreadable file…
      </button>
    );
  }
  return (
    <span className="confirm">
      Replace the {snapshot.status} file <code className="id">{snapshot.fileSha256}</code> with an empty store? Its bytes are
      lost.{" "}
      <button type="button" disabled={disabled} onClick={() => void onReplace().then(() => setConfirming(false))}>
        Replace
      </button>{" "}
      <button type="button" onClick={() => setConfirming(false)}>
        Cancel
      </button>
    </span>
  );
}

