// Fixed stock presentation identifiers (`millrace-stock-presentation/v1`).
//
// The service's `bundled_configs` are the authority for Precision, Command and
// Studio. `PRECISION_FALLBACK` exists only for the moments the saved store
// cannot be used (service unreachable, unreadable or newer file): the client
// then shows Precision in memory with a notice and writes nothing.
// Status, notices, unresolved requests and actions are never presentation
// data: they cannot be hidden, saved or moved by a preset.

export const STOCK_FRONTEND = "stock";
export const STOCK_FORMAT = "millrace-stock-presentation/v1";

export const PRESETS = ["precision", "command", "studio"] as const;
export const THEMES = ["system", "light", "dark"] as const;
export const DENSITIES = ["comfortable", "compact"] as const;
export const ARRANGEMENTS = ["detail-split", "status-stack", "graph-split"] as const;
/** Registered optional panels; everything else on the run screen is fixed. */
export const PANELS = ["planes", "current-work", "graph", "inspector", "history", "revisions"] as const;

export type PresetId = (typeof PRESETS)[number];
export type Theme = (typeof THEMES)[number];
export type Density = (typeof DENSITIES)[number];
export type Arrangement = (typeof ARRANGEMENTS)[number];
export type PanelId = (typeof PANELS)[number];

export interface StockPayload {
  preset: PresetId;
  theme: Theme;
  density: Density;
  arrangement: Arrangement;
  panels: Record<PanelId, boolean>;
  split_ratios: Record<Arrangement, number>;
}

export const PRESET_LABELS: Record<PresetId, string> = {
  precision: "Precision",
  command: "Command",
  studio: "Studio",
};

export const ARRANGEMENT_LABELS: Record<Arrangement, string> = {
  "detail-split": "Detail split",
  "status-stack": "Status stack",
  "graph-split": "Graph split",
};

export const PANEL_LABELS: Record<PanelId, string> = {
  planes: "Planes",
  "current-work": "Current work",
  graph: "Graph",
  inspector: "Inspector",
  history: "History",
  revisions: "Revisions",
};

/** The two panels each arrangement's single resizable split divides. */
export const SPLIT_PANELS: Record<Arrangement, readonly [PanelId, PanelId]> = {
  "detail-split": ["graph", "inspector"],
  "status-stack": ["planes", "history"],
  "graph-split": ["graph", "inspector"],
};

export const PRECISION_FALLBACK: StockPayload = {
  preset: "precision",
  theme: "system",
  density: "comfortable",
  arrangement: "detail-split",
  panels: {
    planes: true,
    "current-work": true,
    graph: true,
    inspector: true,
    history: true,
    revisions: true,
  },
  split_ratios: { "detail-split": 0.5, "status-stack": 0.5, "graph-split": 0.5 },
};

function member<T extends string>(values: readonly T[], value: unknown): value is T {
  return typeof value === "string" && (values as readonly string[]).includes(value);
}

function exactKeys(value: object, keys: readonly string[]): boolean {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

/** The strict stock payload rule of `presentation.py` (`_stock_payload`). */
export function isStockPayload(value: unknown): value is StockPayload {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  if (!exactKeys(value, ["preset", "theme", "density", "arrangement", "panels", "split_ratios"])) {
    return false;
  }
  const payload = value as Record<string, unknown>;
  const panels = payload.panels;
  const ratios = payload.split_ratios;
  return (
    member(PRESETS, payload.preset) &&
    member(THEMES, payload.theme) &&
    member(DENSITIES, payload.density) &&
    member(ARRANGEMENTS, payload.arrangement) &&
    typeof panels === "object" &&
    panels !== null &&
    !Array.isArray(panels) &&
    exactKeys(panels, PANELS) &&
    PANELS.every((name) => typeof (panels as Record<string, unknown>)[name] === "boolean") &&
    typeof ratios === "object" &&
    ratios !== null &&
    !Array.isArray(ratios) &&
    exactKeys(ratios, ARRANGEMENTS) &&
    ARRANGEMENTS.every((name) => {
      const ratio = (ratios as Record<string, unknown>)[name];
      return typeof ratio === "number" && ratio > 0 && ratio < 1;
    })
  );
}

export function copyPayload(payload: StockPayload): StockPayload {
  return {
    ...payload,
    panels: { ...payload.panels },
    split_ratios: { ...payload.split_ratios },
  };
}

export function samePayload(a: StockPayload, b: StockPayload): boolean {
  return (
    a.preset === b.preset &&
    a.theme === b.theme &&
    a.density === b.density &&
    a.arrangement === b.arrangement &&
    PANELS.every((name) => a.panels[name] === b.panels[name]) &&
    ARRANGEMENTS.every((name) => a.split_ratios[name] === b.split_ratios[name])
  );
}
