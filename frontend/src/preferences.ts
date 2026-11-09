// Shared presentation preferences for the stock client.
//
// The OS service owns one versioned store (`config_dir/presentation.json`) for
// every client. This module only reads the GET state and builds the strict
// expected-version write bodies the service validates. It mirrors the pure
// helpers in `millrace_os.presentation` for the stock namespace. Other
// frontends' namespaces are copied back byte-for-byte from the GET text, so a
// stock save can never reinterpret their payload numbers or formats.
// No browser storage is used: unsaved layout stays in component memory.

import { isStockPayload, PRESETS, STOCK_FORMAT, STOCK_FRONTEND, type StockPayload } from "./presets.ts";

export const STORE_FORMAT = "millrace-os-presentation-store/v2";
export const WRITE_FORMAT = "millrace-os-presentation-write/v1";
export const CONFIG_FORMAT = "millrace-os-presentation-config/v2";
/** The shared 1 MiB large-data policy for one imported document. */
export const IMPORT_LIMIT = 1_048_576;

const CORE_ID = /^[0-9a-f]{32}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const FRONTEND_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const FORMAT_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const NOT_PLAIN = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\t\n\r]/;
const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;

export class PreferenceError extends Error {
  readonly code: string;
  constructor(code: string, detail?: string) {
    super(detail === undefined ? code : `${code}: ${detail}`);
    this.name = "PreferenceError";
    this.code = code;
  }
}

// ---- strict JSON with spans -------------------------------------------------

const ESCAPES = new Map([
  ['"', '"'],
  ["\\", "\\"],
  ["/", "/"],
  ["b", "\b"],
  ["f", "\f"],
  ["n", "\n"],
  ["r", "\r"],
  ["t", "\t"],
]);

const NUMBER = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;

/**
 * A JSON number JavaScript cannot represent finitely. It is kept as its exact
 * token so another frontend's data is never refused or rounded; stock checks
 * still require real finite numbers.
 */
export interface RawNumber {
  readonly jsonNumber: string;
}

/** JSON.parse would keep the last duplicate key; this parser refuses it. */
export function parseStrictJson(source: string): { value: unknown; span: (value: object) => string | null } {
  const spans = new WeakMap<object, [number, number]>();
  let index = 0;

  function fail(what: string): never {
    throw new PreferenceError("invalid_json", `${what} at offset ${index}`);
  }
  const space = () => {
    while (index < source.length && " \t\n\r".includes(source[index])) index += 1;
  };
  const expect = (char: string) => {
    if (source[index] !== char) fail(`expected ${char}`);
    index += 1;
  };
  const string = (): string => {
    expect('"');
    let result = "";
    for (;;) {
      if (index >= source.length) fail("unterminated string");
      const char = source[index];
      index += 1;
      if (char === '"') return result;
      if (char < " ") fail("control character in string");
      if (char !== "\\") {
        result += char;
        continue;
      }
      const escape = source[index];
      index += 1;
      const simple = ESCAPES.get(escape);
      if (simple !== undefined) {
        result += simple;
      } else if (escape === "u") {
        const hex = source.slice(index, index + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail("bad unicode escape");
        result += String.fromCharCode(Number.parseInt(hex, 16));
        index += 4;
      } else {
        fail("bad escape");
      }
    }
  };
  const value = (): unknown => {
    space();
    const start = index;
    const char = source[index];
    if (char === "{") {
      index += 1;
      const object: Record<string, unknown> = {};
      const seen = new Set<string>();
      space();
      if (source[index] === "}") {
        index += 1;
      } else {
        for (;;) {
          space();
          const key = string();
          if (seen.has(key)) fail(`duplicate key ${JSON.stringify(key)}`);
          seen.add(key);
          space();
          expect(":");
          Object.defineProperty(object, key, { value: value(), enumerable: true, writable: true, configurable: true });
          space();
          if (source[index] === ",") {
            index += 1;
            continue;
          }
          expect("}");
          break;
        }
      }
      spans.set(object, [start, index]);
      return object;
    }
    if (char === "[") {
      index += 1;
      const array: unknown[] = [];
      space();
      if (source[index] === "]") {
        index += 1;
      } else {
        for (;;) {
          array.push(value());
          space();
          if (source[index] === ",") {
            index += 1;
            continue;
          }
          expect("]");
          break;
        }
      }
      spans.set(array, [start, index]);
      return array;
    }
    if (char === '"') return string();
    for (const [word, literal] of [["true", true], ["false", false], ["null", null]] as const) {
      if (source.startsWith(word, index)) {
        index += word.length;
        return literal;
      }
    }
    NUMBER.lastIndex = index;
    const match = NUMBER.exec(source);
    if (match === null) fail("unexpected token");
    index += match[0].length;
    const number = Number(match[0]);
    // The whole token is read: no length window. A token beyond JavaScript's
    // finite range stays exact text rather than refusing the document.
    return Number.isFinite(number) ? number : ({ jsonNumber: match[0] } satisfies RawNumber);
  };
  let parsed: unknown;
  try {
    parsed = value();
    space();
    if (index !== source.length) fail("trailing data");
  } catch (error) {
    if (error instanceof PreferenceError) throw error;
    throw new PreferenceError("invalid_json", "JSON nests too deeply");
  }
  return {
    value: parsed,
    span: (target) => {
      const range = spans.get(target);
      return range === undefined ? null : source.slice(range[0], range[1]);
    },
  };
}

// ---- snapshot of the GET state ---------------------------------------------

export type Ref = { kind: "bundled"; id: string } | { kind: "user"; id: string };

export interface StockConfig {
  id: string;
  name: string;
  payload: StockPayload;
}

export interface BundledConfig {
  id: string;
  name: string;
  payload: StockPayload;
}

export interface OtherNamespace {
  frontendId: string;
  presentationFormat: string;
  configCount: number;
  raw: string;
}

export interface Snapshot {
  status: "absent" | "ready" | "incompatible" | "invalid";
  version: string | null;
  fileSha256: string | null;
  /** Null until a stock namespace is saved; an absent one defaults to Precision. */
  stock: { defaultRef: Ref; configs: StockConfig[] } | null;
  others: OtherNamespace[];
  bundled: BundledConfig[];
}

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function ref(value: unknown): Ref | null {
  const item = object(value);
  if (item === null || Object.keys(item).length !== 2 || typeof item.id !== "string") return null;
  if (item.kind === "user" && CORE_ID.test(item.id)) return { kind: "user", id: item.id };
  if (item.kind === "bundled") return { kind: "bundled", id: item.id };
  return null;
}

function nullableMatch(value: unknown, pattern: RegExp): string | null | undefined {
  if (value === null) return null;
  return typeof value === "string" && pattern.test(value) ? value : undefined;
}

export function validLabel(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const length = [...value].length;
  return length >= 1 && length <= 256 && !NOT_PLAIN.test(value) && !LONE_SURROGATE.test(value);
}

/** Validate the GET text; unknown shapes refuse rather than guess. */
export function readSnapshot(source: string): Snapshot {
  const { value, span } = parseStrictJson(source);
  const state = object(value);
  if (state === null || state.contract !== "millrace-os-presentation-state/v1") {
    throw new PreferenceError("invalid_response");
  }
  const status = state.status;
  if (status !== "absent" && status !== "ready" && status !== "incompatible" && status !== "invalid") {
    throw new PreferenceError("invalid_response");
  }
  const version = nullableMatch(state.version, CORE_ID);
  const fileSha256 = nullableMatch(state.file_sha256, SHA256);
  if (version === undefined || fileSha256 === undefined) throw new PreferenceError("invalid_response");
  const bundled: BundledConfig[] = [];
  for (const item of Array.isArray(state.bundled_configs) ? state.bundled_configs : []) {
    const config = object(item);
    if (
      config?.frontend_id === STOCK_FRONTEND &&
      config.presentation_format === STOCK_FORMAT &&
      typeof config.id === "string" &&
      (PRESETS as readonly string[]).includes(config.id) &&
      validLabel(config.name) &&
      isStockPayload(config.payload)
    ) {
      bundled.push({ id: config.id, name: config.name, payload: config.payload });
    }
  }
  const snapshot: Snapshot = { status, version, fileSha256, stock: null, others: [], bundled };
  if (status !== "ready") return snapshot;
  const store = object(state.store);
  if (store === null || store.format !== STORE_FORMAT || !Array.isArray(store.namespaces)) {
    throw new PreferenceError("invalid_response");
  }
  for (const entry of store.namespaces) {
    const namespace = object(entry);
    const configs = namespace === null ? null : namespace.configs;
    if (
      namespace === null ||
      typeof namespace.frontend_id !== "string" ||
      typeof namespace.presentation_format !== "string" ||
      !Array.isArray(configs)
    ) {
      throw new PreferenceError("invalid_response");
    }
    if (namespace.frontend_id === STOCK_FRONTEND && namespace.presentation_format === STOCK_FORMAT) {
      const defaultRef = ref(namespace.default_ref);
      const parsed: StockConfig[] = [];
      for (const raw of configs) {
        const config = object(raw);
        if (
          config === null ||
          typeof config.id !== "string" ||
          !CORE_ID.test(config.id) ||
          !validLabel(config.name) ||
          !isStockPayload(config.payload)
        ) {
          throw new PreferenceError("invalid_response");
        }
        parsed.push({ id: config.id, name: config.name, payload: config.payload });
      }
      if (defaultRef === null) throw new PreferenceError("invalid_response");
      snapshot.stock = { defaultRef, configs: parsed };
    } else {
      const raw = span(namespace);
      if (raw === null) throw new PreferenceError("invalid_response");
      snapshot.others.push({
        frontendId: namespace.frontend_id,
        presentationFormat: namespace.presentation_format,
        configCount: configs.length,
        raw,
      });
    }
  }
  return snapshot;
}

// ---- effective presentation -------------------------------------------------

export interface Effective {
  source: "saved" | "bundled" | "fallback";
  ref: Ref | null;
  name: string;
  payload: StockPayload;
  /** Why a built-in fallback is in use; null when the saved choice applies. */
  reason: string | null;
}

/** Resolve the namespace default. Nothing here writes or repairs the store. */
export function effectiveDefault(snapshot: Snapshot, fallback: StockPayload): Effective {
  if (snapshot.status === "invalid") {
    return { source: "fallback", ref: null, name: "Precision", payload: fallback, reason: "presentation_integrity_error" };
  }
  if (snapshot.status === "incompatible") {
    return { source: "fallback", ref: null, name: "Precision", payload: fallback, reason: "presentation_incompatible" };
  }
  const chosen: Ref = snapshot.stock?.defaultRef ?? { kind: "bundled", id: "precision" };
  if (chosen.kind === "user") {
    const config = snapshot.stock?.configs.find((item) => item.id === chosen.id);
    if (config !== undefined) {
      return { source: "saved", ref: chosen, name: config.name, payload: config.payload, reason: null };
    }
  } else {
    const base = snapshot.bundled.find((item) => item.id === chosen.id);
    if (base !== undefined) {
      return { source: "bundled", ref: chosen, name: base.name, payload: base.payload, reason: null };
    }
  }
  return { source: "fallback", ref: null, name: "Precision", payload: fallback, reason: "presentation_config_not_found" };
}

export function resolveRef(snapshot: Snapshot, target: Ref): { name: string; payload: StockPayload } {
  if (target.kind === "user") {
    const config = snapshot.stock?.configs.find((item) => item.id === target.id);
    if (config === undefined) throw new PreferenceError("presentation_config_not_found");
    return { name: config.name, payload: config.payload };
  }
  const base = snapshot.bundled.find((item) => item.id === target.id);
  if (base === undefined) throw new PreferenceError("presentation_config_not_found");
  return { name: base.name, payload: base.payload };
}

// ---- write bodies (one expected-version PUT each) ---------------------------

interface StockNamespace {
  defaultRef: Ref;
  configs: StockConfig[];
}

export interface WriteBody {
  /** The exact PUT body; non-stock namespaces are the GET's raw text. */
  text: string;
  /** The stock namespace this write intends, for uncertain-write comparison. */
  intended: StockNamespace;
}

function editable(snapshot: Snapshot): StockNamespace {
  if (snapshot.status === "invalid") throw new PreferenceError("presentation_integrity_error");
  if (snapshot.status === "incompatible") throw new PreferenceError("presentation_incompatible");
  const stock = snapshot.stock ?? { defaultRef: { kind: "bundled", id: "precision" }, configs: [] };
  return {
    defaultRef: { ...stock.defaultRef },
    configs: stock.configs.map((config) => ({ ...config, payload: copyStock(config.payload) })),
  };
}

function copyStock(payload: StockPayload): StockPayload {
  return { ...payload, panels: { ...payload.panels }, split_ratios: { ...payload.split_ratios } };
}

function stockJson(stock: StockNamespace): string {
  const configs = [...stock.configs].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return JSON.stringify({
    frontend_id: STOCK_FRONTEND,
    presentation_format: STOCK_FORMAT,
    default_ref: stock.defaultRef,
    configs: configs.map((config) => ({ id: config.id, name: config.name, payload: config.payload })),
  });
}

function namespacesJson(snapshot: Snapshot, stock: StockNamespace | null): string {
  const entries: { key: [string, string]; raw: string }[] = snapshot.others.map((item) => ({
    key: [item.frontendId, item.presentationFormat],
    raw: item.raw,
  }));
  if (stock !== null) entries.push({ key: [STOCK_FRONTEND, STOCK_FORMAT], raw: stockJson(stock) });
  entries.sort((a, b) => {
    if (a.key[0] !== b.key[0]) return a.key[0] < b.key[0] ? -1 : 1;
    return a.key[1] < b.key[1] ? -1 : a.key[1] > b.key[1] ? 1 : 0;
  });
  return "[" + entries.map((entry) => entry.raw).join(",") + "]";
}

function compare(snapshot: Snapshot, stock: StockNamespace): WriteBody {
  const store = `{"format":${JSON.stringify(STORE_FORMAT)},"namespaces":${namespacesJson(snapshot, stock)}}`;
  return {
    text: `{"format":${JSON.stringify(WRITE_FORMAT)},"mode":"compare","expected_version":${JSON.stringify(
      snapshot.version,
    )},"store":${store}}`,
    intended: stock,
  };
}

function newId(stock: StockNamespace): string {
  for (;;) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    const id = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    if (!stock.configs.some((config) => config.id === id)) return id;
  }
}

function checkedName(name: string): string {
  if (!validLabel(name)) throw new PreferenceError("invalid_request", "names are 1-256 plain characters without tabs or line breaks");
  return name;
}

function checkedPayload(payload: StockPayload): StockPayload {
  if (!isStockPayload(payload)) throw new PreferenceError("invalid_request");
  return copyStock(payload);
}

function find(stock: StockNamespace, id: string): StockConfig {
  const config = stock.configs.find((item) => item.id === id);
  if (config === undefined) throw new PreferenceError("presentation_config_not_found");
  return config;
}

export function createConfig(snapshot: Snapshot, name: string, payload: StockPayload): WriteBody {
  const stock = editable(snapshot);
  stock.configs.push({ id: newId(stock), name: checkedName(name), payload: checkedPayload(payload) });
  return compare(snapshot, stock);
}

export function updateConfig(snapshot: Snapshot, id: string, payload: StockPayload): WriteBody {
  const stock = editable(snapshot);
  find(stock, id).payload = checkedPayload(payload);
  return compare(snapshot, stock);
}

export function renameConfig(snapshot: Snapshot, id: string, name: string): WriteBody {
  const stock = editable(snapshot);
  find(stock, id).name = checkedName(name);
  return compare(snapshot, stock);
}

export function duplicateConfig(snapshot: Snapshot, source: Ref, name: string): WriteBody {
  const stock = editable(snapshot);
  const origin = resolveRef(snapshot, source);
  stock.configs.push({ id: newId(stock), name: checkedName(name), payload: copyStock(origin.payload) });
  return compare(snapshot, stock);
}

/** Deleting the default user record falls back to bundled Precision. */
export function deleteConfig(snapshot: Snapshot, id: string): WriteBody {
  const stock = editable(snapshot);
  const config = find(stock, id);
  stock.configs = stock.configs.filter((item) => item !== config);
  if (stock.defaultRef.kind === "user" && stock.defaultRef.id === id) {
    stock.defaultRef = { kind: "bundled", id: "precision" };
  }
  return compare(snapshot, stock);
}

export function setDefaultConfig(snapshot: Snapshot, target: Ref): WriteBody {
  const stock = editable(snapshot);
  if (target.kind === "user") find(stock, target.id);
  else if (!(PRESETS as readonly string[]).includes(target.id)) throw new PreferenceError("invalid_request");
  stock.defaultRef = { ...target };
  return compare(snapshot, stock);
}

/** Copy the currently supported bundled base of the record's preset. */
export function resetConfig(snapshot: Snapshot, id: string): WriteBody {
  const stock = editable(snapshot);
  const config = find(stock, id);
  const base = snapshot.bundled.find((item) => item.id === config.payload.preset);
  if (base === undefined) throw new PreferenceError("presentation_incompatible");
  config.payload = copyStock(base.payload);
  return compare(snapshot, stock);
}

/** A portable `millrace-os-presentation-config/v2` document (pretty JSON). */
export function exportConfig(snapshot: Snapshot, target: Ref): string {
  editable(snapshot);
  const chosen = resolveRef(snapshot, target);
  return (
    JSON.stringify(
      {
        format: CONFIG_FORMAT,
        frontend_id: STOCK_FRONTEND,
        presentation_format: STOCK_FORMAT,
        name: chosen.name,
        payload: chosen.payload,
      },
      null,
      2,
    ) + "\n"
  );
}

/** Import creates a new ID and never overwrites. Other frontends' data stays theirs. */
export function importConfig(snapshot: Snapshot, source: string, name?: string): WriteBody {
  const config = object(parseStrictJson(source).value);
  const keys = ["format", "frontend_id", "presentation_format", "name", "payload"];
  if (
    config === null ||
    Object.keys(config).length !== keys.length ||
    !keys.every((key) => Object.hasOwn(config, key)) ||
    config.format !== CONFIG_FORMAT ||
    typeof config.frontend_id !== "string" ||
    !FRONTEND_ID.test(config.frontend_id) ||
    typeof config.presentation_format !== "string" ||
    !FORMAT_ID.test(config.presentation_format)
  ) {
    throw new PreferenceError("invalid_request", "not a millrace-os-presentation-config/v2 config");
  }
  if (config.frontend_id !== STOCK_FRONTEND || config.presentation_format !== STOCK_FORMAT) {
    throw new PreferenceError(
      "foreign_namespace",
      `this configuration belongs to frontend ${config.frontend_id}; import it with that frontend or the CLI`,
    );
  }
  if (!isStockPayload(config.payload)) throw new PreferenceError("invalid_request", "the stock payload is not valid");
  const stock = editable(snapshot);
  const label = checkedName(name ?? (typeof config.name === "string" ? config.name : ""));
  stock.configs.push({ id: newId(stock), name: label, payload: copyStock(config.payload) });
  return compare(snapshot, stock);
}

/** Deliberately replace an unreadable store by its observed digest. */
export function replaceUnreadable(snapshot: Snapshot): string {
  if ((snapshot.status !== "invalid" && snapshot.status !== "incompatible") || snapshot.fileSha256 === null) {
    throw new PreferenceError("invalid_request");
  }
  return JSON.stringify({
    format: WRITE_FORMAT,
    mode: "replace_unreadable",
    observed_file_sha256: snapshot.fileSha256,
    store: { format: STORE_FORMAT, namespaces: [] },
  });
}

/** Key-order-independent JSON text of stock values (finite numbers only). */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return "{" + entries.map(([key, item]) => JSON.stringify(key) + ":" + canonical(item)).join(",") + "}";
  }
  return JSON.stringify(value);
}

/** After an uncertain write: is exactly the intended content now visible? */
export function writeVisible(sent: WriteBody, before: Snapshot, fresh: Snapshot): boolean {
  if (fresh.status !== "ready" || fresh.stock === null) return false;
  if (fresh.others.length !== before.others.length) return false;
  // Other namespaces were sent as their exact text; anything but the same
  // text (even an equal-looking rounded number) is not proof of this write.
  if (fresh.others.some((item, index) => item.raw !== before.others[index].raw)) return false;
  return canonical(JSON.parse(stockJson(fresh.stock))) === canonical(JSON.parse(stockJson(sent.intended)));
}
