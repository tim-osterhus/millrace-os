// Which report `sources` URLs may render as a clickable link.
//
// A link needs an absolute `https:` URL with a host and no userinfo. The host
// must not be `localhost` (or a `.localhost` name, trailing dot included) and
// must not be a loopback, private, shared, link-local or unspecified IP
// literal. Hosts are classified after WHATWG URL parsing, which already turns
// numeric and hex IPv4 shorthands into dotted quads and serializes IPv6
// (including IPv4-mapped forms) in brackets. No DNS lookup happens here, so a
// public-looking name is not a reachability or safety claim. Anything else
// renders as inert text; the source itself is never refused or rewritten.

export type SourceLink =
  | { kind: "link"; href: string; host: string }
  | { kind: "inert"; reason: string };

function ipv4Blocked(octets: number[]): boolean {
  const [a, b] = octets;
  return (
    a === 0 || // "this network" and unspecified
    a === 10 ||
    a === 127 ||
    (a === 100 && b >= 64 && b <= 127) || // shared address space
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168)
  );
}

function parseIpv4(host: string): number[] | null {
  const parts = host.split(".");
  if (parts.length !== 4 || !parts.every((part) => /^(0|[1-9][0-9]{0,2})$/.test(part))) return null;
  const octets = parts.map(Number);
  return octets.every((octet) => octet <= 255) ? octets : null;
}

/** Eight 16-bit groups from a WHATWG-serialized IPv6 literal (no brackets). */
function parseIpv6(literal: string): number[] | null {
  const halves = literal.split("::");
  if (halves.length > 2) return null;
  const groups = (half: string) => (half === "" ? [] : half.split(":"));
  const head = groups(halves[0]);
  const tail = halves.length === 2 ? groups(halves[1]) : [];
  const missing = 8 - head.length - tail.length;
  if (halves.length === 1 ? head.length !== 8 : missing < 1) return null;
  const all = [...head, ...Array<string>(halves.length === 2 ? missing : 0).fill("0"), ...tail];
  if (!all.every((group) => /^[0-9a-f]{1,4}$/.test(group))) return null;
  return all.map((group) => Number.parseInt(group, 16));
}

function ipv6Blocked(groups: number[]): boolean {
  const zeroThrough = (end: number) => groups.slice(0, end).every((group) => group === 0);
  if (zeroThrough(8)) return true; // ::
  if (zeroThrough(7) && groups[7] === 1) return true; // ::1
  if ((groups[0] & 0xfe00) === 0xfc00) return true; // fc00::/7 unique local
  if ((groups[0] & 0xffc0) === 0xfe80) return true; // fe80::/10 link-local
  if ((groups[0] & 0xffc0) === 0xfec0) return true; // fec0::/10 former site-local
  const embedded = [groups[6] >> 8, groups[6] & 0xff, groups[7] >> 8, groups[7] & 0xff];
  if (zeroThrough(5) && groups[5] === 0xffff) return ipv4Blocked(embedded); // ::ffff:a.b.c.d
  if (zeroThrough(6)) return ipv4Blocked(embedded); // deprecated ::a.b.c.d
  if (groups[0] === 0x64 && groups[1] === 0xff9b && groups.slice(2, 6).every((group) => group === 0)) {
    return ipv4Blocked(embedded); // 64:ff9b::/96 translated
  }
  return false;
}

export function classifySource(raw: unknown): SourceLink {
  if (typeof raw !== "string") return { kind: "inert", reason: "not text" };
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { kind: "inert", reason: "not an absolute URL" };
  }
  if (url.protocol !== "https:") return { kind: "inert", reason: "not https" };
  if (url.username !== "" || url.password !== "") return { kind: "inert", reason: "contains credentials" };
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  if (host === "") return { kind: "inert", reason: "no host" };
  if (host === "localhost" || host.endsWith(".localhost")) return { kind: "inert", reason: "local host name" };
  if (host.startsWith("[")) {
    const groups = parseIpv6(host.slice(1, -1));
    if (groups === null || ipv6Blocked(groups)) return { kind: "inert", reason: "local or private address" };
  } else {
    const octets = parseIpv4(host);
    if (octets !== null && ipv4Blocked(octets)) return { kind: "inert", reason: "local or private address" };
  }
  return { kind: "link", href: url.href, host };
}
