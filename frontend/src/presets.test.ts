import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ARRANGEMENT_LABELS,
  ARRANGEMENTS,
  DENSITIES,
  PANEL_LABELS,
  PANELS,
  PRECISION_FALLBACK,
  PRESET_LABELS,
  PRESETS,
  SPLIT_PANELS,
  THEMES,
  copyPayload,
  isStockPayload,
  samePayload,
} from "./presets.ts";
import type { StockPayload } from "./presets.ts";

// Fixtures mirror the bundled stock definitions in
// src/millrace_os/presentation.py (_BUNDLED). The Python module is not read
// at test time; each call returns a fresh object.

function precision(): StockPayload {
  return copyPayload(PRECISION_FALLBACK);
}

function command(): StockPayload {
  return {
    preset: "command",
    theme: "system",
    density: "compact",
    arrangement: "status-stack",
    panels: {
      planes: true,
      "current-work": true,
      graph: false,
      inspector: false,
      history: true,
      revisions: false,
    },
    split_ratios: { "detail-split": 0.5, "status-stack": 0.5, "graph-split": 0.5 },
  };
}

function studio(): StockPayload {
  return {
    preset: "studio",
    theme: "system",
    density: "comfortable",
    arrangement: "graph-split",
    panels: {
      planes: false,
      "current-work": false,
      graph: true,
      inspector: true,
      history: true,
      revisions: false,
    },
    split_ratios: { "detail-split": 0.5, "status-stack": 0.5, "graph-split": 0.65 },
  };
}

// Single-change variants of Precision. Invalid shapes are typed `unknown` so
// they can be passed to isStockPayload deliberately.

function withField(key: string, value: unknown): unknown {
  return { ...precision(), [key]: value };
}

function withoutField(key: string): unknown {
  const copy: Record<string, unknown> = { ...precision() };
  delete copy[key];
  return copy;
}

function withPanel(name: string, value: unknown): unknown {
  const base = precision();
  return { ...base, panels: { ...base.panels, [name]: value } };
}

function withoutPanel(name: string): unknown {
  const panels: Record<string, unknown> = { ...precision().panels };
  delete panels[name];
  return { ...precision(), panels };
}

function withRatio(name: string, value: unknown): unknown {
  const base = precision();
  return { ...base, split_ratios: { ...base.split_ratios, [name]: value } };
}

test("ID lists are exact and ordered", () => {
  assert.deepEqual(PRESETS, ["precision", "command", "studio"]);
  assert.deepEqual(THEMES, ["system", "light", "dark"]);
  assert.deepEqual(DENSITIES, ["comfortable", "compact"]);
  assert.deepEqual(ARRANGEMENTS, ["detail-split", "status-stack", "graph-split"]);
  assert.deepEqual(PANELS, [
    "planes",
    "current-work",
    "graph",
    "inspector",
    "history",
    "revisions",
  ]);
});

test("status, notices, actions and requests are never presentation panels", () => {
  const panels: readonly string[] = PANELS;
  for (const region of ["status", "notices", "actions", "requests"]) {
    assert.equal(panels.includes(region), false, region);
  }
  assert.deepEqual(Object.keys(PRECISION_FALLBACK.panels).sort(), [...PANELS].sort());
});

test("PRECISION_FALLBACK matches the bundled Precision payload", () => {
  assert.deepEqual(PRECISION_FALLBACK, {
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
  });
  assert.equal(isStockPayload(PRECISION_FALLBACK), true);
});

test("bundled Precision, Command and Studio payloads are stock", () => {
  assert.equal(isStockPayload(precision()), true);
  assert.equal(isStockPayload(command()), true);
  assert.equal(isStockPayload(studio()), true);
});

test("isStockPayload rejects non-stock and malformed payloads", () => {
  const rejected: Array<[string, unknown]> = [
    ["null", null],
    ["array", []],
    ["extra top-level key", withField("extra", true)],
    ["missing split_ratios", withoutField("split_ratios")],
    ["preset custom", withField("preset", "custom")],
    ["theme auto", withField("theme", "auto")],
    ["density dense", withField("density", "dense")],
    ["arrangement grid", withField("arrangement", "grid")],
    ["extra panel status", withPanel("status", true)],
    ["missing panel revisions", withoutPanel("revisions")],
    ["non-boolean panel", withPanel("graph", "yes")],
    ["ratio 0", withRatio("detail-split", 0)],
    ["ratio 1", withRatio("detail-split", 1)],
    ["ratio -0.1", withRatio("detail-split", -0.1)],
    ["ratio 1.5", withRatio("detail-split", 1.5)],
    ["string ratio", withRatio("detail-split", "0.5")],
    ["NaN ratio", withRatio("detail-split", Number.NaN)],
    ["Infinity ratio", withRatio("graph-split", Number.POSITIVE_INFINITY)],
  ];
  for (const [label, value] of rejected) {
    assert.equal(isStockPayload(value), false, label);
  }
});

test("copyPayload returns an equal payload with independent nested objects", () => {
  const original = precision();
  const copy = copyPayload(original);
  assert.deepEqual(copy, original);
  assert.notEqual(copy, original);
  assert.notEqual(copy.panels, original.panels);
  assert.notEqual(copy.split_ratios, original.split_ratios);

  copy.panels.graph = false;
  copy.split_ratios["graph-split"] = 0.9;
  assert.equal(original.panels.graph, true);
  assert.equal(original.split_ratios["graph-split"], 0.5);
});

test("samePayload detects changes to the preset, a panel or a split ratio", () => {
  const base = precision();
  assert.equal(samePayload(base, copyPayload(base)), true);
  assert.equal(samePayload(base, command()), false);

  const panelChanged = copyPayload(base);
  panelChanged.panels.history = false;
  assert.equal(samePayload(base, panelChanged), false);

  const ratioChanged = copyPayload(base);
  ratioChanged.split_ratios["graph-split"] = 0.65;
  assert.equal(samePayload(base, ratioChanged), false);
});

test("every split pair names two distinct registered panels", () => {
  for (const arrangement of ARRANGEMENTS) {
    const [first, second] = SPLIT_PANELS[arrangement];
    assert.notEqual(first, second, arrangement);
    assert.ok(PANELS.includes(first), arrangement);
    assert.ok(PANELS.includes(second), arrangement);
  }
});

test("every preset, arrangement and panel has exactly one non-empty label", () => {
  const groups: Array<[readonly string[], Record<string, string>]> = [
    [PRESETS, PRESET_LABELS],
    [ARRANGEMENTS, ARRANGEMENT_LABELS],
    [PANELS, PANEL_LABELS],
  ];
  for (const [ids, labels] of groups) {
    assert.deepEqual(Object.keys(labels).sort(), [...ids].sort());
    for (const id of ids) {
      assert.notEqual(labels[id].trim(), "", id);
    }
  }
});
