import { test } from "node:test";
import assert from "node:assert/strict";
import { classifySource } from "./links.ts";

// Public-looking hosts render as links. No DNS lookup is involved, so these
// inputs make no reachability or safety claim.
const LINKS: readonly string[] = [
  "https://example.com/report",
  "https://docs.example.org:8443/a?b=1#c",
  "https://93.184.216.34/",
  "https://[2606:4700::1111]/",
  "https://Example.COM./x",
  // Boundary addresses just outside the 10/8, 100.64/10 and 172.16/12 ranges.
  "https://172.32.0.1/",
  "https://172.15.255.255/",
  "https://100.128.0.1/",
  "https://11.0.0.1/",
];

// Wrong scheme, malformed or non-text input, credentials, local host names,
// and loopback, private, shared, link-local, unspecified or unique-local hosts.
const INERT: readonly unknown[] = [
  "http://example.com/",
  "javascript:alert(1)",
  "data:text/html,x",
  "/relative/path",
  "not a url",
  "",
  42,
  null,
  "https://user:pass@example.com/",
  "https://user@example.com/",
  "https://localhost/",
  "https://LOCALHOST./",
  "https://app.localhost/",
  "https://127.0.0.1/",
  "https://127.1/",
  "https://0x7f.1/",
  "https://2130706433/",
  "https://0.0.0.0/",
  "https://10.1.2.3/",
  "https://172.16.0.1/",
  "https://172.31.255.255/",
  "https://192.168.1.1/",
  "https://169.254.169.254/",
  "https://100.64.0.1/",
  "https://[::1]/",
  "https://[::]/",
  "https://[::ffff:127.0.0.1]/",
  "https://[::ffff:10.0.0.1]/",
  "https://[fe80::1]/",
  "https://[fc00::1]/",
  "https://[fd12:3456::1]/",
];

function hostOf(raw: unknown): string | null {
  const result = classifySource(raw);
  return result.kind === "link" ? result.host : null;
}

test("public https sources classify as links", () => {
  for (const raw of LINKS) {
    assert.equal(classifySource(raw).kind, "link", raw);
  }
});

test("link results carry the WHATWG href and a bare lowercase host", () => {
  assert.deepEqual(classifySource("https://example.com/report"), {
    kind: "link",
    href: "https://example.com/report",
    host: "example.com",
  });
  assert.deepEqual(classifySource("https://docs.example.org:8443/a?b=1#c"), {
    kind: "link",
    href: "https://docs.example.org:8443/a?b=1#c",
    host: "docs.example.org",
  });
  assert.deepEqual(classifySource("https://Example.COM./x"), {
    kind: "link",
    href: "https://example.com./x",
    host: "example.com",
  });
  assert.equal(hostOf("https://93.184.216.34/"), "93.184.216.34");
});

test("unsafe, local and malformed sources classify as inert", () => {
  for (const raw of INERT) {
    assert.equal(classifySource(raw).kind, "inert", JSON.stringify(raw));
  }
});

test("inert results name the reason", () => {
  const cases: Array<[unknown, string]> = [
    [42, "not text"],
    [null, "not text"],
    ["", "not an absolute URL"],
    ["/relative/path", "not an absolute URL"],
    ["http://example.com/", "not https"],
    ["https://user@example.com/", "contains credentials"],
    ["https://LOCALHOST./", "local host name"],
    ["https://127.1/", "local or private address"],
    ["https://[fd12:3456::1]/", "local or private address"],
  ];
  for (const [raw, reason] of cases) {
    assert.deepEqual(classifySource(raw), { kind: "inert", reason }, JSON.stringify(raw));
  }
});
