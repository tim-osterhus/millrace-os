import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  createConfig,
  deleteConfig,
  duplicateConfig,
  effectiveDefault,
  exportConfig,
  importConfig,
  parseStrictJson,
  PreferenceError,
  readSnapshot,
  renameConfig,
  replaceUnreadable,
  resetConfig,
  setDefaultConfig,
  updateConfig,
  validLabel,
  writeVisible,
  type Snapshot,
} from "./preferences.ts";
import { copyPayload, PRECISION_FALLBACK, type StockPayload } from "./presets.ts";

const STOCK = { frontend_id: "stock", presentation_format: "millrace-stock-presentation/v1" };
const USER_A = "a".repeat(32);
const USER_B = "b".repeat(32);
const VERSION = "c".repeat(32);

function stock(preset: StockPayload["preset"], density: StockPayload["density"], hidden: string[], ratio = 0.5): StockPayload {
  const payload = copyPayload(PRECISION_FALLBACK);
  payload.preset = preset;
  payload.density = density;
  payload.arrangement = preset === "precision" ? "detail-split" : preset === "command" ? "status-stack" : "graph-split";
  for (const panel of hidden) payload.panels[panel as keyof StockPayload["panels"]] = false;
  payload.split_ratios["graph-split"] = ratio;
  return payload;
}

const BUNDLED = [
  { ...STOCK, id: "precision", name: "Precision", payload: stock("precision", "comfortable", []) },
  { ...STOCK, id: "command", name: "Command", payload: stock("command", "compact", ["graph", "inspector", "revisions"]) },
  { ...STOCK, id: "studio", name: "Studio", payload: stock("studio", "comfortable", ["planes", "current-work", "revisions"], 0.65) },
].map((item) => ({ ...item, payload_sha256: "d".repeat(64), bundle_sha256: null }));

// A custom frontend namespace whose numbers JavaScript cannot round-trip exactly.
const CUSTOM_RAW =
  '{"frontend_id":"acme","presentation_format":"acme-layout/v3","default_ref":null,' +
  '"configs":[{"id":"' +
  "e".repeat(32) +
  '","name":"Wall","payload":{"ratio":1.0,"seed":12345678901234567890,"tiny":1e-07}}]}';

function stateText(status: string, namespaces: string | null, version: string | null = VERSION): string {
  const store = namespaces === null ? "null" : `{"format":"millrace-os-presentation-store/v2","namespaces":${namespaces},"version":${JSON.stringify(version)}}`;
  return (
    `{"contract":"millrace-os-presentation-state/v1","status":"${status}","version":${namespaces === null ? "null" : JSON.stringify(version)},` +
    `"file_sha256":${status === "absent" ? "null" : JSON.stringify("f".repeat(64))},"store":${store},` +
    `"bundled_configs":${JSON.stringify(BUNDLED)},"observed_at":"2026-11-08T00:00:00+00:00"}`
  );
}

function stockNamespace(defaultRef: unknown, configs: { id: string; name: string; payload: StockPayload }[]): string {
  return JSON.stringify({ ...STOCK, default_ref: defaultRef, configs });
}

function ready(): Snapshot {
  const saved = [
    { id: USER_A, name: "Night shift", payload: stock("command", "compact", ["graph", "inspector", "revisions"]) },
    { id: USER_B, name: "Wide graph", payload: stock("studio", "comfortable", [], 0.7) },
  ];
  return readSnapshot(stateText("ready", `[${CUSTOM_RAW},${stockNamespace({ kind: "user", id: USER_A }, saved)}]`));
}

function absent(): Snapshot {
  return readSnapshot(stateText("absent", null, null));
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (typeof value !== "object" || value === null) return value;
  return Object.fromEntries(
    Object.entries(value)
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([key, item]) => [key, sortKeys(item)]),
  );
}

function sentStore(body: { text: string }) {
  const parsed = JSON.parse(body.text);
  const stockEntry = parsed.store.namespaces.find((item: { frontend_id: string }) => item.frontend_id === "stock");
  return { parsed, stockEntry };
}

test("the strict parser refuses duplicates, nonfinite and trailing data, keeping exact spans", () => {
  for (const source of ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', "{} {}", '{"a":"\u0001"}', "[01]"]) {
    assert.throws(() => parseStrictJson(source), PreferenceError, source);
  }
  // Whole number tokens are read, and a token beyond JavaScript's range stays exact.
  assert.deepEqual(parseStrictJson("[1e400]").value, [{ jsonNumber: "1e400" }]);
  assert.deepEqual(parseStrictJson("[0.5" + "0".repeat(500) + "]").value, [0.5]);
  const source = '{"outer":{"inner":[1,{"x":1.0}]},"__proto__":{"y":2}}';
  const { value, span } = parseStrictJson(source);
  const outer = (value as { outer: object }).outer;
  assert.equal(span(outer), '{"inner":[1,{"x":1.0}]}');
  assert.ok(Object.hasOwn(value as object, "__proto__"));
  assert.equal(Object.getPrototypeOf(value), Object.prototype);
});

test("snapshots keep other frontends' namespaces byte-for-byte", () => {
  const snapshot = ready();
  assert.equal(snapshot.status, "ready");
  assert.equal(snapshot.others.length, 1);
  assert.equal(snapshot.others[0].raw, CUSTOM_RAW);
  assert.equal(snapshot.stock?.configs.length, 2);
  assert.deepEqual(snapshot.stock?.defaultRef, { kind: "user", id: USER_A });
  const body = renameConfig(snapshot, USER_B, "Wider graph");
  assert.ok(body.text.includes(CUSTOM_RAW));
  assert.ok(body.text.includes('"ratio":1.0') && body.text.includes("12345678901234567890") && body.text.includes("1e-07"));
});

test("effective defaults: absent is Precision, a user default applies, unreadable files fall back", () => {
  assert.deepEqual(effectiveDefault(absent(), PRECISION_FALLBACK).ref, { kind: "bundled", id: "precision" });
  const saved = effectiveDefault(ready(), PRECISION_FALLBACK);
  assert.equal(saved.source, "saved");
  assert.equal(saved.name, "Night shift");
  const invalid = effectiveDefault(readSnapshot(stateText("invalid", null, null)), PRECISION_FALLBACK);
  assert.deepEqual([invalid.source, invalid.reason], ["fallback", "presentation_integrity_error"]);
  const newer = effectiveDefault(readSnapshot(stateText("incompatible", null, null)), PRECISION_FALLBACK);
  assert.deepEqual([newer.source, newer.reason], ["fallback", "presentation_incompatible"]);
});

test("create starts a stock namespace from absent with a null expected version", () => {
  const body = createConfig(absent(), "Mine", stock("studio", "comfortable", []));
  const { parsed, stockEntry } = sentStore(body);
  assert.equal(parsed.format, "millrace-os-presentation-write/v1");
  assert.equal(parsed.mode, "compare");
  assert.equal(parsed.expected_version, null);
  assert.equal(parsed.store.format, "millrace-os-presentation-store/v2");
  assert.equal(parsed.store.version, undefined);
  assert.deepEqual(stockEntry.default_ref, { kind: "bundled", id: "precision" });
  assert.equal(stockEntry.configs.length, 1);
  assert.match(stockEntry.configs[0].id, /^[0-9a-f]{32}$/);
});

test("every mutation compares the displayed version and keeps configs sorted by ID", () => {
  const snapshot = ready();
  for (const body of [
    createConfig(snapshot, "Third", stock("precision", "compact", [])),
    updateConfig(snapshot, USER_A, stock("precision", "comfortable", [])),
    renameConfig(snapshot, USER_A, "Renamed"),
    duplicateConfig(snapshot, { kind: "bundled", id: "studio" }, "Copy"),
    setDefaultConfig(snapshot, { kind: "bundled", id: "command" }),
  ]) {
    const { parsed, stockEntry } = sentStore(body);
    assert.equal(parsed.expected_version, VERSION);
    const ids = stockEntry.configs.map((config: { id: string }) => config.id);
    assert.deepEqual(ids, [...ids].sort());
    assert.deepEqual(
      parsed.store.namespaces.map((item: { frontend_id: string }) => item.frontend_id),
      ["acme", "stock"],
    );
  }
});

test("deleting the default user record falls back to bundled Precision", () => {
  const { stockEntry } = sentStore(deleteConfig(ready(), USER_A));
  assert.deepEqual(stockEntry.default_ref, { kind: "bundled", id: "precision" });
  assert.deepEqual(
    stockEntry.configs.map((config: { id: string }) => config.id),
    [USER_B],
  );
  assert.throws(() => deleteConfig(ready(), "9".repeat(32)), /presentation_config_not_found/);
});

test("reset copies the current bundled base of the record's preset under the same ID and name", () => {
  const { stockEntry } = sentStore(resetConfig(ready(), USER_B));
  const reset = stockEntry.configs.find((config: { id: string }) => config.id === USER_B);
  assert.equal(reset.name, "Wide graph");
  assert.deepEqual(reset.payload, BUNDLED[2].payload);
});

test("set default refuses unknown references", () => {
  assert.throws(() => setDefaultConfig(ready(), { kind: "user", id: "9".repeat(32) }), /presentation_config_not_found/);
  assert.throws(() => setDefaultConfig(ready(), { kind: "bundled", id: "default" }), /invalid_request/);
});

test("export and import use the portable config document and never overwrite", () => {
  const exported = exportConfig(ready(), { kind: "user", id: USER_A });
  const document = JSON.parse(exported);
  assert.deepEqual(Object.keys(document).sort(), ["format", "frontend_id", "name", "payload", "presentation_format"]);
  assert.equal(document.format, "millrace-os-presentation-config/v2");
  const { stockEntry } = sentStore(importConfig(ready(), exported));
  assert.equal(stockEntry.configs.length, 3);
  const imported = stockEntry.configs.find((config: { id: string }) => config.id !== USER_A && config.id !== USER_B);
  assert.equal(imported.name, "Night shift");
  assert.throws(() => importConfig(ready(), exported.replace('"name"', '"name": "x", "name"')), PreferenceError);
  const foreign = JSON.stringify({ ...document, frontend_id: "acme", presentation_format: "acme-layout/v3" });
  assert.throws(() => importConfig(ready(), foreign), /foreign_namespace/);
  const broken = JSON.stringify({ ...document, payload: { ...document.payload, theme: "neon" } });
  assert.throws(() => importConfig(ready(), broken), /invalid_request/);
});

test("unreadable stores refuse ordinary writes and allow only digest replacement", () => {
  const invalid = readSnapshot(stateText("invalid", null, null));
  assert.throws(() => createConfig(invalid, "x", PRECISION_FALLBACK), /presentation_integrity_error/);
  assert.throws(() => exportConfig(invalid, { kind: "bundled", id: "precision" }), /presentation_integrity_error/);
  const body = JSON.parse(replaceUnreadable(invalid));
  assert.equal(body.mode, "replace_unreadable");
  assert.equal(body.observed_file_sha256, "f".repeat(64));
  assert.deepEqual(body.store, { format: "millrace-os-presentation-store/v2", namespaces: [] });
  assert.throws(() => replaceUnreadable(ready()), /invalid_request/);
});

test("an uncertain write is confirmed only when exactly the intended content is visible", () => {
  const before = ready();
  const body = renameConfig(before, USER_A, "Morning shift");
  const { stockEntry } = sentStore(body);
  const visible = readSnapshot(stateText("ready", `[${CUSTOM_RAW},${JSON.stringify(stockEntry)}]`, "1".repeat(32)));
  assert.equal(writeVisible(body, before, visible), true);
  // The service returns canonical (sorted-key) JSON; confirmation must not depend on key order.
  const sorted = readSnapshot(
    stateText("ready", `[${CUSTOM_RAW},${JSON.stringify(sortKeys(stockEntry))}]`, "1".repeat(32)),
  );
  assert.equal(writeVisible(body, before, sorted), true);
  assert.equal(writeVisible(body, before, before), false);
  const otherChanged = readSnapshot(
    stateText("ready", `[${CUSTOM_RAW.replace("Wall", "Wall 2")},${JSON.stringify(stockEntry)}]`, "1".repeat(32)),
  );
  assert.equal(writeVisible(body, before, otherChanged), false);
});

test("other frontends' numbers stay lossless and uncertain writes compare them exactly", () => {
  const huge = "9".repeat(400);
  const foreign = (seed: string) =>
    '{"frontend_id":"acme","presentation_format":"acme-layout/v3","default_ref":null,"configs":[{"id":"' +
    "e".repeat(32) +
    `","name":"Wall","payload":{"seed":${seed},"huge":${huge}}}]}`;
  const before = readSnapshot(stateText("ready", `[${foreign("9007199254740993")}]`));
  assert.equal(before.others[0].raw, foreign("9007199254740993"));
  const body = createConfig(before, "Mine", PRECISION_FALLBACK);
  assert.ok(body.text.includes(huge) && body.text.includes("9007199254740993"));
  const { stockEntry } = sentStore(body);
  const rounded = readSnapshot(
    stateText("ready", `[${foreign("9007199254740992")},${JSON.stringify(stockEntry)}]`, "1".repeat(32)),
  );
  // 9007199254740992 and ...993 are the same JavaScript number, but not the same data.
  assert.equal(writeVisible(body, before, rounded), false);
  const exact = readSnapshot(
    stateText("ready", `[${foreign("9007199254740993")},${JSON.stringify(stockEntry)}]`, "1".repeat(32)),
  );
  assert.equal(writeVisible(body, before, exact), true);
});

test("a long but valid stock ratio literal imports, while a nonfinite one is refused", () => {
  const document = JSON.parse(exportConfig(ready(), { kind: "bundled", id: "precision" }));
  const long = JSON.stringify({ ...document }).replace('"detail-split":0.5', `"detail-split":0.5${"0".repeat(500)}`);
  const { stockEntry } = sentStore(importConfig(ready(), long));
  assert.equal(stockEntry.configs.length, 3);
  const nonfinite = JSON.stringify(document).replace('"detail-split":0.5', '"detail-split":1e400');
  assert.throws(() => importConfig(ready(), nonfinite), /invalid_request/);
});

test("labels follow the plain one-to-256 character rule, counted in code points", () => {
  assert.equal(validLabel("Night shift"), true);
  assert.equal(validLabel("x".repeat(256)), true);
  assert.equal(validLabel("x".repeat(257)), false);
  assert.equal(validLabel("🙂".repeat(256)), true);
  for (const bad of ["", "a\tb", "a\nb", "a\rb", "a\u0007b", "a\u007fb", "a\ud800b", "\udc00"]) {
    assert.equal(validLabel(bad), false, JSON.stringify(bad));
  }
  assert.throws(() => renameConfig(ready(), USER_A, "two\nlines"), /invalid_request/);
});

test("the module keeps presentation out of browser storage", () => {
  const source = readFileSync(new URL("./preferences.ts", import.meta.url), "utf8");
  for (const api of ["localStorage", "sessionStorage", "indexedDB", "document.cookie"]) {
    assert.equal(source.includes(api), false, api);
  }
});
