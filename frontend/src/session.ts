// The stock client's only browser-trust owner.
//
// `millrace-os open` puts a one-use nonce in the URL fragment. It is read once
// and removed with `history.replaceState` before any request, then exchanged
// for the HttpOnly session cookie and its CSRF value. A reload has no fragment:
// it restores the same session's CSRF through the session-context read, which
// never renews the session. The CSRF value lives only in this module's closure.
// It is never exported, rendered, stored, logged or copied into history state.

/** OS-G0 D3: compact UTF-8 bytes of every bounded JSON response. */
export const RESPONSE_LIMIT = 1_049_600;
/** Browser deadline per request. Expiry means "not observed", never "stopped". */
export const REQUEST_DEADLINE_MS = 5_000;

const SECRET = /^[A-Za-z0-9_-]{43,64}$/;

export type Trust =
  | { state: "pending" }
  | { state: "ready"; source: "exchange" | "session-context"; expiresAt: number }
  | { state: "expired"; reason: "nonce" | "session" }
  | { state: "refused"; code: string; status: number };

export interface ApiError {
  status: number;
  code: string;
  summary: string;
  retryable: boolean;
}

export type Reply =
  | { ok: true; status: number; body: unknown; text: string; receivedAt: number }
  | { ok: false; status: number; error: ApiError; receivedAt: number };

let csrf: string | null = null;
let trust: Trust = { state: "pending" };
let expiryTimer: number | undefined;
const listeners = new Set<(trust: Trust) => void>();

export function currentTrust(): Trust {
  return trust;
}

export function onTrust(listener: (trust: Trust) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function setTrust(next: Trust): void {
  trust = next;
  if (next.state !== "ready") {
    csrf = null;
    window.clearTimeout(expiryTimer);
  }
  for (const listener of listeners) listener(next);
}

/** Reads and removes the fragment exactly once, keeping path, query and state. */
function takeFragmentNonce(): string | null {
  const raw = window.location.hash.slice(1);
  if (window.location.hash !== "") {
    window.history.replaceState(
      window.history.state,
      "",
      window.location.pathname + window.location.search,
    );
  }
  return SECRET.test(raw) ? raw : null;
}

class TooLarge extends Error {}

async function readBounded(response: Response): Promise<string> {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > RESPONSE_LIMIT) throw new TooLarge();
  if (response.body === null) return "";
  const reader = response.body.getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > RESPONSE_LIMIT) {
      await reader.cancel();
      throw new TooLarge();
    }
    parts.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    bytes.set(part, offset);
    offset += part.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}

function clientError(code: string, summary: string): Reply {
  return {
    ok: false,
    status: 0,
    error: { status: 0, code, summary, retryable: true },
    receivedAt: Date.now(),
  };
}

function apiError(status: number, body: unknown): ApiError {
  if (typeof body === "object" && body !== null) {
    const value = body as Record<string, unknown>;
    if (value.contract === "os.error.v1" && typeof value.code === "string") {
      return {
        status,
        code: value.code,
        summary: typeof value.summary === "string" ? value.summary : value.code,
        retryable: value.retryable === true,
      };
    }
  }
  return { status, code: "unexpected_response", summary: "unexpected response", retryable: false };
}

async function transfer(path: string, init: RequestInit): Promise<Reply> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_DEADLINE_MS);
  try {
    const response = await fetch(path, {
      ...init,
      mode: "same-origin",
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
      signal: controller.signal,
    });
    const text = await readBounded(response);
    let body: unknown = null;
    if (text !== "") {
      try {
        body = JSON.parse(text);
      } catch {
        return clientError("invalid_response", "the service response was not JSON");
      }
    }
    if (response.ok) {
      return { ok: true, status: response.status, body, text, receivedAt: Date.now() };
    }
    return { ok: false, status: response.status, error: apiError(response.status, body), receivedAt: Date.now() };
  } catch (error) {
    if (error instanceof TooLarge) {
      return clientError("response_too_large", "the response exceeded the 1,049,600-byte read limit");
    }
    if (controller.signal.aborted) {
      return clientError("client_deadline", "no response within 5 seconds; the service state is unknown");
    }
    return clientError("network_unavailable", "the service could not be reached");
  } finally {
    window.clearTimeout(timer);
  }
}

/** A 401 ends the session; any 403 disables writes and keeps its sanitized code. */
function observeTrust(reply: Reply): void {
  if (reply.ok || trust.state !== "ready") return;
  if (reply.status === 401) {
    setTrust({ state: "expired", reason: "session" });
  } else if (reply.status === 403) {
    setTrust({ state: "refused", code: reply.error.code, status: 403 });
  }
}

function scheduleExpiry(seconds: number): number {
  const expiresAt = Date.now() + seconds * 1000;
  window.clearTimeout(expiryTimer);
  expiryTimer = window.setTimeout(() => {
    setTrust({ state: "expired", reason: "session" });
  }, seconds * 1000);
  return expiresAt;
}

/** Exchange a fragment nonce, otherwise restore this cookie session's CSRF. */
export async function establishTrust(): Promise<Trust> {
  const nonce = takeFragmentNonce();
  const reply = nonce
    ? await transfer("/api/v1/auth/exchange", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nonce, recovery: false }),
      })
    : await transfer("/api/v1/auth/session-context", {
        method: "GET",
        headers: { "X-Millrace-Session-Context": "1" },
      });
  if (!reply.ok) {
    if (reply.status === 401) {
      setTrust({ state: "expired", reason: nonce ? "nonce" : "session" });
    } else {
      setTrust({ state: "refused", code: reply.error.code, status: reply.status });
    }
    return trust;
  }
  const body = (typeof reply.body === "object" && reply.body !== null ? reply.body : {}) as Record<
    string,
    unknown
  >;
  const seconds = body.expires_in;
  const valid =
    typeof body.csrf === "string" &&
    SECRET.test(body.csrf) &&
    typeof seconds === "number" &&
    Number.isInteger(seconds) &&
    seconds >= 1 &&
    seconds <= 900 &&
    (nonce ? seconds === 900 : body.contract === "os.session-context.v1");
  if (!valid) {
    setTrust({ state: "refused", code: "invalid_response", status: reply.status });
    return trust;
  }
  csrf = body.csrf as string;
  const expiresAt = scheduleExpiry(seconds as number);
  setTrust({ state: "ready", source: nonce ? "exchange" : "session-context", expiresAt });
  return trust;
}

/** An authenticated GET of one fixed API path. Reads stop after expiry. */
export async function read(path: string): Promise<Reply> {
  if (trust.state === "pending" || trust.state === "expired") {
    return clientError("trust_unavailable", "browser trust is not established");
  }
  const reply = await transfer(path, { method: "GET" });
  observeTrust(reply);
  return reply;
}

/** An authenticated mutation with the lexical CSRF; refused without trust. */
export async function write(method: "PUT" | "POST", path: string, body: string): Promise<Reply> {
  if (trust.state !== "ready" || csrf === null) {
    return clientError("trust_unavailable", "writes need an authenticated session");
  }
  const reply = await transfer(path, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body,
  });
  observeTrust(reply);
  return reply;
}
