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
import re
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


def open_view(page, name):
    """Open one run view (Overview, Graph, Activity, Reports or Control)."""
    page.get_by_role("navigation", name="Run views").get_by_role(
        "button", name=name, exact=True
    ).click()


def no_page_overflow(page) -> bool:
    return page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 1"
    )


def last_column_text_is_unobscured(table) -> bool:
    """At the right edge, real text must clear the sticky identity column."""
    return table.evaluate("""table => {
      const scroller = table.closest('.table-scroll').getBoundingClientRect();
      return [...table.rows].every(row => {
        const identity = row.cells[0].getBoundingClientRect();
        const cell = row.cells[row.cells.length - 1];
        const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
          if (!node.textContent.trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          if (![...range.getClientRects()].every(rect =>
            rect.left >= identity.right - 1 && rect.right <= scroller.right + 1
          )) return false;
        }
        return true;
      });
    }""")


def real_run(live, review_gate, name="service"):
    service = live(name)
    instance, run = service.create_and_start(review_gate, workspaces=("main", "side"))
    service.settled(instance, run)
    return service, instance, run


def test_nonce_journey_from_inventory_to_graph_inspector_and_history(
    live, page, page_errors, evidence, review_gate, context_reads
):
    service, instance, run = real_run(live, review_gate)
    nonce = service.nonce()
    context_reads.claimed = True
    with page.expect_request("**/api/v1/demo/context") as context_read:
        with page.expect_response("**/api/v1/demo/context") as context_reply:
            page.goto(service.url(nonce=nonce))
    expect(heading(page, "State homes")).to_be_visible()
    first = fetches(page)[0]
    assert first["url"].endswith("/api/v1/auth/exchange") and first["hash"] == ""
    assert nonce not in page.url and "#" not in page.url

    page.get_by_role("link", name="default", exact=True).click()
    expect(heading(page, "Home inventory")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    inventory = page.get_by_role("table", name="Instances in default")
    expect(inventory.locator("tbody tr").first).to_be_visible()
    expect(inventory.get_by_role("columnheader")).to_have_count(6)
    assert no_page_overflow(page)
    inventory_scroll = inventory.locator("..")
    expect(inventory_scroll).to_have_class("table-scroll")
    shot(page, evidence, "inventory-populated-390")
    inventory.get_by_role(
        "columnheader", name="Read", exact=True
    ).scroll_into_view_if_needed()
    assert inventory_scroll.evaluate(
        "node => node.scrollWidth > node.clientWidth && "
        "node.scrollLeft + node.clientWidth >= node.scrollWidth - 1"
    )
    assert no_page_overflow(page)
    assert inventory.locator("tbody th").first.evaluate(
        "node => { const cell = node.getBoundingClientRect(); "
        "const scroller = node.closest('.table-scroll').getBoundingClientRect(); "
        "return cell.left >= scroller.left - 1 && cell.right <= scroller.right; }"
    )
    expect(inventory.locator("tbody th").first.locator(".item-name")).to_be_visible()
    assert last_column_text_is_unobscured(inventory)
    shot(page, evidence, "inventory-columns-end-390")
    page.get_by_role("link", name=instance).click()
    expect(heading(page, "Instance")).to_be_visible()
    runs = page.get_by_role("table", name=f"Runs of instance {instance}")
    expect(runs.locator("tbody tr").first).to_be_visible()
    expect(runs.get_by_role("columnheader")).to_have_count(6)
    assert no_page_overflow(page)
    runs_scroll = runs.locator("..")
    expect(runs_scroll).to_have_class("table-scroll")
    shot(page, evidence, "instance-runs-populated-390")
    runs.get_by_role(
        "columnheader", name="Pause reason", exact=True
    ).scroll_into_view_if_needed()
    assert runs_scroll.evaluate(
        "node => node.scrollWidth > node.clientWidth && "
        "node.scrollLeft + node.clientWidth >= node.scrollWidth - 1"
    )
    assert no_page_overflow(page)
    assert runs.locator("tbody th").first.evaluate(
        "node => { const cell = node.getBoundingClientRect(); "
        "const scroller = node.closest('.table-scroll').getBoundingClientRect(); "
        "return cell.left >= scroller.left - 1 && cell.right <= scroller.right; }"
    )
    expect(runs.locator("tbody th").first.locator(".item-name")).to_be_visible()
    assert last_column_text_is_unobscured(runs)
    shot(page, evidence, "instance-runs-columns-end-390")
    # The instance screen names the project from its observed project folder.
    project = page.locator("#runs-heading").inner_text()
    assert project not in ("", "Unknown project", instance)

    def instance_reads():
        return sum(
            f"/views/instances/{instance}?" in item["url"] for item in fetches(page)
        )

    observed_instance_reads = instance_reads()
    assert observed_instance_reads >= 1
    page.get_by_role("link", name=run).click()
    layout_summary = page.locator(".layout-menu > summary")
    expect(layout_summary).to_have_accessible_name("Layout: Precision")
    expect(layout_summary.locator(".layout-summary-narrow")).to_have_text("Layout")
    expect(layout_summary.locator(".layout-summary-wide")).not_to_be_visible()
    crumbs = page.get_by_role("navigation", name="Target")
    assert crumbs.locator("code, .breadcrumbs__name").evaluate_all(
        "nodes => nodes.every(node => { const box = node.getBoundingClientRect(); "
        "return node.scrollWidth <= node.clientWidth + 1 && box.left >= 0 "
        "&& box.right <= innerWidth; })"
    )
    # Inside the instance just observed, the run's breadcrumb shows that project
    # name. The exact instance ID stays in the link's name and title, and the
    # name comes from the earlier observation: no instance read is issued for it.
    named = crumbs.get_by_role(
        "link", name=f"Instance {project}, ID {instance}", exact=True
    )
    expect(named).to_have_text(project)
    expect(named.locator(".breadcrumbs__name")).to_have_attribute(
        "title", f"Instance ID {instance}"
    )
    assert named.get_attribute("href").endswith(f"instance={instance}")
    assert instance_reads() == observed_instance_reads
    shot(page, evidence, "run-breadcrumbs-390")
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(heading(page, "Run")).to_be_visible()
    assert f"instance={instance}" in page.url and f"run={run}" in page.url

    # The run opens on Overview: status first, with no graph read yet.
    status = page.get_by_role("region", name="Status")
    for text in (run, instance, "Run status", "Core quiescence", "Effective writers"):
        expect(status).to_contain_text(text)
    expect(page.locator(".flow-node")).to_have_count(0)
    assert not any("/revisions/" in item["url"] for item in fetches(page))
    open_view(page, "Activity")
    for region in ("Planes", "Current work and blockers"):
        expect(page.get_by_role("heading", name=region)).to_be_visible()

    # A normal two-plane run leaves at least one readable node-height of canvas
    # in the Graph view's opening desktop viewport.
    open_view(page, "Graph")
    expect(page.locator(".flow-node").first).to_be_visible()
    page.evaluate("window.scrollTo(0, 0)")
    assert page.locator(".graph-canvas").evaluate(
        "canvas => { const box = canvas.getBoundingClientRect(); "
        "const node = canvas.querySelector('.flow-node'); "
        "return node !== null && Math.min(box.bottom, innerHeight) - "
        "Math.max(box.top, 0) >= node.offsetHeight; }"
    )
    for region in ("Graph", "Inspector"):
        expect(page.get_by_role("heading", name=region)).to_be_visible()

    nodes = page.get_by_role("table", name="Nodes of plane main")
    expect(nodes).to_be_visible()
    nodes.get_by_role("button", name="work").click()
    inspector = page.get_by_role("region", name="Inspector")
    expect(inspector).to_contain_text("Captured routes")
    expect(inspector).to_contain_text("RETRY")
    expect(page.locator(".react-flow__node").first).to_be_visible()

    open_view(page, "Activity")
    page.get_by_role("button", name="Invocations").click()
    rows = page.get_by_role("table", name="Invocations, newest attempt first")
    expect(rows).to_be_visible()
    # Inspecting a History row opens the Graph view's Inspector.
    rows.get_by_role("button").first.click()
    expect(inspector).to_contain_text("Original (completion)")
    expect(page.locator(".graph-legend")).to_contain_text(
        "Inspector: invocation not marked"
    )
    expect(inspector).to_contain_text("Effective")
    shot(page, evidence, "journey-run-precision")

    page.go_back()
    expect(heading(page, "Instance")).to_be_visible()
    page.go_forward()
    expect(heading(page, "Run")).to_be_visible()
    expect(page.get_by_role("region", name="Status")).to_contain_text(run)
    expect(named).to_have_text(project)  # same instance: Back and Forward keep it
    assert csp_violations(page) == []
    # Correlate the stock reader's EOF and semantic guard with this exact read.
    context_reads.settle(context_read.value, context_reply.value)
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
        page.get_by_role("link", name=name).focus()
        page.keyboard.press("Enter")
        expect(heading(page, title)).to_be_visible()
        expect(heading(page, title)).to_be_focused()

    open_view(page, "Graph")
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
        open_view(page, "Overview")
        page.evaluate("window.scrollTo(0, 0)")
        views = page.get_by_role("navigation", name="Run views")
        expect(views.locator('[aria-current="page"]')).to_have_text("Overview")
        assert views.evaluate("""nav => {
          const tabs = nav.querySelector('.views__tabs');
          const buttons = [...tabs.querySelectorAll('button')];
          return buttons.length === 5
            && getComputedStyle(tabs).overflowX === 'auto'
            && new Set(buttons.map(button => button.offsetTop)).size === 1;
        }""")
        assert page.locator(".page-heading__title").evaluate("""row => {
          const title = row.querySelector('h1').getBoundingClientRect();
          const refresh = row.querySelector('button').getBoundingClientRect();
          return Math.abs(title.top - refresh.top) < refresh.height;
        }""")
        assert page.locator(".status-state").evaluate(
            "node => node.getBoundingClientRect().bottom <= innerHeight"
        )
        status = page.get_by_role("region", name="Status")
        expect(status).to_contain_text(run)
        if not status.locator("details.technical-details").get_attribute("open"):
            status.get_by_text("Exact target and run details", exact=True).click()
        box = status.get_by_text(run, exact=True).bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width
        assert no_page_overflow(page), label
        expect(page.get_by_role("navigation", name="Run views")).to_be_visible()
        shot(page, evidence, f"run-{label}")

    # Returning to the Graph view opened at desktop width, now at 390: Readable
    # size deliberately recenters the recorded cursor at 100% for the canvas as
    # it is now. This is the desktop-visit, resize and return sequence, not a
    # fresh narrow load.
    open_view(page, "Graph")
    expect(page.locator("#view-graph")).to_be_focused()
    canvas = page.locator(".graph-canvas")
    viewport = canvas.locator(".react-flow__viewport")
    expect(canvas.locator(".flow-node.is-cursor")).to_have_count(1)
    readable = page.locator(".graph-toolbar").get_by_role(
        "button", name="Readable size (100%)", exact=True
    )
    cursor_centered_at_reading_scale = """() => {
      const canvas = document.querySelector('.graph-canvas');
      const current = canvas?.querySelector('.flow-node.is-cursor');
      const view = canvas?.querySelector('.react-flow__viewport');
      if (!current || !view || !/scale\\(1\\)/.test(view.style.transform)) return false;
      const box = canvas.getBoundingClientRect();
      const node = current.getBoundingClientRect();
      const inside = node.left >= box.left && node.top >= box.top
        && node.right <= box.right && node.bottom <= box.bottom;
      const dx = (node.left + node.right) / 2 - (box.left + box.right) / 2;
      const dy = (node.top + node.bottom) / 2 - (box.top + box.bottom) / 2;
      return inside && Math.abs(dx) <= 2 && Math.abs(dy) <= 2;
    }"""
    readable.click()
    page.wait_for_function(cursor_centered_at_reading_scale)
    inspector = page.get_by_role("region", name="Inspector")
    expect(inspector).to_contain_text("check")  # recentering selects nothing new
    shot(page, evidence, "graph-narrow-recentered")
    # An ordinary read keeps a manual zoom and pan; only the button recenters.
    zoom_in = canvas.get_by_role("button", name="Zoom In", exact=True)
    zoom_in.click()
    zoom_in.click()
    # Both zoom steps have been applied before the manual view is recorded.
    expect(viewport).not_to_have_attribute(
        "style", re.compile(r"scale\(1(\.2)?\)")
    )
    manual_view = viewport.get_attribute("style")
    open_view(page, "Overview")
    observed_time = page.locator(".status-lead time")
    before_refresh = observed_time.get_attribute("datetime")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(observed_time).not_to_have_attribute("datetime", before_refresh)
    open_view(page, "Graph")
    expect(viewport).to_have_attribute("style", manual_view)
    # Fit all stays the separate complete-topology overview.
    page.locator(".graph-toolbar").get_by_role(
        "button", name="Fit all", exact=True
    ).click()
    page.wait_for_function(ALL_NODES_IN_CANVAS)
    readable.focus()
    page.keyboard.press("Enter")
    page.wait_for_function(cursor_centered_at_reading_scale)
    assert no_page_overflow(page)
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
    open_view(page, "Graph")
    expect(page.get_by_role("table", name="Nodes of plane main")).to_be_visible()
    open_view(page, "Activity")
    page.get_by_role("button", name="Invocations").click()
    open_view(page, "Reports")
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

    for preset, hidden, view in (
        ("command", "Show Graph", "Graph"),
        ("studio", "Show Planes", "Activity"),
    ):
        page.get_by_role("combobox", name="Presentation").select_option(
            f"bundled:{preset}"
        )
        open_view(page, view)
        expect(page.locator(".view:not([hidden]) .view-hidden")).to_contain_text(
            hidden
        )
        open_view(page, "Overview")
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
    open_view(page, "Graph")
    page.locator(".view:not([hidden]) .view-hidden").get_by_role(
        "button", name="Show Graph"
    ).click()
    expect(page.get_by_text("core_inspection_too_large")).to_be_visible()

    # Node selection reveals a hidden Inspector without changing the target.
    url = page.url
    open_view(page, "Activity")
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
    live, page, evidence, fake_router, crowded_ids, monkeypatch
):
    instance, run = crowded_ids
    fake = fake_router
    original_summary = fake.view_run
    recorded_status = {"value": "running"}
    conditions = {"cancel": True, "hold": True}

    def run_conditions(home, instance_id, run_id, **selectors):
        result = original_summary(home, instance_id, run_id, **selectors)
        if selectors.get("section", "summary") == "summary":
            result["data"].update(
                pause_reason="operator_review" if conditions["hold"] else None,
                cancel_requested=conditions["cancel"],
                status=recorded_status["value"],
            )
            if not conditions["hold"]:
                result["data"]["run_gate"] = None
        return result

    monkeypatch.setattr(fake, "view_run", run_conditions)
    service = live("crowded", router_client=fake, homes={"crowded": evidence})
    page.goto(service.url({"home": "crowded"}, nonce=service.nonce()))
    expect(page.get_by_text("refused store_integrity_error")).to_be_visible()
    refused_instance = page.get_by_role("row").filter(
        has_text="refused store_integrity_error"
    )
    expect(refused_instance.locator(".item-name")).to_have_text("Unknown project")
    expect(refused_instance.locator(".item-subtitle")).to_have_text(
        "Unknown workflow file"
    )
    workflow_cell = refused_instance.get_by_role("cell").nth(1)
    expect(workflow_cell.locator("summary")).to_contain_text("Unknown workflow file")
    expect(workflow_cell.get_by_label("Instance ID: show full value")).to_have_count(0)
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
    # The lead resolves run-level facts before paused planes. A running run with
    # a requested cancellation is pending, not stopped, and is never described
    # as awaiting a resume. Paused planes are a count; exact IDs stay in rows.
    lead = status.locator(".status-next")
    paused_rows = status.locator(".attention > .attention__item").filter(
        has_text="Recorded pause reason"
    )
    paused = paused_rows.count()
    assert paused > 1
    paused_fact = f"{paused} planes still record a pause reason; see their rows"
    expect(lead).to_contain_text(
        "Cancellation is requested but not recorded complete: "
        "the run is still recorded running."
    )
    expect(lead).to_contain_text("Work may still be running.")
    expect(lead).to_contain_text("Writer closure is not confirmed")
    expect(lead).to_contain_text(paused_fact)
    expect(lead).not_to_contain_text("resume decision")
    expect(lead).not_to_contain_text("plane-x")
    for row in paused_rows.all():
        expect(row).to_contain_text("plane-" + "x" * 20)  # whole IDs stay in rows
    # Reports is not the lead action while the lead points at facts and rows.
    expect(status.locator(".status-actions")).to_have_count(0)
    expect(
        status.locator(".observation").get_by_role(
            "button", name="Run reports", exact=True
        )
    ).to_have_count(1)
    open_view(page, "Activity")
    # Current work groups every recorded fact under the run and under each
    # plane that records one. A plane's long ID is stated once, by its group,
    # with the full value one disclosure away, not repeated on every line.
    work = page.get_by_role("region", name="Current work and blockers")
    run_group = work.get_by_role("group", name="Run", exact=True)
    for run_fact in (
        "Recorded run pause reason: operator_review.",
        "Run gate: learning_ack.",
        "Run cancellation requested.",
        "Pending candidate (diagnostic invalid_graph).",
        "Edit mode: locked.",
    ):
        expect(
            run_group.get_by_role("listitem").filter(has_text=run_fact)
        ).to_have_count(1)
    expect(run_group).not_to_contain_text("plane-")
    plane_groups = work.locator('[role="group"]:not([aria-label="Run"])')
    long_plane = "plane-" + "x" * 54
    working, held_plane, routing = (f"{long_plane}{index:02d}" for index in (0, 1, 5))
    working_group = work.get_by_role("group", name=f"Plane {working}", exact=True)
    working_facts = working_group.get_by_role("listitem")
    expect(working_facts).to_have_text([
        f"Invocation {0:032x} at node node_{'y' * 50}_0.",
        f"Cursor node_{'y' * 50}_0 requires terminal other succeeded.",
    ])
    held_group = work.get_by_role("group", name=f"Plane {held_plane}", exact=True)
    expect(held_group.get_by_role("listitem")).to_have_text(
        ["Paused: operator_decision_required; resume target node_1."]
    )
    routing_group = work.get_by_role("group", name=f"Plane {routing}", exact=True)
    expect(routing_group.get_by_role("listitem")).to_have_text([
        "Paused: operator_decision_required; resume target node_5.",
        "1 completion(s) not yet routed.",
    ])
    # One group per plane with a recorded fact: every paused plane has its own.
    expect(
        plane_groups.filter(has_text="Paused: operator_decision_required")
    ).to_have_count(paused)
    assert plane_groups.count() > paused  # working and dependent planes too
    for group, plane_id in ((working_group, working), (held_group, held_plane)):
        # The exact ID is whole and selectable in the group's one disclosure.
        exact = group.locator(".exact-value__full")
        expect(exact).to_have_count(1)
        expect(exact).to_have_text(plane_id)
        disclosure = group.locator("details.exact-value")
        expect(disclosure).not_to_have_attribute("open", "")
        expect(group.get_by_role("list")).not_to_contain_text("plane-")
        group.get_by_label(f"Plane {plane_id}: show full value").click()
        expect(disclosure).to_have_attribute("open", "")
        expect(exact).to_be_visible()
    expect(work.get_by_role("group", name="Plane sparse", exact=True)).to_have_count(0)
    shot(page, evidence, "activity-current-work-grouped")
    planes = page.get_by_role("region", name="Planes")
    expect(planes).to_contain_text("operator_decision_required")
    sparse = planes.get_by_role("row", name="sparse")
    expect(sparse).to_contain_text("unknown")
    expect(planes.locator(".table-scroll-hint")).to_contain_text("Scroll horizontally")
    open_view(page, "Graph")
    expect(page.get_by_text("no graph is shown for it")).to_be_visible()
    open_view(page, "Overview")
    for width in (1440, 640, 390):
        page.set_viewport_size({"width": width, "height": 900})
        assert no_page_overflow(page), width
        shot(page, evidence, f"crowded-{width}")

    other_planes = status.locator("details.other-planes")
    expect(other_planes).not_to_have_attribute("open", "")
    expect(other_planes.locator("summary")).to_contain_text("Other plane activity (")
    # Every other recorded plane remains available, without a top-N cap.
    other_planes.locator("summary").click()
    expect(status.locator(".attention__item")).to_have_count(13)
    expect(other_planes).not_to_contain_text("sparse")
    expect(other_planes).not_to_contain_text("Active invocation:")
    other_planes.locator("summary").click()
    expect(status.locator(".attention > .attention__item").filter(
        has_text="sparse"
    )).to_be_visible()
    # A requested cancellation leads, so no resume is recommended: each row's
    # Review resume is an ordinary secondary button, drawn like Inspect node,
    # and exactly as available as before.
    inspect_node = status.get_by_role("button", name="Inspect node").first

    def drawn(button):
        return button.evaluate(
            "node => { const style = getComputedStyle(node); return "
            "[style.borderTopColor, style.color, style.fontWeight]; }"
        )

    def resumes_are(emphasis):
        buttons = status.get_by_role("button", name="Review resume")
        expect(buttons).to_have_count(paused)
        for button in buttons.all():
            expect(button).to_be_enabled()
            expect(button).to_have_class(emphasis)
        return buttons.first

    secondary = "button--review button--plain"
    assert drawn(resumes_are(secondary)) == drawn(inspect_node)
    active_row = status.locator(".attention > .attention__item").filter(
        has_text="Active invocation: " + f"{0:032x}"
    )
    expect(active_row).to_be_visible()

    # Status keeps each plane's blocker code and targets when Studio hides
    # Planes and Current work.
    page.set_viewport_size({"width": 1440, "height": 900})
    page.locator("details.layout-menu summary").click()
    page.get_by_role("combobox", name="Presentation").select_option("bundled:studio")
    open_view(page, "Activity")
    expect(page.get_by_role("heading", name="Planes")).to_have_count(0)
    expect(page.locator(".view:not([hidden]) .view-hidden")).to_contain_text(
        "Show Planes"
    )
    open_view(page, "Overview")
    expect(active_row).to_be_visible()
    active_row.get_by_role("button", name=f"{0:032x}", exact=True).click()
    expect(page.locator("#destination-inspector")).to_be_focused()
    expect(page.get_by_role("region", name="Inspector")).to_contain_text(f"{0:032x}")
    graph_box = page.get_by_role("region", name="Graph", exact=True).bounding_box()
    inspector_box = page.get_by_role("region", name="Inspector").bounding_box()
    assert graph_box is not None and inspector_box is not None
    assert graph_box["height"] < inspector_box["height"]
    graph_error = page.get_by_role("region", name="Graph", exact=True)
    expect(graph_error).to_contain_text("core_inspection_too_large")
    expect(graph_error.get_by_role("button", name="Read again")).to_be_visible()
    handle_box = page.get_by_role("separator", name="Resize panels").bounding_box()
    assert handle_box is not None and handle_box["height"] <= 64
    shot(page, evidence, "graph-error-content-sized")
    open_view(page, "Overview")
    for visible_run_fact in (
        "Run gate: learning_ack.",
        "Pending candidate (diagnostic invalid_graph).",
        "Recorded run pause reason: operator_review.", "Run cancellation requested.",
    ):
        expect(status.locator(".run-level-facts")).to_contain_text(visible_run_fact)
    expect(status.locator(".run-level-facts")).not_to_contain_text("Edit mode")
    expect(status.locator("details.technical-details")).to_contain_text("Edit mode")
    blocked = "paused operator_decision_required at node_" + "y" * 50 + "_1"
    expect(status).to_contain_text(blocked)
    expect(status).to_contain_text("resume node_1")
    expect(status).to_contain_text("Inspection target: node_" + "y" * 50 + "_1")
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
    expect(status.locator(".status-state")).to_contain_text("Stale observation")
    # The failed read is its own readable statement above the lead, in fact ink,
    # not part of the muted clock line. The last observed lead stays, labelled.
    stale = status.locator(".observation-failure")
    expect(stale).to_have_count(1)
    expect(stale).to_contain_text(
        "This is the last successful observation, not the current state"
    )
    expect(status.locator(".observation .observation-failure")).to_have_count(0)
    expect(status.locator(".observation")).not_to_contain_text("Stale:")
    stale_ink = stale.evaluate("node => getComputedStyle(node).color")
    assert stale_ink == status.evaluate("node => getComputedStyle(node).color")
    assert stale_ink != status.locator(".observation").evaluate(
        "node => getComputedStyle(node).color"
    )
    assert stale.evaluate(
        "node => parseFloat(getComputedStyle(node).fontSize) > parseFloat("
        "getComputedStyle(node.parentElement.querySelector('.observation'))"
        ".fontSize)"
    )
    assert stale.evaluate(
        "node => !!(node.compareDocumentPosition("
        "node.parentElement.querySelector('.status-next')) & "
        "Node.DOCUMENT_POSITION_FOLLOWING)"
    )
    expect(lead).to_contain_text("Cancellation is requested but not recorded complete")
    expect(lead).to_contain_text(paused_fact)
    expect(
        status.get_by_role("heading", name="Plane activity", exact=True)
    ).to_be_visible()
    expect(status.locator(".attention__item")).to_have_count(13)
    expect(status.locator(".attention__item").first).to_contain_text(
        "Requires terminal: other succeeded"
    )
    expect(
        status.get_by_role("button", name="Run reports", exact=True)
    ).to_have_count(1)
    expect(status.get_by_role("button", name="Reports for")).to_have_count(0)
    expect(status.locator(".safety-facts--attention")).to_contain_text("may_live 1")
    assert status.locator(".safety-facts--attention").evaluate(
        "node => !!(node.compareDocumentPosition("
        "node.parentElement.querySelector('.attention')) & "
        "Node.DOCUMENT_POSITION_FOLLOWING)"
    )
    expect(status).to_contain_text("may_live 1")  # the last observation stays visible
    shot(page, evidence, "crowded-stale")
    expect(status.locator(".observation-qualifier")).to_have_text("Stale observation")
    expect(status.locator(".observation-qualifier.state")).to_have_count(0)
    fake.fail_summary = False
    badge = status.locator(".state--large")
    small_badge = status.locator(".attention__identity .state").first
    small_font = small_badge.evaluate("node => getComputedStyle(node).fontFamily")
    fact_ink = status.evaluate("node => getComputedStyle(node).color")
    uncertain_bar = None
    for state in ("unknown", "cancelled", "succeeded", "failed", "paused", "running"):
        recorded_status["value"] = state
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(badge).to_have_text(state)
        expect(badge).to_have_css("color", fact_ink)
        if state == "unknown":
            uncertain_bar = badge.evaluate(
                "node => getComputedStyle(node).borderLeftColor"
            )
            assert uncertain_bar != fact_ink
        if state == "paused":
            expect(badge).to_have_css("border-left-color", uncertain_bar)
        expect(badge).to_have_css("font-family", small_font)
        expect(badge).to_have_css("border-radius", "0px")
        expect(badge).to_have_css("border-top-width", "0px")
        expect(badge).to_have_css("border-left-width", "3px")
        expect(badge).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        if state in ("unknown", "cancelled", "succeeded"):
            classes = set(badge.get_attribute("class").split())
            assert classes == {"state", "state--large"} | (
                {"state--uncertain"} if state == "unknown" else set()
            )
            if state == "unknown":
                expect(badge).to_have_css("border-left-style", "dashed")
            expect(badge).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        # Run-level state leads; retained plane pauses never become a resume
        # recommendation for an unknown, terminal or cancelling run.
        expect(lead).not_to_contain_text("resume decision")
        expect(lead).not_to_contain_text("plane-x")
        expect(lead).to_contain_text(paused_fact)
        if state == "unknown":
            expect(lead).to_contain_text(
                "The overall run state is not established by this observation, "
                "so no next step is inferred."
            )
        elif state in ("cancelled", "succeeded", "failed"):
            # Recorded may_live remains, so no finished or closure claim is made.
            expect(lead).to_contain_text(
                f"Recorded {state}. Writer closure is not confirmed; "
                "check the writer facts below."
            )
            expect(lead).not_to_contain_text("Finished")
            expect(lead).not_to_contain_text("Cancellation is requested")
        else:
            expect(lead).to_contain_text(
                "Cancellation is requested but not recorded complete: "
                f"the run is still recorded {state}."
            )
        # Guidance is wording only: every recorded fact and action stays.
        expect(status.locator(".run-level-facts")).to_contain_text(
            "Run cancellation requested."
        )
        expect(status.locator(".safety-facts--attention")).to_contain_text("may_live 1")
        # An unknown, terminal or cancelling run recommends no resume: the
        # buttons stay enabled and reachable, without review emphasis.
        assert drawn(resumes_are(secondary)) == drawn(inspect_node)
        expect(status.locator(".status-actions")).to_have_count(0)
        if state in ("unknown", "failed"):
            shot(page, evidence, f"run-state-{state}")

    # Without a cancellation, the run-level hold leads and qualifies the paused
    # planes: a count that points at their rows, with every row action intact.
    recorded_status["value"] = "paused"
    conditions["cancel"] = False
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(lead).to_contain_text("Held at run level; the recorded reason is below.")
    expect(lead).to_contain_text(
        f"{paused} planes are also paused: inspect each recorded node where one "
        "is shown, then review a resume in the rows under Plane activity."
    )
    # This fresh lead recommends reviewing a resume, so the same buttons carry
    # review emphasis: accent ink and border, heavier than Inspect node.
    emphasized = resumes_are("button--review")
    assert drawn(emphasized) != drawn(inspect_node)
    assert drawn(emphasized)[2] == "650"
    expect(lead).not_to_contain_text("Cancellation")
    expect(lead).not_to_contain_text("plane-x")
    expect(status.locator(".run-level-facts")).to_contain_text(
        "Recorded run pause reason: operator_review."
    )
    expect(status.locator(".run-level-facts")).not_to_contain_text(
        "Run cancellation requested."
    )
    # An ordinary pause (no run-level hold) is the one lead that asks for a resume
    # decision, still as a count over the same exact rows.
    conditions["hold"] = False
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(lead).to_have_text(
        f"{paused} planes are paused and waiting on a resume decision. Inspect "
        "each recorded node where one is shown, then review a resume in the rows "
        "under Plane activity."
    )
    expect(paused_rows).to_have_count(paused)
    assert drawn(resumes_are("button--review")) != drawn(inspect_node)
    expect(status.get_by_role("button", name="Inspect node").first).to_be_enabled()
    expect(status.locator(".status-actions")).to_have_count(0)
    expect(
        status.get_by_role("button", name="Run reports", exact=True)
    ).to_have_count(1)
    shot(page, evidence, "run-lead-paused-count")
    # A stale observation keeps its last lead, labelled, but recommends nothing
    # now: the emphasis goes and every Review resume stays enabled.
    fake.fail_summary = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(status.locator(".observation-failure")).to_have_count(1)
    expect(lead).to_contain_text("waiting on a resume decision")
    assert drawn(resumes_are(secondary)) == drawn(inspect_node)
    fake.fail_summary = False
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(status.locator(".observation-failure")).to_have_count(0)
    resumes_are("button--review")
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
    target = {"home": "default", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))

    def listing(url):
        return "/router/outputs/instances/" in url and "representation=" not in url

    def listing_reads():
        return sum(listing(item["url"]) for item in fetches(page))

    def report_reads():
        return sum("representation=report" in item["url"] for item in fetches(page))

    def unreachable(route):
        route.abort()

    panel = page.locator(".report-panel")
    refresh_outputs = page.get_by_role("button", name="Refresh outputs", exact=True)
    answer = page.get_by_role("button", name="answer-check")

    # The first listing read fails. The failure stays, with an explicit retry
    # beside it; nothing re-reads the listing on its own.
    page.route(listing, unreachable)
    open_view(page, "Reports")
    expect(panel).to_contain_text("Published outputs not read")
    expect(panel).to_contain_text("network_unavailable")
    page.unroute(listing, unreachable)
    page.get_by_role("button", name="Refresh", exact=True).click()  # run summary
    expect(panel).to_contain_text("Published outputs not read")
    assert listing_reads() == 1
    # The explicit retry succeeds and clears the failure: nothing is published.
    refresh_outputs.click()
    expect(panel).to_contain_text("No outputs are published for this run.")
    expect(panel).not_to_contain_text("Published outputs not read")
    assert listing_reads() == 2

    # An output published after that empty listing is found by Refresh outputs,
    # in the same mounted view, and by nothing before it.
    published = service.http.post(
        "/api/v1/router/outputs", json={"home": "default", "record": record}
    )
    assert published.status_code == 200, published.text
    open_view(page, "Overview")
    open_view(page, "Reports")
    expect(panel).to_contain_text("No outputs are published for this run.")
    expect(answer).to_have_count(0)
    assert listing_reads() == 2
    refresh_outputs.click()
    expect(answer).to_be_visible()
    expect(panel).not_to_contain_text("No outputs are published for this run.")
    assert listing_reads() == 3
    answer.click()
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
    view.get_by_text("Exact output provenance", exact=True).click()
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

    # Refreshing the listing keeps the selected report: its escaped body, the
    # open exact provenance and its digest stay through the pending read and
    # after it, and the report itself is not read again.
    digest = record["representations"]["report"]["sha256"]
    provenance = view.locator("details.report-evidence")
    selected_row = panel.get_by_role("row").filter(has=answer)

    def selected_report_is_kept():
        expect(rendered).to_contain_text("Answer <script>alert(1)</script>")
        assert rendered.locator("script, b").count() == 0
        expect(provenance).to_have_attribute("open", "")
        expect(provenance).to_contain_text(digest)
        expect(provenance).to_contain_text(event["completion_id"])
        expect(selected_row).to_have_attribute("aria-current", "true")
        assert report_reads() == 1

    selected_report_is_kept()
    with hold(page, listing) as held:
        refresh_outputs.click()
        wait_held(page, held, 1)
        expect(panel).to_contain_text("Reading published outputs…")
        selected_report_is_kept()
        refresh_outputs.click()  # a listing read in flight is not repeated
        selected_report_is_kept()
        assert len(held) == 1
        held.release(0)
        expect(panel).not_to_contain_text("Reading published outputs…")
    selected_report_is_kept()
    assert listing_reads() == 4
    # A failed re-read keeps the last successful listing, labelled, with the
    # selected report; the next successful read clears the failure.
    page.route(listing, unreachable)
    refresh_outputs.click()
    expect(panel).to_contain_text("Published outputs not read")
    expect(panel).to_contain_text("The listing below is the last successful read")
    expect(answer).to_be_visible()
    selected_report_is_kept()
    page.unroute(listing, unreachable)
    refresh_outputs.click()
    expect(panel).not_to_contain_text("Published outputs not read")
    expect(answer).to_be_visible()
    selected_report_is_kept()
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Download report (API response)").click()
    kept = json.loads(open(downloaded.value.path(), encoding="utf-8").read())
    assert kept["representation"]["sha256"] == digest
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
    instance, run = crowded_ids
    other_home, other_instance = "z" * 90, "7" * 32
    service = live("switch", router_client=fake_router, homes={"crowded": evidence})
    page.goto(service.url({"home": "crowded"}, nonce=service.nonce()))
    expect(page.get_by_role(
        "link", name=f"Open crowded-project instance {instance}", exact=True
    ).get_by_text("crowded-project", exact=True)).to_be_visible()

    def listing(url):
        return "/views/instances?" in url and other_home in url

    with hold(page, listing) as held:
        page.get_by_role("link", name=other_home, exact=True).click()
        wait_held(page, held, 1)
        expect(page.get_by_text("Reading instances…")).to_be_visible()
        expect(page.get_by_text("crowded-project", exact=True)).to_have_count(0)
        held.release(0)
    # This matcher is gone before Back re-reads the same home's listing.
    expect(page.get_by_role(
        "link", name=f"Open {other_home}-project instance {instance}", exact=True
    ).get_by_text(f"{other_home}-project", exact=True)).to_be_visible()

    page.get_by_role("link", name=instance).click()
    page.get_by_text("Instance settings and exact paths", exact=True).click()
    expect(page.get_by_text(f"/p/{instance}", exact=True)).to_be_visible()
    page.go_back()
    expect(heading(page, "Home inventory")).to_be_visible()
    with hold(page, lambda url: f"/views/instances/{other_instance}" in url) as held:
        page.get_by_role("link", name=other_instance).click()
        wait_held(page, held, 1)
        expect(page.get_by_text("Reading instance…")).to_be_visible()
        expect(page.get_by_text(f"/p/{instance}", exact=True)).to_have_count(0)
        held.release(0)
    page.get_by_text("Instance settings and exact paths", exact=True).click()
    expect(page.get_by_text(f"/p/{other_instance}", exact=True)).to_be_visible()

    # A run of the instance just observed names its project folder in the
    # breadcrumb (this fixture's folder is /p/<instance ID>), with the exact
    # instance ID in the link's name and title.
    crumbs = page.get_by_role("navigation", name="Target")
    name = crumbs.locator(".breadcrumbs__name")

    def other_instance_reads():
        return sum(
            f"/views/instances/{other_instance}?" in item["url"]
            for item in fetches(page)
        )

    observed_reads = other_instance_reads()
    page.get_by_role("link", name=f"Open run {run}", exact=True).click()
    expect(heading(page, "Run")).to_be_visible()
    expect(name).to_have_text(other_instance)
    expect(name).to_have_attribute("title", f"Instance ID {other_instance}")
    expect(crumbs.get_by_role(
        "link", name=f"Instance {other_instance}, ID {other_instance}", exact=True
    )).to_be_visible()

    # Leaving that instance clears the name. Back on its run without a fresh
    # observation, the breadcrumb is the exact ID's short form, and the first
    # instance's late reply is never adopted as this target's name.
    crumbs.get_by_role("link", name=other_home, exact=True).click()
    expect(heading(page, "Home inventory")).to_be_visible()
    fallback = crumbs.get_by_role(
        "link", name=f"Instance {other_instance}", exact=True
    )
    with hold(page, lambda url: f"/views/instances/{instance}?" in url) as held:
        page.get_by_role("link", name=instance).click()
        wait_held(page, held, 1)
        expect(page.get_by_text("Reading instance…")).to_be_visible()
        page.go_back()
        expect(heading(page, "Home inventory")).to_be_visible()
        page.go_back()
        expect(heading(page, "Run")).to_be_visible()
        expect(name).to_have_count(0)
        expect(fallback.locator("code")).to_have_text(
            f"{other_instance[:8]}…{other_instance[-6:]}"
        )
        expect(fallback.locator("code")).to_have_attribute("title", other_instance)
        held.release(0)
        expect(name).to_have_count(0)
        expect(fallback).to_be_visible()
    # Neither the name nor its absence caused another read of that instance.
    assert other_instance_reads() == observed_reads


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

    status.get_by_text("Exact in-flight invocations", exact=True).click()
    with hold(page, lambda url: "invocation_id=" in url) as held:
        status.locator(".inflight-details").get_by_role(
            "button", name=first, exact=True
        ).click()
        wait_held(page, held, 1)
        # Inspecting opened the Graph view's Inspector; choose again from Overview.
        open_view(page, "Overview")
        status.locator(".inflight-details").get_by_role(
            "button", name=second, exact=True
        ).click()
        wait_held(page, held, 2)
        held.release(1, consumed=True)
        expect(inspector).to_contain_text(f"node_{second[-6:]}")
        held.release(0, consumed=True)  # A's reply is read after B's
        expect(inspector).to_contain_text(f"node_{second[-6:]}")
        expect(inspector).not_to_contain_text(f"node_{first[-6:]}")
        identity = inspector.locator(".inspector-identity")
        expect(identity).to_contain_text("Selected recorded invocation")
        expect(identity).to_contain_text(second)
        expect(identity).to_contain_text(f"node_{second[-6:]}")
        expect(identity).not_to_contain_text(first)

    open_view(page, "Reports")
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


def test_selected_invocation_refresh_keeps_its_own_time_and_history_provenance(
    live, page, evidence, fake_router, crowded_ids, monkeypatch
):
    from millrace_os.router_client import RouterUnavailable

    instance, run = crowded_ids
    original = fake_router.view_run
    fail_exact = {"value": False}
    epoch_values = {
        "created_at": 1704067200, "deadline": 1704070800,
        "run_deadline": 1704074400,
    }

    def invocation_projection(home, instance_id, run_id, **selectors):
        if selectors.get("invocation_id") and fail_exact["value"]:
            raise RouterUnavailable("router_read_busy", 429)
        result = original(home, instance_id, run_id, **selectors)
        if selectors.get("section") == "invocations":
            for row in result["items"]:
                row.update(epoch_values)
        if selectors.get("invocation_id"):
            # An explicitly changed fact in the exact read, not a writer inference
            # from the independently unchanged History or summary projections.
            result["items"][0]["node_id"] = (
                "refreshed_exact_node"
                if instance_id == instance else "other_target_node"
            )
        return result

    monkeypatch.setattr(fake_router, "view_run", invocation_projection)
    service = live(
        "invocation-freshness", router_client=fake_router, homes={"crowded": evidence}
    )
    target = {"home": "crowded", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    open_view(page, "Activity")
    page.get_by_role("button", name="Invocations", exact=True).click()
    history = page.get_by_role("table", name="Invocations, newest attempt first")
    history.get_by_role("button").first.click()
    inspector = page.get_by_role("region", name="Inspector")
    snapshot = inspector.locator(".invocation-observation")
    expect(snapshot).to_contain_text("History snapshot")
    expect(inspector).to_contain_text("node_aaaaaa")
    history_time = snapshot.locator("time").get_attribute("datetime")
    assert history_time != "2024-01-01T00:00:00.000Z"  # Created is not read time.
    for stamp in ("00:00:00", "01:00:00", "02:00:00"):
        expect(inspector.locator(
            f'time[datetime="2024-01-01T{stamp}.000Z"]'
        )).to_be_visible()
    inspector.locator(".epoch-raw > summary").first.click()
    expect(inspector.locator(".epoch-raw").first).to_contain_text("1704067200")
    shot(page, evidence, "invocation-numeric-epoch-UTC-and-raw")
    exact_reads = fake_router.calls.count("run:invocations")
    with hold(page, lambda url: "invocation_id=" in url) as held:
        page.get_by_role("button", name="Refresh", exact=True).click()
        wait_held(page, held, 1)
        expect(snapshot).to_contain_text("History snapshot")
        expect(snapshot.locator("time")).to_have_attribute("datetime", history_time)
        expect(inspector).to_contain_text("node_aaaaaa")
        expect(inspector).not_to_contain_text("refreshed_exact_node")
        with page.expect_response(
            lambda response: response.request == held[0].request
        ) as exact_reply:
            held.release(0, consumed=True)
    expect(snapshot).to_contain_text("Invocation read")
    expect(snapshot.locator("time")).to_have_attribute(
        "datetime", exact_reply.value.json()["observed_at"]
    )
    expect(inspector).to_contain_text("refreshed_exact_node")
    expect(inspector).not_to_contain_text("node_aaaaaa")
    assert fake_router.calls.count("run:invocations") > exact_reads
    with hold(page, lambda url: "invocation_id=" in url) as held:
        page.get_by_role("button", name="Refresh", exact=True).click()
        wait_held(page, held, 1)
        page.get_by_role("button", name="Refresh", exact=True).click()
        wait_held(page, held, 2)
        # The shared EOF helper intentionally requires one unique URL. These
        # repeated exact GETs instead bind responses to their held Request and
        # wait for every matching application body reader after the late reply.
        with page.expect_response(
            lambda response: response.request == held[1].request
        ) as latest_reply:
            held.release(1)
        latest_time = latest_reply.value.json()["observed_at"]
        expect(snapshot.locator("time")).to_have_attribute("datetime", latest_time)
        with page.expect_response(
            lambda response: response.request == held[0].request
        ) as late_reply:
            held.release(0)
        assert late_reply.value.json()["observed_at"] != latest_time
        probe = {"url": held[0].request.url, "method": "GET"}
        page.wait_for_function(
            f"(probe) => {{ const reads = ({PROBED})(probe); "
            "return reads.length === 3 && reads.every(item => item.consumed); }",
            arg=probe,
        )
        page.evaluate(NEXT_FRAMES)
        expect(snapshot.locator("time")).to_have_attribute("datetime", latest_time)
    read_time = snapshot.locator("time").get_attribute("datetime")
    expect(inspector).to_contain_text("may_live")
    expect(inspector.locator(".state--uncertain")).to_contain_text("may_live")
    fail_exact["value"] = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(inspector).to_contain_text("router_read_busy")
    expect(inspector).to_contain_text("earlier snapshot below is retained")
    expect(snapshot.locator("time")).to_have_attribute("datetime", read_time)
    expect(inspector).to_contain_text("refreshed_exact_node")
    expect(inspector).to_contain_text("may_live")
    shot(page, evidence, "invocation-refresh-retained-snapshot")

    fail_exact["value"] = False
    with hold(page, lambda url: "invocation_id=" in url and
              f"/instances/{instance}/" in url) as held:
        page.get_by_role("button", name="Refresh", exact=True).click()
        wait_held(page, held, 1)
        # Public links switch to another target while retaining the same
        # invocation ID in both fixtures; only the target guard can separate it.
        page.get_by_role("navigation", name="Target").get_by_role(
            "link", name="crowded", exact=True
        ).click()
        page.get_by_role(
            "link", name=f"Open Unknown project instance {'7' * 32}", exact=True
        ).click()
        page.get_by_role("link", name=f"Open run {run}", exact=True).click()
        open_view(page, "Activity")
        page.get_by_role("button", name="Invocations", exact=True).click()
        history.get_by_role("button").first.click()
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(inspector).to_contain_text("other_target_node")
        expect(inspector.locator(".inspector-identity")).to_contain_text("a" * 32)
        other_time = snapshot.locator("time").get_attribute("datetime")
        with page.expect_response(
            lambda response: response.request == held[0].request
        ) as stale_target_reply:
            held.release(0)
        assert stale_target_reply.value.json()["observed_at"] != other_time
        probe = {"url": held[0].request.url, "method": "GET"}
        page.wait_for_function(
            f"(probe) => {{ const reads = ({PROBED})(probe); "
            "return reads.length === 5 && reads.every(item => item.consumed); }",
            arg=probe,
        )
        page.evaluate(NEXT_FRAMES)
        expect(snapshot.locator("time")).to_have_attribute("datetime", other_time)
        expect(inspector).to_contain_text("other_target_node")
        expect(inspector).not_to_contain_text("refreshed_exact_node")
        expect(inspector).not_to_contain_text("node_aaaaaa")
    shot(page, evidence, "invocation-target-switch-keeps-current-snapshot")
    epoch_values.update(created_at=1e20, deadline="not an epoch", run_deadline=None)
    page.get_by_role("button", name="Refresh", exact=True).click()
    created = inspector.get_by_text("Created", exact=True).locator("..").locator("dd")
    deadline = inspector.get_by_text("Deadline", exact=True).locator("..").locator("dd")
    run_deadline = inspector.get_by_text(
        "Run deadline", exact=True
    ).locator("..").locator("dd")
    expect(created).to_have_text("100000000000000000000")
    expect(deadline).to_have_text("not an epoch")
    expect(run_deadline).to_have_text("none")
    expect(created.locator("time")).to_have_count(0)
    expect(deadline.locator("time")).to_have_count(0)
    expect(run_deadline.locator("time")).to_have_count(0)
    expect(inspector).to_contain_text("other_target_node")
    shot(page, evidence, "invocation-unrepresentable-epochs-faithful")
    assert csp_violations(page) == []



def test_history_sections_never_render_another_sections_rows(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("sections", router_client=fake_router, homes={"crowded": evidence})
    target = {"home": "crowded", "instance": instance, "run": run}
    page.goto(service.url(target, nonce=service.nonce()))
    open_view(page, "Activity")
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
        open_view(page, "Graph")
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
                graph.get_by_role(
                    "radio", name="Grid overview" if engine == "Grid" else engine,
                    exact=True,
                ).check()
                # Capture only the canvas laid out for this plane and engine.
                laid_out = graph.get_by_role(
                    "group",
                    name=f"Captured graph of plane {plane}, {engine.lower()} layout, "
                    "read-only",
                    exact=True,
                )
                expect(laid_out).to_be_visible()
                guidance = graph.locator(".graph-overview-guidance")
                if engine == "Grid":
                    expect(guidance).to_contain_text("emphasizes node positions")
                    expect(guidance).to_contain_text("Directed or the Routes table")
                else:
                    expect(guidance).to_have_count(0)
                expect(page.get_by_text("Directed layout failed")).to_have_count(0)
                if label == "two-plane" and plane == "main":
                    retry = canvas.locator("path.flow-edge--retry")
                    expect(retry).to_have_count(1)
                    expect(retry).to_have_attribute(
                        "d", re.compile(r"^M .+ C .+")
                    )
                    retry_label = canvas.locator(".flow-edge-label--retry")
                    retry_node = canvas.locator('.react-flow__node[data-id="work"]')
                    expect(retry_label).to_be_visible()
                    assert retry_label.evaluate(
                        "(label, node) => label.getBoundingClientRect().bottom "
                        "<= node.getBoundingClientRect().top",
                        retry_node.element_handle()
                    )

                page.wait_for_function("""() => {
                  const canvas = document.querySelector('.graph-canvas');
                  const nodes = [...canvas.querySelectorAll('.flow-node')];
                  const anchor = canvas.querySelector('.flow-node.is-cursor')
                    || nodes.find(node => node.querySelector('.flow-node__marks')
                      ?.textContent.includes('selected'))
                    || nodes.find(node => node.querySelector('.flow-node__marks')
                      ?.textContent.includes('start'));
                  if (!anchor) return false;
                  const box = canvas.getBoundingClientRect();
                  const node = anchor.getBoundingClientRect();
                  return node.left >= box.left && node.top >= box.top
                    && node.right <= box.right && node.bottom <= box.bottom;
                }""")
                # Standard zoom and Fit controls, without the editing toggle: after
                # zooming in, Fit all (by keyboard) shows every laid-out node.
                expect(
                    graph.get_by_role("button", name="Toggle Interactivity")
                ).to_have_count(0)
                zoom_in = graph.get_by_role("button", name="Zoom In", exact=True)
                zoom_in.click()
                zoom_in.click()
                page.wait_for_function(f"() => !({ALL_NODES_IN_CANVAS})()")
                viewport = canvas.locator(".react-flow__viewport")
                manual_view = viewport.get_attribute("style")
                observed_time = page.locator(".status-lead time")
                before_refresh = observed_time.get_attribute("datetime")
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(observed_time).not_to_have_attribute("datetime", before_refresh)
                expect(viewport).to_have_attribute("style", manual_view)
                graph.get_by_role("button", name="Fit all", exact=True).focus()
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


def test_workbench_destinations_focus_after_reveal_and_lazy_report_load(
    live, page, review_gate
):
    service, instance, run = real_run(live, review_gate, "destinations")
    page.goto(
        service.url(
            {"home": "default", "instance": instance, "run": run},
            nonce=service.nonce(),
        )
    )
    expect(heading(page, "Run")).to_be_visible()
    nav = page.get_by_role("navigation", name="Run views")
    current = nav.locator('[aria-current="page"]')
    expect(current).to_have_text("Overview")
    expect(page.locator("#destination-graph")).to_have_count(0)  # unopened: unmounted
    # An ordinary paused run: the lead counts the paused planes and points at
    # their rows; Reports is a secondary link, not the lead button.
    overview = page.get_by_role("region", name="Status")
    resumes = overview.get_by_role("button", name="Review resume")
    expect(resumes.first).to_be_enabled()
    paused = resumes.count()
    # This fresh lead recommends reviewing a resume: the row buttons carry
    # review emphasis.
    for resume in resumes.all():
        expect(resume).to_have_class("button--review")
    # A run opened directly has observed no instance. Its breadcrumb is the
    # exact ID's short form, and no instance read is issued to name it.
    crumbs = page.get_by_role("navigation", name="Target")
    expect(crumbs.locator(".breadcrumbs__name")).to_have_count(0)
    unnamed = crumbs.get_by_role("link", name=f"Instance {instance}", exact=True)
    expect(unnamed.locator("code")).to_have_attribute("title", instance)
    expect(unnamed.locator("code")).to_have_text(f"{instance[:8]}…{instance[-6:]}")
    assert not any(
        f"/views/instances/{instance}?" in item["url"] for item in fetches(page)
    )
    counted = "1 plane is" if paused == 1 else f"{paused} planes are"
    expect(overview.locator(".status-next")).to_contain_text(
        re.compile(rf"{counted} (also )?paused")
    )
    expect(overview.locator(".status-next")).to_contain_text(
        "review a resume in "
        + ("its row" if paused == 1 else "the rows")
        + " under Plane activity."
    )
    for plane in ("main", "side"):
        expect(overview.locator(".status-next")).not_to_contain_text(plane)
    expect(overview.locator(".status-actions")).to_have_count(0)
    # A deliberate view choice focuses that view's stable container, which the
    # chosen button controls and names; the view bar stays where it was.
    for label, view_id in (
        ("Activity", "view-activity"),
        ("Control", "view-controls"),
        ("Overview", "view-overview"),
    ):
        bar_top = nav.evaluate("node => node.getBoundingClientRect().top")
        open_view(page, label)
        expect(current).to_have_text(label)
        expect(current).to_have_attribute("aria-controls", view_id)
        shown = page.locator(f"#{view_id}")
        expect(shown).to_be_focused()
        expect(shown).to_be_visible()
        expect(shown).to_have_attribute("role", "group")
        assert shown.evaluate(
            "node => document.getElementById(node.getAttribute('aria-labelledby'))"
            " === document.querySelector('nav.views [aria-current=\"page\"]')"
        )
        assert nav.evaluate("node => node.getBoundingClientRect().top") == bar_top
        expect(page.locator(".view:not([hidden])")).to_have_count(1)
    assert nav.evaluate("""nav => [...nav.querySelectorAll('.views__tabs button')]
      .every(button => {
        const view = document.getElementById(button.getAttribute('aria-controls'));
        return view !== null && view.classList.contains('view')
          && view.getAttribute('aria-labelledby') === button.id;
      })""")
    expect(page.locator("#view-reports")).to_have_count(1)  # stable before opening
    expect(page.locator("#destination-reports")).to_be_empty()  # and still unread
    open_view(page, "Graph")
    expect(current).to_have_text("Graph")
    expect(page.locator("#view-graph")).to_be_focused()
    adjacent_node = page.get_by_role("table", name="Nodes of plane main").get_by_role(
        "button", name="check", exact=True
    )
    expect(adjacent_node).to_be_visible()
    # Beside the graph, the Inspector starts level with the Graph region and
    # is as tall as its own content, not stretched to the graph's height.
    graph_box = page.get_by_role("region", name="Graph", exact=True).bounding_box()
    inspector_region = page.get_by_role("region", name="Inspector")
    inspector_box = inspector_region.bounding_box()
    assert graph_box is not None and inspector_box is not None
    assert abs(inspector_box["y"] - graph_box["y"]) <= 1
    assert inspector_box["height"] < graph_box["height"]
    assert inspector_region.evaluate(
        "node => { const style = getComputedStyle(node); const inner = "
        "node.clientHeight - parseFloat(style.paddingTop) - "
        "parseFloat(style.paddingBottom); const last = "
        "node.lastElementChild.getBoundingClientRect().bottom - "
        "node.getBoundingClientRect().top - node.clientTop - "
        "parseFloat(style.paddingTop); return inner - last <= 1; }"
    )
    expect(page.get_by_role("separator", name="Resize panels")).to_be_visible()
    expect(
        page.get_by_role("button", name="Widen graph workspace", exact=True)
    ).to_be_enabled()
    adjacent_node.click()
    expect(adjacent_node).to_be_focused()  # visible adjacent split stays in place
    expect(page.locator("#destination-inspector")).not_to_be_focused()
    page.locator("details.layout-menu summary").click()
    page.get_by_role("checkbox", name="Inspector", exact=True).uncheck()
    page.locator("details.layout-menu summary").click()
    expect(page.locator("#destination-inspector")).to_have_count(0)
    adjacent_node.click()
    expect(page.locator("#destination-inspector")).to_be_focused()
    # First and repeated Reports opens show the same durable shell; the Status
    # shortcut focuses it.
    for _ in range(2):
        open_view(page, "Reports")
        expect(current).to_have_text("Reports")
        expect(page.locator("#destination-reports")).to_be_visible()
        expect(page.get_by_role("heading", name="Reports", exact=True)).to_be_visible()
        open_view(page, "Overview")
    page.get_by_role("region", name="Status").get_by_role(
        "button", name="Run reports", exact=True
    ).click()
    # The contextual shortcut focuses its own destination, not the whole view.
    expect(page.locator("#destination-reports")).to_be_focused()
    expect(page.locator("#view-reports")).not_to_be_focused()
    expect(current).to_have_text("Reports")
    open_view(page, "Activity")
    for destination in ("planes", "current-work", "history"):
        expect(page.locator(f"#destination-{destination}")).to_be_visible()
    open_view(page, "Graph")
    expect(page.locator("#destination-revisions")).to_be_visible()
    open_view(page, "Control")
    expect(page.locator("#destination-controls")).to_be_visible()
    open_view(page, "Graph")

    page.locator("details.layout-menu summary").click()
    page.get_by_role("combobox", name="Presentation").select_option("bundled:command")
    page.locator("details.layout-menu summary").click()
    expect(page.locator("#destination-graph")).to_have_count(0)
    page.locator(".view:not([hidden]) .view-hidden").get_by_role(
        "button", name="Show Graph", exact=True
    ).click()
    expect(page.locator("#destination-graph")).to_be_focused()
    nodes = page.get_by_role("table", name="Nodes of plane main")
    expect(nodes).to_be_visible()
    nodes.get_by_role("button", name="check", exact=True).click()
    expect(page.locator("#destination-inspector")).to_be_focused()
    # In a stacked narrow layout, selecting a node takes the operator to detail.
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    expect(heading(page, "Run")).to_be_visible()
    open_view(page, "Graph")
    page.wait_for_function("""() => {
      const canvas = document.querySelector('.graph-canvas');
      const current = canvas?.querySelector('.flow-node.is-cursor');
      if (!current) return false;
      const box = canvas.getBoundingClientRect();
      const node = current.getBoundingClientRect();
      return node.left >= box.left && node.top >= box.top
        && node.right <= box.right && node.bottom <= box.bottom;
    }""")
    expect(page.locator(".graph-canvas").get_by_role(
        "button", name="Readable size (100%)", exact=True
    )).to_have_count(0)
    expect(page.locator(".graph-toolbar").get_by_role(
        "button", name="Fit all", exact=True
    )).to_be_enabled()
    scale = page.get_by_role("group", name="Graph scale", exact=True)
    expect(scale.get_by_role("button")).to_have_count(2)
    assert scale.evaluate("""group => {
      const buttons = group.querySelectorAll('button');
      return buttons[0].offsetTop === buttons[1].offsetTop;
    }""")
    expect(page.get_by_role("separator", name="Resize panels")).to_have_count(0)
    nodes.get_by_role("button", name="check", exact=True).click()
    expect(page.get_by_role(
        "button", name="Inspect selection in Inspector", exact=True
    )).to_have_text("Inspect selection")
    expect(page.locator("#destination-inspector")).to_be_focused()
    expect(page.get_by_role("region", name="Inspector")).to_contain_text("check")
    assert no_page_overflow(page)
    assert csp_violations(page) == []


def test_selected_graph_node_requires_both_plane_and_revision_identity(
    live, page, evidence, fake_router, crowded_ids, monkeypatch
):
    # This test-local projected graph repeats a routine node name across both
    # planes and revisions. It exercises only rendering identity, no Core write.
    from conftest import REVISION, _result

    startup = "8" * 64
    original = fake_router.view_run

    def read_run(home, instance, run, **selectors):
        result = original(home, instance, run, **selectors)
        if selectors.get("section", "summary") == "summary":
            result["data"].update(
                startup_revision_id=startup,
                pending_candidate=None,
                planes={
                    name: {
                        "status": "paused", "cursor": "check",
                        "pause_reason": "review", "resume_target": "check",
                    }
                    for name in ("main", "side")
                },
            )
        return result

    def read_graph(home, instance, run, revision):
        action = {
            "kind": "finish", "target": "succeeded", "reason": None,
            "limit": None, "exhausted": None,
        }
        node = {
            "binding": "command", "timeout_seconds": 10,
            "outcomes": ["READY"], "routes": {"READY": action},
            "on_error": None, "on_unknown": None, "requires_terminal": [],
        }
        return _result("graph", {
            "revision_id": revision, "graph_format": "millrace-router/v4",
            "limits": {}, "bindings": ["command"],
            "planes": {
                name: {"start": "check", "nodes": {"check": node}}
                for name in ("main", "side")
            },
            "seams": {},
        })

    monkeypatch.setattr(fake_router, "view_run", read_run)
    monkeypatch.setattr(fake_router, "view_revision", read_graph)
    instance, run = crowded_ids
    service = live("identity", router_client=fake_router, homes={"crowded": evidence})
    page.goto(
        service.url(
            {"home": "crowded", "instance": instance, "run": run},
            nonce=service.nonce(),
        )
    )
    graph = page.get_by_role("region", name="Graph", exact=True)
    main = page.get_by_role("table", name="Nodes of plane main")
    status = page.get_by_role("region", name="Status")
    status.get_by_role("button", name="Inspect node main / check", exact=True).click()
    expect(page.locator("#destination-inspector")).to_be_focused()
    main.get_by_role("button", name="check", exact=True).click()
    expect(main.locator('tr[aria-selected="true"]')).to_have_count(1)
    inspector = page.get_by_role("region", name="Inspector")
    expect(inspector).to_contain_text(REVISION)
    expect(inspector.locator(".inspector-identity")).to_contain_text("main / check")
    expect(graph.get_by_role(
        "button", name="Widen graph workspace", exact=True
    )).to_have_text("Widen")
    expect(graph.get_by_role(
        "button", name="Inspect selection in Inspector", exact=True
    )).to_have_count(0)
    expect(graph.locator(".graph-legend")).to_contain_text("Recorded cursor: check")
    expect(graph.locator(".graph-legend")).to_contain_text(
        "Inspector node: check"
    )
    graph.get_by_role("combobox", name="Plane", exact=True).select_option("side")
    side = page.get_by_role("table", name="Nodes of plane side")
    expect(side.locator('tr[aria-selected="true"]')).to_have_count(0)
    expect(graph.locator(".react-flow__node.selected")).to_have_count(0)
    expect(inspector).to_contain_text("main")
    expect(graph.locator(".graph-legend")).to_contain_text(
        "Inspector node: none"
    )
    expect(inspector.locator(".inspector-identity")).to_contain_text("main / check")
    side.get_by_role("button", name="check", exact=True).click()
    expect(side.locator('tr[aria-selected="true"]')).to_have_count(1)
    revisions = page.get_by_role("region", name="Revisions")
    startup_row = revisions.get_by_role("row").filter(has_text="Startup")
    startup_row.get_by_role("button", name="Show in Graph", exact=True).click()
    compact_revision = graph.get_by_text(
        startup[:8] + "…" + startup[-6:], exact=True
    )
    expect(compact_revision).to_be_visible()
    expect(side.locator('tr[aria-selected="true"]')).to_have_count(0)
    expect(graph.locator(".react-flow__node.selected")).to_have_count(0)
    expect(inspector).to_contain_text(REVISION)  # exact original selection retained
    assert csp_violations(page) == []


# The node and every ancestor are rendered and not hidden from assistive technology.
EXPOSED = """node => {
  for (let element = node; element; element = element.parentElement) {
    if (element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
  }
  return true;
}"""


def test_cosmetic_observation_clocks_do_not_change_live_announcements(
    live, page, evidence, fake_router, crowded_ids, monkeypatch
):
    original = fake_router.view_run
    changed = {"on": False}
    run_fields: dict = {}

    def run_conditions(home, instance_id, run_id, **selectors):
        result = original(home, instance_id, run_id, **selectors)
        if changed["on"] and selectors.get("section", "summary") == "summary":
            result["data"]["status"] = "paused"
            result["data"]["planes"]["sparse"].update(
                pause_reason="announced_hold", cursor="announce_node"
            )
            result["data"].update(run_fields)
        return result

    monkeypatch.setattr(fake_router, "view_run", run_conditions)
    start = 1_900_000_000_000
    page.clock.install(time=start)
    page.clock.pause_at(start + 1_000)
    instance, run = crowded_ids
    service = live(
        "announcements", router_client=fake_router, homes={"crowded": evidence}
    )
    page.goto(
        service.url(
            {"home": "crowded", "instance": instance, "run": run},
            nonce=service.nonce(),
        )
    )
    status = page.get_by_role("region", name="Status")
    expect(status).to_contain_text("Run status")
    # Exactly one run announcement, outside Status and outside every view.
    announcement = page.locator(".run-announcement")
    expect(announcement).to_have_count(1)
    expect(announcement).to_have_attribute("role", "status")
    expect(announcement).to_have_attribute("aria-live", "polite")
    expect(status.locator('[role="status"]')).to_have_count(0)
    expect(page.locator(".view .run-announcement")).to_have_count(0)
    expect(announcement).to_contain_text("Observed run running.")
    before = announcement.inner_text()
    assert status.locator(".observation").get_attribute("role") is None
    assert status.locator(".observation").get_attribute("aria-live") is None
    assert page.locator(".monitor").get_attribute("role") is None
    page.clock.run_for(1_000)
    expect(announcement).to_have_text(before)

    # Another view hides Overview and its Status region, never the announcement.
    open_view(page, "Activity")
    expect(page.get_by_role("region", name="Status")).to_have_count(0)
    assert announcement.evaluate(EXPOSED)
    expect(
        page.get_by_role("status").filter(has_text="Observed run running.")
    ).to_have_count(1)
    page.clock.run_for(1_000)
    expect(announcement).to_have_text(before)  # a clock tick announces nothing

    # A recorded status and blocker change is announced on this view.
    changed["on"] = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(announcement).to_contain_text("Observed run paused.")
    expect(announcement).to_contain_text(
        "sparse: paused announced_hold at announce_node"
    )
    meaningful = announcement.inner_text()
    assert meaningful != before
    page.clock.run_for(1_000)
    expect(announcement).to_have_text(meaningful)

    # Run-level hold, gate and cancellation facts are announced on this view,
    # where Overview is hidden, when only they change: the status and every
    # plane stay exactly as recorded. A clock tick after each announces nothing.
    gate = "Run gate: learning_ack."
    assert gate in before and gate in meaningful
    for fact in ("Recorded run pause reason", "Run cancellation"):
        expect(announcement).not_to_contain_text(fact)
    spoken = meaningful
    for fields, added, removed in (
        (
            {"pause_reason": "operator_review"},
            "Recorded run pause reason: operator_review.",
            None,
        ),
        ({"run_gate": "release_ack"}, "Run gate: release_ack.", gate),
        ({"cancel_requested": True}, "Run cancellation requested.", None),
    ):
        run_fields.update(fields)
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(announcement).to_contain_text(added)
        if removed is not None:
            expect(announcement).not_to_contain_text(removed)
        expect(announcement).to_contain_text("Observed run paused.")
        expect(announcement).to_contain_text(
            "sparse: paused announced_hold at announce_node"
        )
        changed_text = announcement.inner_text()
        assert changed_text != spoken
        spoken = changed_text
        page.clock.run_for(1_000)
        expect(announcement).to_have_text(spoken)  # the clock alone: unchanged
        assert announcement.evaluate(EXPOSED)
    # A requested cancellation is spoken as requested, never as stopped work.
    for claim in ("cancelled", "stopped", "closed"):
        expect(announcement).not_to_contain_text(claim)
    expect(page.locator(".run-announcement")).to_have_count(1)
    expect(page.get_by_role("region", name="Status")).to_have_count(0)
    meaningful = spoken

    # So is a failed summary read, with the stale facts it leaves.
    fake_router.fail_summary = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(announcement).to_contain_text("router_read_busy")
    expect(announcement).to_contain_text("stale")
    failed = announcement.inner_text()
    assert failed != meaningful
    page.clock.run_for(1_000)
    expect(announcement).to_have_text(failed)
    assert announcement.evaluate(EXPOSED)
    expect(page.locator(".run-announcement")).to_have_count(1)
