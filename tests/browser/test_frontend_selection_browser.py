"""C1 in a real browser: two independent prebuilt clients, switch and fallback.

The clients are plain HTML and JavaScript modules with a stylesheet, a
self-hosted font and an image, all document-relative. They follow the public
trust contract (fragment nonce, exchange, session-context reload). The stock
client, the package launcher and recovery stay reachable throughout, and a
broken custom client is replaced only through the public selection API. The
stock client's own exits to the launcher and recovery wait for its unsent and
unresolved requests to be exported and then disposed of or resolved.
"""

from __future__ import annotations

import hashlib
import json
import struct
import time
import zlib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from millrace_os import frontend
from millrace_os import presentation as p
from millrace_os.contracts import schema_artifacts

expect = pytest.importorskip(
    "playwright.sync_api", reason="the optional browser extra is not installed"
).expect

FRONTEND = "/api/v1/client/frontend"
POSTS = "**/api/v1/router/requests"
STRICT = "/api/v1/router/views/requests/"
# A fixed fake start, as in the UI3 control journeys: with the page clock
# paused, time moves only if a case advances it, so no automatic read starts.
CLOCK_START = 1_900_000_000_000
TRUST = b"""const same = {
  mode: "same-origin", credentials: "same-origin", redirect: "error", cache: "no-store",
};
let csrf = null; // lexical only: never in the DOM, storage or history
export async function signIn() {
  const nonce = location.hash.slice(1);
  history.replaceState(history.state, "", location.pathname + location.search);
  const reply = nonce
    ? await fetch("/api/v1/auth/exchange", {
      ...same, method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({nonce}),
    })
    : await fetch("/api/v1/auth/session-context", {
      ...same, headers: {"X-Millrace-Session-Context": "1"},
    });
  if (!reply.ok) throw new Error(`sign-in ${reply.status}`);
  csrf = (await reply.json()).csrf;
  return csrf ? (nonce ? "exchange" : "session-context") : "none";
}
export async function read(path) {
  const reply = await fetch(path, same);
  if (!reply.ok) throw new Error(`${path} ${reply.status}`);
  return reply.json();
}
"""
SHOW = b"""const show = (id, text) => {
  document.getElementById(id).textContent = text;
};
signIn().then(async (how) => {
  show("trust", how);
  show("target", new URLSearchParams(location.search).get("home") ?? "inventory");
  show("effective", (await read("/api/v1/client/frontend")).effective.base);
  await document.fonts.load("16px Body").catch(() => {});
  document.body.dataset.ready = "yes";
}).catch((error) => show("trust", `failed: ${error.message}`));
"""


def png() -> bytes:
    """One valid 1x1 RGB PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30"))
        + chunk(b"IEND", b"")
    )


def body(title: bytes, extra: bytes = b"") -> bytes:
    return (
        b'<main><h1>' + title + b'</h1><p id="trust"></p><p id="target"></p>'
        b'<p id="effective"></p>' + extra + b"</main>"
    )


CLIENT_A = {
    "index.html": b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
    b'<title>Client A</title><link rel="stylesheet" href="./assets/app.css">'
    b'<script type="module" src="./assets/app.js"></script></head><body>'
    + body(b"Client A", b'<img id="logo" src="./img/logo.png" alt="Client A logo">')
    + b"</body></html>",
    "assets/app.css": b'@font-face{font-family:Body;src:url("../fonts/body.woff2")'
    b' format("woff2")}body{font-family:Body,sans-serif}h1{color:rgb(1, 2, 3)}\n',
    "assets/app.js": b'import {signIn, read} from "./trust.js";\n' + SHOW,
    "assets/trust.js": TRUST,
    "fonts/body.woff2": b"wOF2" + bytes(60),
    "img/logo.png": png(),
}
CLIENT_B = {
    "index.html": b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
    b'<title>Client B</title><link rel="stylesheet" href="./b.css">'
    b'<script type="module" src="./main.mjs"></script></head><body>'
    + body(b"Client B")
    + b"</body></html>",
    "b.css": b"h1{color:rgb(4, 5, 6)}\n",
    "lib/session.mjs": TRUST,
    "main.mjs": b'import {signIn, read} from "./lib/session.mjs";\n' + SHOW,
}
CLIENT_C = {
    "index.html": b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
    b'<title>Client C</title><script type="module" src="./boom.js"></script>'
    b"</head><body><h1>Client C</h1></body></html>",
    "boom.js": b'throw new Error("broken custom client");\n',
}


def write_client(root: Path, files: dict[str, bytes], frontend_id: str) -> str:
    """One complete prebuilt closure with explicit modes; returns its key."""
    for name, content in files.items():
        target = root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o644)
        for folder in target.parents:
            folder.chmod(0o755)
            if folder == root:
                break
    digests = {e.contract_id: e.sha256 for e in schema_artifacts()[1].schemas}
    manifest = {
        "format": frontend.MANIFEST_FORMAT,
        "frontend_id": frontend_id,
        "frontend_version": "1.0.0",
        "entry": "index.html",
        "assets": [
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "media_type": frontend.MEDIA_TYPES["." + name.rsplit(".", 1)[1]],
            }
            for name, content in sorted(files.items())
        ],
        "api_version": "v1",
        "required_contracts": [
            {"contract_id": "FrontendState", "version": 1,
             "sha256": digests["FrontendState"]}
        ],
        "required_capabilities": ["client.frontend.read"],
        "presentation_format": None,
        "default_presentation": None,
    }
    (root / frontend.MANIFEST_NAME).write_text(json.dumps(manifest))
    (root / frontend.MANIFEST_NAME).chmod(0o644)
    return hashlib.sha256(p.canonical_json_bytes(manifest)).hexdigest()


def choose(service, desired: dict, version):
    """Select through the public API (as CLI/MCP do); return the new version."""
    if desired["kind"] == "local":
        validated = service.http.post(
            FRONTEND + "/validate", json={"path": desired["path"]}
        )
        assert validated.json()["bundle_sha256"] == desired["bundle_sha256"]
    response = service.http.put(
        FRONTEND, json={"expected_version": version, "desired": desired}
    )
    assert response.status_code == 200, response.text
    return response.json()["version"]


def local(root: Path, bundle_key: str) -> dict:
    return {"kind": "local", "path": str(root), "bundle_sha256": bundle_key}


def ready(page, trust: str):
    expect(page.locator("body")).to_have_attribute("data-ready", "yes")
    expect(page.locator("#trust")).to_have_text(trust)


def color(page) -> str:
    return page.evaluate("getComputedStyle(document.querySelector('h1')).color")


def test_two_independent_clients_sign_in_switch_and_keep_an_open_page(
    live, page, evidence
):
    service = live("two-clients")
    a_root, b_root = service.root / "client-a", service.root / "client-b"
    a = write_client(a_root, CLIENT_A, "example.a")
    b = write_client(b_root, CLIENT_B, "example.b")
    assert a != b
    version = choose(service, local(a_root, a), None)
    a_base, b_base = f"/ui/local/{a}/", f"/ui/local/{b}/"
    nonce = service.nonce()
    with (
        page.expect_response(lambda r: urlsplit(r.url).path == a_base) as shell,
        page.expect_response(lambda r: r.url.endswith("/fonts/body.woff2")) as font,
    ):
        page.goto(service.base + "/?home=default#" + nonce)
    # `/` resolved the effective base and kept the target; the client removed
    # the nonce fragment before signing in with it.
    assert page.url == service.base + a_base + "?home=default"
    assert shell.value.headers["content-security-policy"] == frontend.SHELL_CSP
    assert shell.value.headers["cache-control"] == "no-store"
    ready(page, "exchange")
    expect(page.locator("#target")).to_have_text("default")
    expect(page.locator("#effective")).to_have_text(a_base)
    assert color(page) == "rgb(1, 2, 3)"  # the relative stylesheet applied
    page.wait_for_function(
        "() => { const logo = document.getElementById('logo');"
        " return logo.complete && logo.naturalWidth === 1; }"
    )
    assert (font.value.status, font.value.headers["content-type"]) == (
        200,
        "font/woff2",
    )
    assert page.evaluate("window.__csp") == []
    assert nonce not in page.content() and "#" not in page.url
    page.screenshot(path=str(evidence / "frontend-client-a.png"), full_page=True)

    # Reload: the same session through session-context, no new nonce.
    page.reload()
    ready(page, "session-context")

    # Switching never replaces the open page; its later asset reads refuse.
    choose(service, local(b_root, b), version)
    expect(page.locator("h1")).to_have_text("Client A")
    assert page.url == service.base + a_base + "?home=default"
    status = page.evaluate(
        "fetch('./assets/app.js', {cache: 'no-store'}).then((reply) => reply.status)"
    )
    assert status == 503
    # A new navigation reaches the new client, which signs itself in.
    page.goto(service.base + "/?home=default")
    assert page.url == service.base + b_base + "?home=default"
    ready(page, "session-context")
    expect(page.locator("h1")).to_have_text("Client B")
    expect(page.locator("#effective")).to_have_text(b_base)
    assert color(page) == "rgb(4, 5, 6)"
    assert page.evaluate("window.__csp") == []
    page.screenshot(path=str(evidence / "frontend-client-b.png"), full_page=True)
    state = service.http.get(FRONTEND).json()
    assert state["desired"] == local(b_root, b) and state["reason"] is None


def test_fallback_launcher_stock_recovery_and_a_broken_client_reset(
    live, page, evidence
):
    service = live("fallback")
    a_root, c_root = service.root / "client-a", service.root / "client-c"
    a = write_client(a_root, CLIENT_A, "example.a")
    c = write_client(c_root, CLIENT_C, "example.c")
    version = choose(service, local(c_root, c), None)

    # A selected client whose own code is broken still loads as its closure.
    with page.expect_event("pageerror") as failure:
        page.goto(service.base + "/#" + service.nonce())
    assert "broken custom client" in str(failure.value)
    expect(page.locator("h1")).to_have_text("Client C")

    # The package launcher signs in on its own and never runs bundle code.
    page.goto(service.base + "/client#" + service.nonce())
    assert page.url == service.base + "/client"
    status = page.locator("#status")
    expect(status).to_have_text("The selected frontend is in use.")
    expect(page.locator("#desired")).to_contain_text("example.c 1.0.0")
    expect(page.locator("#desired")).to_contain_text(c)
    assert page.evaluate("window.__csp") == []
    page.screenshot(path=str(evidence / "frontend-launcher.png"), full_page=True)
    page.get_by_role("link", name="Open stock frontend").click()
    expect(page.get_by_role("heading", name="State homes", level=1)).to_be_visible()
    assert urlsplit(page.url).path == "/ui/stock/"

    # Reset through the public API (the CLI/MCP reset): stock is effective.
    version = choose(service, {"kind": "stock"}, version)
    page.goto(service.base + "/")
    assert urlsplit(page.url).path == "/ui/stock/"

    # A selected client whose files fail falls back to complete stock.
    version = choose(service, local(a_root, a), version)
    font = a_root / "fonts" / "body.woff2"
    saved = font.read_bytes()
    font.unlink()
    page.goto(service.base + "/?home=default")
    assert page.url == service.base + "/ui/stock/?home=default"
    # The complete stock client says why it stands in, and links out.
    notice = page.get_by_role("status").filter(has_text="is not available")
    expect(notice).to_contain_text("The selected frontend example.a is not available")
    expect(notice).to_contain_text("frontend_unavailable")
    expect(notice).to_contain_text("The selection is kept.")
    launcher = notice.get_by_role("link", name="frontend launcher")
    expect(launcher).to_have_attribute("href", "/client")
    expect(notice.get_by_role("link", name="Recovery")).to_have_attribute(
        "href", "/recovery"
    )
    assert page.evaluate("window.__csp") == []
    page.screenshot(path=str(evidence / "frontend-stock-notice.png"), full_page=True)
    unavailable = page.goto(service.base + f"/ui/local/{a}/")
    assert unavailable.status == 503
    expect(page.get_by_role("heading", level=1)).to_have_text(
        "This frontend is not available"
    )
    expect(page.locator("main")).to_contain_text("frontend_unavailable")
    assert page.evaluate("window.__csp") == []
    page.screenshot(path=str(evidence / "frontend-unavailable.png"), full_page=True)
    page.get_by_role("link", name="Open the stock frontend").click()
    expect(page.get_by_role("heading", name="State homes", level=1)).to_be_visible()
    page.goto(service.base + "/client")
    expect(status).to_contain_text("not available: its files are missing")
    state = service.http.get(FRONTEND).json()
    assert state["desired"] == local(a_root, a)
    assert (state["reason"], state["effective"]["kind"]) == (
        "frontend_unavailable",
        "stock",
    )
    page.goto(service.base + "/recovery")
    expect(page.get_by_role("heading", name="Millrace OS Recovery")).to_be_visible()

    # Restored files make it effective again without another selection.
    font.write_bytes(saved)
    font.chmod(0o644)
    page.goto(service.base + "/")
    assert page.url == service.base + f"/ui/local/{a}/"
    ready(page, "session-context")
    assert service.http.get(FRONTEND).json()["version"] == version


def drop_posts(route):
    """Abort request POSTs in the browser, so none reaches the service."""
    if route.request.method == "POST":
        route.abort()
    else:
        route.continue_()


def review(form) -> str:
    """Review the form's request; return its exact reviewed bytes."""
    form.get_by_role("button", name="Review request").click()
    body = form.locator(".request-body")
    expect(body).to_be_visible()
    return body.inner_text()


def exported(page, button) -> dict:
    with page.expect_download() as download:
        button.click()
    return json.loads(Path(download.value.path()).read_text(encoding="utf-8"))


def until_held(page, held, count):
    """The UI3 journeys' bounded wait for a routed request to reach its hold."""
    deadline = time.monotonic() + 4
    while len(held) < count:
        assert time.monotonic() < deadline, "the held request was never issued"
        page.wait_for_timeout(10)


def test_stock_exits_wait_for_unsent_and_unresolved_requests(
    live, page, evidence, review_gate
):
    service = live("leaving")
    instance = service.create(review_gate, workspaces=("main", "side"))
    a_root = service.root / "client-a"
    a = write_client(a_root, CLIENT_A, "example.a")
    choose(service, local(a_root, a), None)
    (a_root / "fonts" / "body.woff2").unlink()  # unavailable before stock boots
    posted: list[str | None] = []
    page.on(
        "request",
        lambda sent: posted.append(sent.post_data)
        if sent.method == "POST" and urlsplit(sent.url).path == POSTS[2:]
        else None,
    )
    stock = service.base + f"/ui/stock/?home=default&instance={instance}"
    # The request monitor never reads on its own here: the only receipt read
    # below is the operator's Observe.
    page.clock.install(time=CLOCK_START)
    page.clock.pause_at(CLOCK_START + 1_000)
    page.goto(service.base + f"/?home=default&instance={instance}#" + service.nonce())
    assert page.url == stock
    notices = page.get_by_role("status").filter(has_text="is not available")
    expect(notices).to_contain_text("The selected frontend example.a is not available")
    launcher = notices.get_by_role("link", name="frontend launcher")
    recovery = notices.get_by_role("link", name="Recovery")
    form = page.get_by_role("region", name="Start a run")

    # An unsent reviewed Start holds both exits; its ID and bytes stay as shown.
    first = review(form)
    unsent = json.loads(first)["request"]
    for link in (launcher, recovery):
        link.click()
        expect(notices).to_contain_text("This page stayed open")
        expect(notices).to_contain_text("an unsent reviewed request")
        assert page.url == stock
    expect(form.locator(".request-body")).to_have_text(first)
    document = exported(
        page, form.get_by_role("button", name="Export reviewed request JSON")
    )
    assert document == {
        "format": "millrace-os-stock-request-intent/v1",
        "home": "default",
        "request_id": unsent["request_id"],
        "body": first,
        "sent": False,
        "binding": None,
        "conflicting_bodies": [],
    }
    form.get_by_role("button", name="Discard", exact=True).click()
    expect(form.locator(".request-body")).to_have_count(0)
    expect(notices).to_contain_text("Nothing in this tab is unsent or unresolved now")
    assert page.url == stock and posted == []

    # A Start whose POST is dropped before admission stays retained and unknown.
    second = review(form)
    retained = json.loads(second)["request"]
    assert retained["request_id"] != unsent["request_id"]
    page.route(POSTS, drop_posts)
    try:
        form.get_by_role("button", name="Send", exact=True).click()
        requests = page.get_by_role("region", name="Requests in this tab")
        entry = requests.get_by_role("listitem").filter(has_text=retained["request_id"])
        expect(entry).to_contain_text("Delivery unknown")
    finally:
        page.unroute(POSTS, drop_posts)
    assert posted == [second]
    strict = service.http.get(
        STRICT + retained["request_id"],
        params={"home": "default", "instance_id": instance},
    )
    assert strict.status_code == 404  # Core never received it; no run started

    launcher.click()
    expect(notices).to_contain_text("the unresolved request " + retained["request_id"])
    assert page.url == stock
    # A modified click opens another tab and is never held.
    with page.context.expect_page() as opened:
        launcher.click(modifiers=["ControlOrMeta"])
    try:
        opened.value.wait_for_load_state()
        assert urlsplit(opened.value.url).path == "/client"
    finally:
        opened.value.close()
    assert page.url == stock
    document = exported(page, entry.get_by_role("button", name="Export request JSON"))
    assert (document["request_id"], document["body"], document["sent"]) == (
        retained["request_id"],
        second,
        True,
    )
    # With Retire's confirmation open, an Observe read in flight disables it and
    # keeps both exits held; the read is held in the browser, so it sends nothing.
    entry.get_by_role("button", name="Retire", exact=True).click()
    confirm = entry.get_by_role("button", name="Retire unresolved request")
    expect(confirm).to_be_enabled()
    reads = "**" + STRICT + retained["request_id"] + "*"
    held: list = []

    def hold(route):
        held.append(route)

    page.route(reads, hold)
    try:
        entry.get_by_role("button", name="Observe").click()
        until_held(page, held, 1)
        expect(confirm).to_be_disabled()
        for link in (launcher, recovery):
            link.click()
            expect(notices).to_contain_text(retained["request_id"])
            assert page.url == stock
        held.pop().continue_()  # the service's typed 404: still unknown
        expect(entry).to_contain_text("No observation is available (HTTP 404)")
        expect(confirm).to_be_enabled()
    finally:
        page.unroute(reads, hold)
        for route in held:
            route.abort()
    # Cancelling Retire keeps the exact retained request, and the exit stays held.
    entry.get_by_role("button", name="Cancel", exact=True).click()
    intents = page.evaluate("history.state")["millrace_os_intents"]["intents"]
    assert [item["body"] for item in intents] == [second]
    expect(entry).to_contain_text("Delivery unknown")
    recovery.click()
    expect(notices).to_contain_text(retained["request_id"])
    assert page.url == stock
    page.screenshot(path=str(evidence / "frontend-exit-held.png"), full_page=True)

    # A confirmed Retire forgets it here only; the page still waits to be asked.
    entry.get_by_role("button", name="Retire", exact=True).click()
    entry.get_by_role("button", name="Retire unresolved request").click()
    expect(entry).to_have_count(0)
    expect(notices).to_contain_text("Nothing in this tab is unsent or unresolved now")
    assert page.url == stock
    # Another deliberate activation leaves, and leaving sends nothing.
    launcher.click()
    page.wait_for_url(service.base + "/client")
    expect(page.locator("#status")).to_contain_text("is not available")
    assert posted == [second]
