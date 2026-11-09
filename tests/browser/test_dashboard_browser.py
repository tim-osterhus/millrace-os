"""Real bundled-browser journeys over the packaged stock client (UI2).

Each journey drives the exact packaged closure through a real OS service. Real
runs use command-only Core workflows; crowded and uncertain states come from a
fixed fake router projection. Screenshots and CSP logs go to the evidence
directory; no journey invokes a native provider.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time

import pytest

expect = pytest.importorskip(
    "playwright.sync_api", reason="the optional browser extra is not installed"
).expect


def csp_violations(page) -> list[dict]:
    return page.evaluate("window.__csp")


def fetches(page) -> list[dict]:
    return page.evaluate("window.__fetches")


def shot(page, evidence, name):
    page.screenshot(path=str(evidence / f"{name}.png"), full_page=True)


def heading(page, name):
    return page.get_by_role("heading", name=name, level=1)


def no_page_overflow(page) -> bool:
    return page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 1"
    )


def real_run(live, review_gate, name="service"):
    service = live(name)
    instance, run = service.create_and_start(review_gate, workspaces=("main", "side"))
    service.settled(instance, run)
    return service, instance, run


def test_nonce_journey_from_inventory_to_graph_inspector_and_history(
    live, page, page_errors, evidence, review_gate
):
    service, instance, run = real_run(live, review_gate)
    nonce = service.nonce()
    page.goto(service.url(nonce=nonce))
    expect(heading(page, "State homes")).to_be_visible()
    first = fetches(page)[0]
    assert first["url"].endswith("/api/v1/auth/exchange") and first["hash"] == ""
    assert nonce not in page.url and "#" not in page.url

    page.get_by_role("link", name="default", exact=True).click()
    expect(heading(page, "Home inventory")).to_be_visible()
    page.get_by_role("link", name=instance).click()
    expect(heading(page, "Instance")).to_be_visible()
    page.get_by_role("link", name=run).click()
    expect(heading(page, "Run")).to_be_visible()
    assert f"instance={instance}" in page.url and f"run={run}" in page.url

    status = page.get_by_role("region", name="Status")
    for text in (run, instance, "Run status", "Core quiescence", "Effective writers"):
        expect(status).to_contain_text(text)
    for region in ("Planes", "Current work and blockers", "Graph", "Inspector"):
        expect(page.get_by_role("heading", name=region)).to_be_visible()

    nodes = page.get_by_role("table", name="Nodes of plane main")
    expect(nodes).to_be_visible()
    nodes.get_by_role("button", name="work").click()
    inspector = page.get_by_role("region", name="Inspector")
    expect(inspector).to_contain_text("Captured routes")
    expect(inspector).to_contain_text("RETRY")
    expect(page.locator(".react-flow__node").first).to_be_visible()

    page.get_by_role("button", name="Invocations").click()
    rows = page.get_by_role("table", name="Invocations, newest attempt first")
    expect(rows).to_be_visible()
    rows.get_by_role("button").first.click()
    expect(inspector).to_contain_text("Original (completion)")
    expect(inspector).to_contain_text("Effective")
    shot(page, evidence, "journey-run-precision")

    page.go_back()
    expect(heading(page, "Instance")).to_be_visible()
    page.go_forward()
    expect(heading(page, "Run")).to_be_visible()
    expect(page.get_by_role("region", name="Status")).to_contain_text(run)
    assert csp_violations(page) == []
    assert page_errors == []


def test_trust_material_never_reaches_dom_history_storage_or_links(live, page):
    service = live()
    nonce = service.nonce()
    with page.expect_response("**/api/v1/auth/exchange") as exchanged:
        page.goto(service.url({"home": "default"}, nonce=nonce))
    csrf = exchanged.value.json()["csrf"]
    expect(heading(page, "Home inventory")).to_be_visible()
    html = page.content()
    assert nonce not in html and csrf not in html
    assert nonce not in page.url and "#" not in page.url
    state = page.evaluate("JSON.stringify(history.state)")
    assert csrf not in state and nonce not in state
    assert page.evaluate("[localStorage.length, sessionStorage.length]") == [0, 0]
    assert page.evaluate("indexedDB.databases().then((items) => items.length)") == 0
    assert page.evaluate("document.cookie") == ""
    cookie = next(
        item for item in page.context.cookies() if item["name"] == "millrace_os_session"
    )
    assert cookie["httpOnly"] is True and cookie["sameSite"] == "Strict"
    hrefs = page.eval_on_selector_all(
        "a[href]", "items => items.map((item) => item.getAttribute('href'))"
    )
    assert hrefs and all(csrf not in href and nonce not in href for href in hrefs)


def test_reload_restores_the_session_without_renewal_and_expiry_stops_reads(
    live, page
):
    service = live()
    now = [time.monotonic()]
    service.service.trust.clock = lambda: now[0]
    page.goto(service.url({"home": "default"}, nonce=service.nonce()))
    expect(heading(page, "Home inventory")).to_be_visible()
    sid = next(
        item["value"]
        for item in page.context.cookies()
        if item["name"] == "millrace_os_session"
    )
    before = service.service.trust.sessions[sid].expires
    with page.expect_response("**/api/v1/auth/session-context") as restored:
        page.reload()
    assert restored.value.status == 200
    assert restored.value.request.headers["x-millrace-session-context"] == "1"
    expect(heading(page, "Home inventory")).to_be_visible()
    assert service.service.trust.sessions[sid].expires == before

    now[0] += 901
    page.get_by_role("button", name="Refresh").click()
    # The service notice, not the inventory's own failure note, names the command.
    ended = page.get_by_role("status").filter(has_text="Reads and writes are stopped")
    expect(ended.get_by_text("The browser session ended")).to_be_visible()
    expect(ended.get_by_text("millrace-os open --home default")).to_be_visible()
    count = len(fetches(page))
    page.get_by_role("button", name="Refresh").click()
    page.wait_for_timeout(300)
    assert len(fetches(page)) == count  # reads stop after expiry


def test_navigation_cannot_read_session_context_and_recovery_cannot_mount(
    live, page
):
    service = live()
    page.goto(service.url(nonce=service.nonce()))
    expect(heading(page, "State homes")).to_be_visible()
    response = page.goto(service.base + "/api/v1/auth/session-context")
    assert response.status == 403
    assert response.json()["code"] == "session_context_forbidden"

    page.goto(service.base + "/recovery#" + service.nonce(recovery=True))
    expect(page.get_by_text("Authenticated local operator")).to_be_visible()
    page.goto(service.url({"home": "default"}))
    expect(page.get_by_text("recovery_session_only")).to_be_visible()
    assert page.get_by_role("table").count() == 0


def test_keyboard_only_paths_split_resize_and_zoomed_layouts(
    live, page, evidence, review_gate
):
    service, instance, run = real_run(live, review_gate)
    page.goto(service.url(nonce=service.nonce()))
    for name, title in (
        ("default", "Home inventory"),
        (instance, "Instance"),
        (run, "Run"),
    ):
        page.get_by_role("link", name=name, exact=True).focus()
        page.keyboard.press("Enter")
        expect(heading(page, title)).to_be_visible()
        expect(heading(page, title)).to_be_focused()

    table = page.get_by_role("table", name="Nodes of plane main")
    table.get_by_role("button", name="check").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("region", name="Inspector")).to_contain_text("check")

    separator = page.get_by_role("separator", name="Resize panels")
    separator.focus()
    before = separator.get_attribute("aria-valuenow")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    expect(separator).not_to_have_attribute("aria-valuenow", before or "")
    expect(separator).to_be_focused()  # a keyboard resize never remounts the split

    for width, label in ((640, "zoom-200"), (390, "narrow")):
        page.set_viewport_size({"width": width, "height": 900})
        status = page.get_by_role("region", name="Status")
        expect(status).to_contain_text(run)
        box = status.get_by_text(run, exact=True).bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width
        assert no_page_overflow(page), label
        expect(page.get_by_role("navigation", name="Actions")).to_be_visible()
        shot(page, evidence, f"run-{label}")
    assert csp_violations(page) == []


def test_the_closure_loads_locally_with_every_other_origin_blocked(
    live, browser, evidence, review_gate
):
    service, instance, run = real_run(live, review_gate)
    context = browser.new_context()
    attempted: list[str] = []
    served: list[tuple[str, str, int]] = []

    def route(handle):
        if handle.request.url.startswith(service.base):
            handle.continue_()
        else:
            attempted.append(handle.request.url)
            handle.abort()

    context.route("**/*", route)
    page = context.new_page()
    page.on(
        "response",
        lambda response: served.append(
            (response.url, response.headers.get("content-type", ""), response.status)
        )
        if "/ui/stock/" in response.url
        else None,
    )
    target = {"home": "default", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    expect(page.get_by_role("table", name="Nodes of plane main")).to_be_visible()
    page.get_by_role("button", name="Invocations").click()
    page.get_by_role("button", name="Read published outputs").click()
    expect(page.get_by_text("No outputs are published for this run.")).to_be_visible()
    context.close()
    assert attempted == []
    assert served and all(status == 200 for _, _, status in served)
    assert any(kind.startswith("text/javascript") for _, kind, _ in served)
    (evidence / "offline-served.json").write_text(json.dumps(served, indent=1))


def _presentation(service):
    return service.http.get("/api/v1/client/presentation").json()


def test_presets_shared_preferences_restart_conflict_and_unreadable_fallback(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("prefs", router_client=fake_router, homes={"crowded": evidence})
    target = {"home": "crowded", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    expect(heading(page, "Run")).to_be_visible()
    menu = page.locator("details.layout-menu")
    menu.locator("summary").click()

    for preset, hidden in (
        ("command", "Graph (hidden)"),
        ("studio", "Planes (hidden)"),
    ):
        page.get_by_role("combobox", name="Presentation").select_option(
            f"bundled:{preset}"
        )
        expect(page.get_by_role("navigation", name="Actions")).to_contain_text(hidden)
        expect(page.get_by_role("region", name="Status")).to_contain_text(run)
        shot(page, evidence, f"preset-{preset}")
    page.get_by_role("combobox", name="Presentation").select_option("bundled:command")

    page.get_by_label("Name").fill("Night shift")
    page.get_by_role("button", name="Save as new").click()
    expect(page.get_by_text("Saved “Night shift”.")).to_be_visible()
    store = _presentation(service)["store"]
    stock = next(item for item in store["namespaces"] if item["frontend_id"] == "stock")
    saved = next(item for item in stock["configs"] if item["name"] == "Night shift")
    assert saved["payload"]["preset"] == "command"

    page.get_by_role("button", name="Make default").click()
    expect(page.get_by_text("is the default for the next page load")).to_be_visible()
    page.get_by_label("Name").fill("Night shift two")
    page.get_by_role("button", name="Duplicate as").click()
    expect(page.get_by_text("Duplicated as “Night shift two”.")).to_be_visible()
    page.get_by_label("Name").fill("Night shift 2")
    page.get_by_role("button", name="Rename to").click()
    expect(page.get_by_text("Renamed to “Night shift 2”.")).to_be_visible()
    with page.expect_download() as exported:
        page.get_by_role("button", name="Export").click()
    document = json.loads(open(exported.value.path(), encoding="utf-8").read())
    assert document["format"] == "millrace-os-presentation-config/v2"
    page.get_by_label("Import a saved presentation").set_input_files(
        exported.value.path()
    )
    expect(page.get_by_text("as a new saved presentation")).to_be_visible()
    page.get_by_role("button", name="Delete").click()
    expect(page.get_by_text("Deleted")).to_be_visible()
    names = sorted(
        config["name"]
        for config in next(
            item
            for item in _presentation(service)["store"]["namespaces"]
            if item["frontend_id"] == "stock"
        )["configs"]
    )
    assert names == ["Night shift", "Night shift 2"]

    # A new origin on the same config root reads the same saved default.
    service.restart()
    page.goto(service.url(target, nonce=service.nonce()))
    expect(page.locator("details.layout-menu summary")).to_contain_text("Night shift")
    requests = [item["url"] for item in fetches(page)]
    assert not any("/revisions/" in url for url in requests)  # Graph is hidden
    page.get_by_role("navigation", name="Actions").get_by_role(
        "button", name="Graph (hidden)"
    ).click()
    expect(page.get_by_text("core_inspection_too_large")).to_be_visible()

    # Node selection reveals a hidden Inspector without changing the target.
    url = page.url
    planes = page.get_by_role("region", name="Planes")
    planes.get_by_role("button").first.click()
    expect(page.get_by_role("heading", name="Inspector")).to_be_visible()
    assert page.url == url

    # Another client writes between this page's read and its next write.
    from millrace_os import presentation as helpers

    state = _presentation(service)
    namespace = {
        "frontend_id": "stock",
        "presentation_format": "millrace-stock-presentation/v1",
    }
    payload = state["bundled_configs"][0]["payload"]
    other = helpers.create(state, namespace, "From the CLI", payload)
    written = service.http.put("/api/v1/client/presentation", json=other)
    assert written.status_code == 200, written.text
    page.locator("details.layout-menu summary").click()
    page.get_by_role("button", name="Make default").click()
    expect(page.get_by_text("changed in another client")).to_be_visible()
    stock = next(
        item
        for item in _presentation(service)["store"]["namespaces"]
        if item["frontend_id"] == "stock"
    )
    assert any(config["name"] == "From the CLI" for config in stock["configs"])

    # Newer or malformed saved data is preserved and never rewritten on mount.
    path = service.config.config_dir / "presentation.json"
    newer = b'{"format":"millrace-os-presentation-store/v9","namespaces":[]}'
    path.write_bytes(newer)
    os.chmod(path, 0o600)
    page.reload()
    expect(page.get_by_text("presentation_incompatible")).to_be_visible()
    expect(page.get_by_role("region", name="Status")).to_contain_text(run)
    assert path.read_bytes() == newer
    assert csp_violations(page) == []


def test_crowded_uncertain_and_stale_states_stay_truthful(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    fake = fake_router
    service = live("crowded", router_client=fake, homes={"crowded": evidence})
    page.goto(service.url({"home": "crowded"}, nonce=service.nonce()))
    expect(page.get_by_text("refused store_integrity_error")).to_be_visible()
    shot(page, evidence, "inventory-crowded")
    target = {"home": "crowded", "instance": instance, "run": run}
    page.goto(service.url(target))
    status = page.get_by_role("region", name="Status")
    for fact in (
        "may_live 1",
        "unknown 2",
        "running 3",
        "not quiescent",
        "learning_ack",
        run,
        instance,
    ):
        expect(status).to_contain_text(fact)
    planes = page.get_by_role("region", name="Planes")
    expect(planes).to_contain_text("operator_decision_required")
    sparse = planes.get_by_role("row", name="sparse")
    expect(sparse).to_contain_text("unknown")
    expect(page.get_by_text("no graph is shown for it")).to_be_visible()
    for width in (1440, 640, 390):
        page.set_viewport_size({"width": width, "height": 900})
        assert no_page_overflow(page), width
        shot(page, evidence, f"crowded-{width}")

    # Status keeps each plane's blocker code and targets when Studio hides
    # Planes and Current work.
    page.set_viewport_size({"width": 1440, "height": 900})
    page.locator("details.layout-menu summary").click()
    page.get_by_role("combobox", name="Presentation").select_option("bundled:studio")
    expect(page.get_by_role("heading", name="Planes")).to_have_count(0)
    blocked = "paused operator_decision_required at node_" + "y" * 50 + "_1"
    expect(status).to_contain_text(blocked)
    expect(status).to_contain_text("resume node_1")
    expect(status).to_contain_text("requires terminal other succeeded")
    page.locator("details.layout-menu summary").click()

    # Reconciled: recorded may_live stays visible beside effective quiescence.
    fake.reconciled = True
    page.get_by_role("button", name="Refresh").click()
    expect(status).to_contain_text(
        "quiescent: every invocation's effective writer is stopped"
    )
    expect(status).to_contain_text("may_live 1")
    expect(status).to_contain_text("stopped 81")
    shot(page, evidence, "crowded-reconciled-studio")

    fake.fail_summary = True
    page.get_by_role("button", name="Refresh").click()
    expect(status).to_contain_text("Stale: the latest read failed (router_read_busy)")
    expect(status).to_contain_text("may_live 1")  # the last observation stays visible
    shot(page, evidence, "crowded-stale")
    assert csp_violations(page) == []


def test_generic_report_escapes_text_links_safely_and_labels_downloads(
    live, page, evidence, review_gate
):
    from millrace_plus import workflow_views as views

    service, instance, run = real_run(live, review_gate)
    events = service.http.get(
        f"/api/v1/router/views/instances/{instance}/runs/{run}",
        params={"home": "default", "section": "transitions"},
    ).json()["data"]["items"]
    event = next(
        item
        for item in events
        if item["event_kind"] == "transition" and item["node_id"] == "work"
    )

    def representation(format_id, data):
        digest = hashlib.sha256(views.canonical_json_bytes(data)).hexdigest()
        return {
            "format": format_id,
            "media_type": "application/json",
            "sha256": digest,
            "data": data,
        }

    report = {
        "title": "Answer <script>alert(1)</script>",
        "paragraphs": ["First line\nsecond line", "<b>not bold</b>"],
        "facts": [{"label": "Answer", "value": "42"}],
        "tables": [
            {
                "caption": "Checks",
                "columns": ["Name", "Value"],
                "rows": [["answer", "42"], ["long", "x" * 400]],
            }
        ],
        "sources": [
            {"label": "Public source", "url": "https://example.com/source"},
            {"label": "Local", "url": "https://localhost/x"},
            {"label": "Loopback", "url": "https://127.1/x"},
            {"label": "Plain", "url": "http://example.com/"},
            {"label": "Script", "url": "javascript:alert(1)"},
        ],
    }
    payload = {"answer": 42, "ratio": 1.0}
    record = {
        "format": views.OUTPUT_FORMAT,
        "output_id": "answer-check",
        "instance_id": instance,
        "run_id": run,
        "plane_id": event["plane_id"],
        "node_id": event["node_id"],
        "invocation_id": event["completion_id"],
        "producing_revision_id": event["producing_revision_id"],
        "producer": {"id": "example.answer-exporter", "version": "1.0.0"},
        "label": "Answer check",
        "published_at": "2026-11-08T12:00:00Z",
        "representations": {
            "report": representation(views.REPORT_FORMAT, report),
            "json": representation("example.answer/v1", payload),
        },
    }
    published = service.http.post(
        "/api/v1/router/outputs", json={"home": "default", "record": record}
    )
    assert published.status_code == 200, published.text

    target = {"home": "default", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    page.get_by_role("button", name="Read published outputs").click()
    page.get_by_role("button", name="answer-check").click()
    rendered = page.locator(".report")
    expect(rendered).to_contain_text("Answer <script>alert(1)</script>")
    expect(rendered).to_contain_text("<b>not bold</b>")
    assert rendered.locator("script, b").count() == 0
    links = rendered.locator("a")
    expect(links).to_have_count(1)
    assert links.get_attribute("href") == "https://example.com/source"
    assert links.get_attribute("rel") == "noopener noreferrer"
    assert links.get_attribute("referrerpolicy") == "no-referrer"
    for reason in ("local host name", "local or private address", "not https"):
        expect(rendered).to_contain_text(f"not a link: {reason}")
    view = page.locator(".report-view")
    expect(view).to_contain_text("example.answer-exporter")
    expect(view).to_contain_text(record["representations"]["report"]["sha256"])
    expect(view).to_contain_text("exact API response envelope")
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Download JSON (API response)").click()
    envelope = json.loads(open(downloaded.value.path(), encoding="utf-8").read())
    assert envelope["contract"] == "millrace-os-workflow-output/v1"
    assert envelope["representation"]["name"] == "json"
    assert envelope["representation"]["sha256"] == (
        record["representations"]["json"]["sha256"]
    )
    shot(page, evidence, "report")
    assert csp_violations(page) == []


def test_an_ordinary_service_is_established_only_by_its_typed_404(live, page):
    ordinary = live("ordinary")
    page.goto(ordinary.url(nonce=ordinary.nonce()))
    expect(heading(page, "State homes")).to_be_visible()
    expect(page.get_by_text("Service context is")).to_have_count(0)
    page.locator("details.layout-menu summary").click()
    page.get_by_label("Name").fill("Mine")
    expect(page.get_by_role("button", name="Save as new")).to_be_enabled()


@pytest.mark.parametrize("mode", ["pending", "failed", "unreachable"])
def test_unresolved_context_never_enables_preference_writes(live, page, mode):
    from millrace_os.service import DemoContextPending

    def pending():
        raise DemoContextPending()

    def failed():
        raise RuntimeError("PRIVATE reader failure")

    reader = {"pending": pending, "failed": failed, "unreachable": None}[mode]
    service = live(mode, reader=reader)
    if mode == "unreachable":
        # A transport failure is not the typed ordinary 404.
        page.route("**/api/v1/demo/context", lambda handle: handle.abort())
    page.goto(service.url(nonce=service.nonce()))
    expect(page.get_by_text("Service context is")).to_be_visible()
    notice = "saving presentations and every control stay disabled"
    expect(page.get_by_text(notice)).to_be_visible()
    page.locator("details.layout-menu summary").click()
    page.get_by_label("Name").fill("Mine")
    for name in ("Save as new", "Duplicate as", "Make default"):
        expect(page.get_by_role("button", name=name)).to_be_disabled()
    expect(page.get_by_label("Import a saved presentation")).to_be_disabled()
    # In-memory presentation still changes; nothing is written.
    page.get_by_label("dark", exact=True).check()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert [item for item in fetches(page) if item["method"] == "PUT"] == []
    assert "PRIVATE" not in page.content()


NEXT_FRAMES = (
    "new Promise((done) => "
    "requestAnimationFrame(() => requestAnimationFrame(done)))"
)


class Held(list):
    """Requests paused by one hold, in arrival order; each released at most once."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.released: set[int] = set()

    def release(self, index, *, consumed=False):
        route = self[index]
        self.released.add(index)
        with self.page.expect_response(
            lambda response: response.request == route.request
        ) as reply:
            route.continue_()
        reply.value.finished()
        if consumed:
            # Network completion is not application consumption: wait for the
            # application's body reader of exactly this request to reach its end.
            wait_consumed(self.page, route.request)
        # Let the reply's render commit before asserting what is shown.
        self.page.evaluate(NEXT_FRAMES)


@contextlib.contextmanager
def hold(page, matches, method="GET"):
    """Pause matching requests inside the block until the test releases each.

    Leaving the block, even through a failed assertion, removes this exact
    handler and aborts every request it still pauses: nothing paused outlives
    the block, and no later read is intercepted.
    """
    held = Held(page)

    def handle(route):
        if route.request.method == method:
            held.append(route)
        else:
            route.continue_()

    page.route(matches, handle)
    try:
        yield held
    finally:
        page.unroute(matches, handle)
        for index, route in enumerate(held):
            if index not in held.released:
                route.abort()


def wait_held(page, held, count):
    deadline = time.monotonic() + 4
    while len(held) < count:
        assert time.monotonic() < deadline, "the held request was never issued"
        page.wait_for_timeout(10)


# The probed fetches issued for one exact request (method and resolved URL).
PROBED = (
    "(probe) => window.__fetches.filter((item) => item.method === probe.method"
    " && new URL(item.url, document.baseURI).href === probe.url)"
)


def wait_consumed(page, request):
    """Wait until the application's own reader of this request's body is done."""
    probe = {"url": request.url, "method": request.method}
    count = page.evaluate(f"(probe) => ({PROBED})(probe).length", probe)
    assert count == 1, f"{count} probed fetches match the released request"
    page.wait_for_function(f"(probe) => ({PROBED})(probe)[0].consumed", arg=probe)


def test_a_pending_save_never_marks_later_edits_saved_or_adopts_a_new_choice(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("save-race", router_client=fake_router, homes={"crowded": evidence})
    target = {"home": "crowded", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    summary = page.locator("details.layout-menu summary")
    summary.click()
    page.get_by_label("Name").fill("Base")
    page.get_by_role("button", name="Save as new").click()
    expect(page.get_by_text("Saved “Base”.")).to_be_visible()

    with hold(page, "**/api/v1/client/presentation", method="PUT") as held:
        page.get_by_label("light", exact=True).check()
        page.get_by_role("button", name="Save", exact=True).click()
        wait_held(page, held, 1)
        page.get_by_label("compact", exact=True).check()  # edited while pending
        held.release(0)
        settled = page.get_by_role("button", name="Make default")
        expect(settled).to_be_enabled()  # the write and its adoption have completed
        expect(page.get_by_text("Saved “Base”.")).to_be_visible()
        stock = next(
            item
            for item in _presentation(service)["store"]["namespaces"]
            if item["frontend_id"] == "stock"
        )
        base = next(item for item in stock["configs"] if item["name"] == "Base")
        stored = base["payload"]
        assert (stored["theme"], stored["density"]) == ("light", "comfortable")
        expect(summary).to_contain_text("(unsaved changes)")
        expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()

        # A different presentation chosen while a rename is pending stays chosen.
        page.get_by_label("Name").fill("Renamed base")
        page.get_by_role("button", name="Rename to").click()
        wait_held(page, held, 2)
        chooser = page.get_by_role("combobox", name="Presentation")
        chooser.select_option("bundled:studio")
        held.release(1)
    expect(settled).to_be_enabled()
    expect(page.get_by_text("Renamed to “Renamed base”.")).to_be_visible()
    expect(summary).to_contain_text("Layout: Studio")
    names = [
        config["name"]
        for item in _presentation(service)["store"]["namespaces"]
        if item["frontend_id"] == "stock"
        for config in item["configs"]
    ]
    assert names == ["Renamed base"]


def test_target_switches_never_show_the_previous_targets_rows(
    live, page, evidence, fake_router, crowded_ids
):
    instance, _ = crowded_ids
    other_home, other_instance = "z" * 90, "7" * 32
    service = live("switch", router_client=fake_router, homes={"crowded": evidence})
    page.goto(service.url({"home": "crowded"}, nonce=service.nonce()))
    expect(page.get_by_text("crowded-project")).to_be_visible()

    def listing(url):
        return "/views/instances?" in url and other_home in url

    with hold(page, listing) as held:
        page.get_by_role("link", name=other_home, exact=True).click()
        wait_held(page, held, 1)
        expect(page.get_by_text("Reading instances…")).to_be_visible()
        expect(page.get_by_text("crowded-project")).to_have_count(0)
        held.release(0)
    # This matcher is gone before Back re-reads the same home's listing.
    expect(page.get_by_text(f"{other_home}-project")).to_be_visible()

    page.get_by_role("link", name=instance, exact=True).click()
    expect(page.get_by_text(f"/p/{instance}", exact=True)).to_be_visible()
    page.go_back()
    expect(heading(page, "Home inventory")).to_be_visible()
    with hold(page, lambda url: f"/views/instances/{other_instance}" in url) as held:
        page.get_by_role("link", name=other_instance, exact=True).click()
        wait_held(page, held, 1)
        expect(page.get_by_text("Reading instance…")).to_be_visible()
        expect(page.get_by_text(f"/p/{instance}", exact=True)).to_have_count(0)
        held.release(0)
    expect(page.get_by_text(f"/p/{other_instance}", exact=True)).to_be_visible()


def test_late_replies_never_replace_the_current_selection(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("selection", router_client=fake_router, homes={"crowded": evidence})
    target = {"home": "crowded", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    status = page.get_by_role("region", name="Status")
    inspector = page.get_by_role("region", name="Inspector")
    first, second = f"{0:032x}", f"{4:032x}"

    with hold(page, lambda url: "invocation_id=" in url) as held:
        status.get_by_role("button", name=first, exact=True).click()
        wait_held(page, held, 1)
        status.get_by_role("button", name=second, exact=True).click()
        wait_held(page, held, 2)
        held.release(1, consumed=True)
        expect(inspector).to_contain_text(f"node_{second[-6:]}")
        held.release(0, consumed=True)  # A's reply is read after B's
        expect(inspector).to_contain_text(f"node_{second[-6:]}")
        expect(inspector).not_to_contain_text(f"node_{first[-6:]}")

    page.get_by_role("button", name="Read published outputs").click()
    with hold(page, lambda url: "representation=report" in url) as held:
        page.get_by_role("button", name="first-report", exact=True).click()
        wait_held(page, held, 1)
        page.get_by_role("button", name="second-report", exact=True).click()
        wait_held(page, held, 2)
        held.release(1, consumed=True)
        report = page.locator(".report-view")
        expect(report).to_contain_text("Report second-report")
        held.release(0, consumed=True)
        expect(report).to_contain_text("Report second-report")
        expect(report).not_to_contain_text("Report first-report")


def test_history_sections_never_render_another_sections_rows(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("sections", router_client=fake_router, homes={"crowded": evidence})
    target = {"home": "crowded", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    page.get_by_role("button", name="Invocations").click()
    invocations = page.get_by_role("table", name="Invocations, newest attempt first")
    expect(invocations).to_be_visible()
    with hold(page, lambda url: "section=transitions" in url) as held:
        page.get_by_role("button", name="Transitions and moves").click()
        wait_held(page, held, 1)
        expect(invocations).to_have_count(0)
        expect(page.get_by_text("Reading history…")).to_be_visible()
        held.release(0)
    expect(page.get_by_text("Nothing is recorded in this section yet.")).to_be_visible()


# Whether every laid-out graph node lies inside the graph canvas's own box.
ALL_NODES_IN_CANVAS = """() => {
  const canvas = document.querySelector('.graph-canvas');
  const box = canvas.getBoundingClientRect();
  const nodes = [...canvas.querySelectorAll('.react-flow__node')];
  return nodes.length > 0 && nodes.every((node) => {
    const r = node.getBoundingClientRect();
    return r.left >= box.left && r.top >= box.top
      && r.right <= box.right && r.bottom <= box.bottom;
  });
}"""


def test_graph_is_read_only_with_tables_and_records_the_layout_comparison(
    live, page, evidence, review_gate, lad_topology
):
    service = live("lad")
    files_by_name, workspaces = lad_topology
    instance, run = service.create_and_start(files_by_name, workspaces=workspaces)
    # The learning plane may wait on a seam; the captured graph needs only the run.
    service.settled(instance, run, timeout=60, finished=False)
    two, two_instance, two_run = real_run(live, review_gate, "two-plane")
    for owner, (target_instance, target_run), planes, label in (
        (service, (instance, run), ("foreground", "learning"), "lad"),
        (two, (two_instance, two_run), ("main", "side"), "two-plane"),
    ):
        target = {"home": "default", "instance": target_instance, "run": target_run}
        page.goto(owner.url(target, nonce=owner.nonce()))
        canvas = page.locator(".graph-canvas")
        for plane in planes:
            graph = page.get_by_role("region", name="Graph")
            # Both captures have two planes: the chooser must render, then choose.
            plane_choice = graph.get_by_role("combobox", name="Plane")
            expect(plane_choice).to_be_visible()
            plane_choice.select_option(plane)
            for kind in ("Nodes", "Routes"):
                table = page.get_by_role("table", name=f"{kind} of plane {plane}")
                expect(table).to_be_visible()
            # Whole identifiers widen only the tables' own scroller, not the page.
            assert no_page_overflow(page), f"{label} {plane}"
            for engine in ("Directed", "Grid"):
                graph.get_by_role("radio", name=engine, exact=True).check()
                # Capture only the canvas laid out for this plane and engine.
                laid_out = graph.get_by_role(
                    "group",
                    name=f"Captured graph of plane {plane}, {engine.lower()} layout, "
                    "read-only",
                    exact=True,
                )
                expect(laid_out).to_be_visible()
                expect(page.get_by_text("Directed layout failed")).to_have_count(0)
                # Standard zoom and Fit controls, without the editing toggle: after
                # zooming in, Fit View (by keyboard) shows every laid-out node.
                expect(
                    graph.get_by_role("button", name="Toggle Interactivity")
                ).to_have_count(0)
                zoom_in = graph.get_by_role("button", name="Zoom In", exact=True)
                zoom_in.click()
                zoom_in.click()
                page.wait_for_function(f"() => !({ALL_NODES_IN_CANVAS})()")
                graph.get_by_role("button", name="Fit View", exact=True).focus()
                page.keyboard.press("Enter")
                page.wait_for_function(ALL_NODES_IN_CANVAS)
                name = f"layout-{label}-{plane}-{engine}.png"
                canvas.screenshot(path=str(evidence / name))
        node = page.locator(".react-flow__node").first
        placed = node.evaluate("element => element.style.transform")
        box = node.bounding_box()
        node.hover()
        page.mouse.down()
        page.mouse.move(box["x"] + 160, box["y"] + 90)
        page.mouse.up()
        # The viewport may pan, but the node's flow position never changes.
        assert node.evaluate("element => element.style.transform") == placed
        assert page.locator(".react-flow__handle.connectable").count() == 0
    assert csp_violations(page) == []


def test_any_403_disables_writes_without_another_request(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("refused", router_client=fake_router, homes={"crowded": evidence})
    target = {"home": "crowded", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    expect(page.get_by_role("region", name="Status")).to_contain_text(run)
    refusal = {
        "contract": "os.error.v1",
        "code": "example_forbidden",
        "category": "trust",
        "summary": "example forbidden",
        "retryable": False,
    }
    page.route(
        "**/api/v1/router/views/instances/**",
        lambda handle: handle.fulfill(
            status=403, content_type="application/json", body=json.dumps(refusal)
        ),
    )
    page.get_by_role("button", name="Refresh").click()
    expect(page.get_by_text("Browser trust refused")).to_be_visible()
    expect(page.get_by_text("example_forbidden").first).to_be_visible()
    page.locator("details.layout-menu summary").click()
    page.get_by_label("Name").fill("After refusal")
    expect(page.get_by_role("button", name="Save as new")).to_be_disabled()
    expect(page.get_by_role("button", name="Make default")).to_be_disabled()
    writes = [item for item in fetches(page) if item["method"] == "PUT"]
    assert writes == []
