"""Exact control requests in the packaged stock client (UI3), over real runs.

Every case uses command-only Core workflows through a real OS service. Sent
bytes are compared with the reviewed bytes; a lost answer after admission is
corroborated by Core's recorded receipt at the service's own transport before
the browser's answer is dropped. Every run a case starts is cancelled through
the public request route before the service stops.
"""

from __future__ import annotations

import contextlib
import json
import re
import time

import pytest

expect = pytest.importorskip(
    "playwright.sync_api", reason="the optional browser extra is not installed"
).expect

POSTS = "**/api/v1/router/requests"
STRICT = "/api/v1/router/views/requests/"


def heading(page, name):
    return page.get_by_role("heading", name=name, level=1)


def posts(page) -> list[dict]:
    return [
        item
        for item in page.evaluate("window.__fetches")
        if item["method"] == "POST" and item["url"].endswith("/api/v1/router/requests")
    ]


def target(instance, run=None):
    value = {"home": "default", "instance": instance}
    return value if run is None else {**value, "run": run}


def paused_run(service, review_gate):
    instance, run = service.create_and_start(review_gate, workspaces=("main", "side"))
    service.settled(instance, run, finished=False)
    return instance, run


def review(form, operation=None):
    """Review one request and return its exact reviewed bytes."""
    if operation is not None:
        form.get_by_role("combobox", name="Request").select_option(operation)
    form.get_by_role("button", name="Review request").click()
    body = form.locator(".request-body")
    expect(body).to_be_visible()
    return body.inner_text()


def send(page, form) -> str:
    with page.expect_request(
        lambda request: request.method == "POST" and request.url.endswith(POSTS[2:])
    ) as sent:
        form.get_by_role("button", name="Send", exact=True).click()
    return sent.value.post_data


def item(page, request_id):
    requests = page.get_by_role("region", name="Requests in this tab")
    return requests.get_by_role("listitem").filter(has_text=request_id)


def recorded(service, request, instance, run=None):
    """Core's recorded receipt through the strict view, once it is not queued."""
    params = {"home": "default", "instance_id": instance}
    if run is not None:
        params["run_id"] = run
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = service.http.get(STRICT + request["request_id"], params=params)
        if response.status_code in (200, 202):
            receipt = response.json()["core_receipt"]
            if receipt and receipt["status"] != "queued":
                return response.json()
        time.sleep(0.05)
    raise AssertionError("no recorded receipt for " + request["request_id"])


def controls(service, instance, run):
    response = service.http.get(
        f"/api/v1/router/views/instances/{instance}/runs/{run}",
        params={"home": "default", "section": "controls"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


def test_start_sends_its_reviewed_bytes_and_binds_the_run_from_the_receipt(
    live, page, review_gate
):
    service = live("start")
    instance = service.create(review_gate, workspaces=("main", "side"))
    page.goto(service.url(target(instance), nonce=service.nonce()))
    form = page.get_by_role("region", name="Start a run")
    expect(form.get_by_role("radio", name="locked")).to_be_checked()
    reviewed = review(form)
    request = json.loads(reviewed)["request"]
    assert list(request) == [
        "format",
        "operation",
        "instance_id",
        "run_id",
        "plane_id",
        "request_id",
        "expected_state_version",
        "payload",
    ]
    assert request["operation"] == "start_run" and request["instance_id"] == instance
    assert request["run_id"] is None and request["plane_id"] is None
    assert request["payload"] == {"edit_mode": "locked"}
    assert request["expected_state_version"] == service.read(instance)["state_version"]
    assert re.fullmatch(r"[0-9a-f]{32}", request["request_id"])
    assert send(page, form) == reviewed  # exactly the reviewed bytes

    run = recorded(service, request, instance)["core_receipt"]["run_id"]
    entry = item(page, request["request_id"])
    expect(entry).to_contain_text("applied")
    expect(entry).to_contain_text("not attested (call/v1")
    try:
        entry.get_by_role("button", name=run).click()
        expect(heading(page, "Run")).to_be_visible()
        assert f"run={run}" in page.url
        expect(page.get_by_role("region", name="Status")).to_contain_text(run)
    finally:
        service.settled(instance, run, finished=False)
        service.cancel(instance, run)
    assert len(posts(page)) == 1


def test_a_stale_reviewed_request_is_refused_and_never_replaced(
    live, page, review_gate
):
    service = live("stale")
    instance, run = paused_run(service, review_gate)
    try:
        page.goto(service.url(target(instance, run), nonce=service.nonce()))
        form = page.get_by_role("region", name="Control this run")
        reviewed = review(form, "resume_run")
        request = json.loads(reviewed)["request"]
        version = request["expected_state_version"]
        assert version == service.read(instance, run)["state_version"]
        # A separate command peer changes the run before this Send.
        assert service.send(service.request("pause_run", instance, run))[
            "status"
        ] == "applied"
        assert service.read(instance, run)["state_version"] > version
        assert send(page, form) == reviewed  # the reviewed version, unrefreshed
        entry = item(page, request["request_id"])
        expect(entry).to_contain_text("refused (stale_state_version)")
        receipt = recorded(service, request, instance, run)["core_receipt"]
        assert receipt["code"] == "stale_state_version"
        # A refresh changes only the display; nothing is resent or replaced.
        page.get_by_role("button", name="Refresh").click()
        expect(entry).to_contain_text("refused (stale_state_version)")
        assert len(posts(page)) == 1
        rows = controls(service, instance, run)
        assert [row["request_id"] for row in rows].count(request["request_id"]) == 1
    finally:
        service.cancel(instance, run)


def test_an_answer_lost_after_admission_is_observed_and_replayed_exactly(
    live, page, review_gate
):
    service = live("lost-answer")
    instance, run = paused_run(service, review_gate)
    admitted = []

    def lose_answer(route):
        if route.request.method != "POST":
            route.continue_()
            return
        raw = route.request.post_data
        # Admit these exact bytes through the service's own bearer transport
        # and confirm Core's recorded receipt before the browser loses the answer.
        response = service.http.post(
            "/api/v1/router/requests",
            content=raw.encode(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in (200, 202), response.text
        request = json.loads(raw)["request"]
        admitted.append((raw, recorded(service, request, instance, run)))
        route.abort()

    try:
        page.goto(service.url(target(instance, run), nonce=service.nonce()))
        form = page.get_by_role("region", name="Control this run")
        page.route(POSTS, lose_answer)
        try:
            reviewed = review(form, "pause_run")
            request = json.loads(reviewed)["request"]
            assert request["expected_state_version"] is None
            assert send(page, form) == reviewed
            entry = item(page, request["request_id"])
            expect(entry).to_contain_text("Delivery unknown")
        finally:
            page.unroute(POSTS, lose_answer)
        assert len(admitted) == 1 and admitted[0][0] == reviewed
        receipt = admitted[0][1]["core_receipt"]
        assert receipt["status"] == "applied"
        # The coordinator finds the recorded receipt by exact ID and target.
        expect(entry).to_contain_text("applied")
        version = service.read(instance, run)["state_version"]
        rows = controls(service, instance, run)

        entry.get_by_role("button", name="Replay exact request…").click()
        with page.expect_request(
            lambda sent: sent.method == "POST" and sent.url.endswith(POSTS[2:])
        ) as replayed:
            entry.get_by_role("button", name="Send exact bytes again").click()
        assert replayed.value.post_data == reviewed
        expect(entry).to_contain_text("The service answered with an observation.")
        expect(entry).to_contain_text("applied")
        assert recorded(service, request, instance, run)["core_receipt"] == receipt
        assert service.read(instance, run)["state_version"] == version
        assert controls(service, instance, run) == rows  # no second action
    finally:
        service.cancel(instance, run)


def test_unknown_requests_survive_presets_navigation_back_and_reload(
    live, page, review_gate
):
    service = live("survive")
    instance, run = paused_run(service, review_gate)

    def never_delivered(route):
        if route.request.method == "POST":
            route.abort()
        else:
            route.continue_()

    started = None
    try:
        page.goto(service.url(target(instance, run), nonce=service.nonce()))
        page.route(POSTS, never_delivered)
        form = page.get_by_role("region", name="Control this run")
        pause = review(form, "pause_run")
        assert send(page, form) == pause
        first = json.loads(pause)["request"]
        entry = item(page, first["request_id"])
        expect(entry).to_contain_text("Delivery unknown")
        # Observation finds nothing: that stays unknown, never "not executed".
        expect(entry).to_contain_text("No observation is available (HTTP 404)")

        # Presentation changes keep the request and send nothing.
        page.locator("details.layout-menu summary").click()
        page.get_by_role("combobox", name="Presentation").select_option(
            "bundled:studio"
        )
        page.locator("details.layout-menu summary").click()
        expect(entry).to_contain_text(first["request_id"])

        # A second unresolved request on another screen, then Back and reload.
        page.get_by_role("navigation", name="Target").get_by_role(
            "link", name=instance
        ).click()
        expect(heading(page, "Instance")).to_be_visible()
        start = page.get_by_role("region", name="Start a run")
        begin = review(start)
        assert send(page, start) == begin
        second = json.loads(begin)["request"]
        expect(item(page, second["request_id"])).to_contain_text("Delivery unknown")
        expect(item(page, first["request_id"])).to_be_visible()
        page.go_back()
        expect(heading(page, "Run")).to_be_visible()
        page.reload()
        expect(heading(page, "Run")).to_be_visible()
        for request in (first, second):
            restored = item(page, request["request_id"])
            expect(restored).to_contain_text("Restored from this tab's history")
        state = page.evaluate("history.state")
        assert set(state) == {"millrace_os_intents"}
        bodies = {intent["body"] for intent in state["millrace_os_intents"]["intents"]}
        assert bodies == {pause, begin}  # exact bytes, nothing else kept
        assert len(posts(page)) == 0  # a reload resends nothing
    finally:
        page.unroute(POSTS, never_delivered)

    try:
        # Explicit exact replays, once delivery works: one action each.
        for request, raw in ((first, pause), (second, begin)):
            entry = item(page, request["request_id"])
            entry.get_by_role("button", name="Replay exact request…").click()
            with page.expect_request(
                lambda sent: sent.method == "POST" and sent.url.endswith(POSTS[2:])
            ) as replayed:
                entry.get_by_role("button", name="Send exact bytes again").click()
            assert replayed.value.post_data == raw
        assert recorded(service, first, instance, run)["core_receipt"]["status"] == (
            "applied"
        )
        started = recorded(service, second, instance)["core_receipt"]["run_id"]
    finally:
        if started is not None:
            service.settled(instance, started, finished=False)
            service.cancel(instance, started)
        service.cancel(instance, run)


def test_reconcile_needs_attestation_and_redirect_reads_the_hidden_graph_once(
    live, page, review_gate
):
    service = live("deliberate")
    instance, run = paused_run(service, review_gate)
    try:
        page.goto(service.url(target(instance), nonce=service.nonce()))
        page.locator("details.layout-menu summary").click()
        page.get_by_role("combobox", name="Presentation").select_option(
            "bundled:command"
        )
        page.locator("details.layout-menu summary").click()
        page.get_by_role("link", name=run, exact=True).click()

        def graph_reads():
            fetched = page.evaluate("window.__fetches")
            return [entry for entry in fetched if "/revisions/" in entry["url"]]

        assert graph_reads() == []  # Graph is hidden

        form = page.get_by_role("region", name="Control this run")
        form.get_by_role("combobox", name="Request").select_option(
            "reconcile_invocation"
        )
        attest = form.get_by_role("checkbox")
        expect(attest).not_to_be_checked()  # never preselected
        form.get_by_role("textbox", name="Invocation").fill("c" * 32)
        form.get_by_role("textbox", name="Reason (required)").fill("operator check")
        form.get_by_role("button", name="Review request").click()
        expect(form.get_by_role("alert")).to_contain_text("attestation")
        expect(form.locator(".request-body")).to_have_count(0)

        form.get_by_role("combobox", name="Request").select_option("redirect_plane")
        node = form.get_by_role("combobox", name="Node (active revision)")
        expect(node).to_be_visible()
        expect(node.locator("option", has_text="check")).to_have_count(1)
        assert len(graph_reads()) == 1  # one bounded read of the active graph
        assert posts(page) == []
    finally:
        service.cancel(instance, run)


SET_HIDDEN = """(hidden) => {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
  document.dispatchEvent(new Event("visibilitychange"));
}"""
# Reads of the selected run summary have reached `count`, and none is in flight.
READS = """([suffix, count]) =>
  window.__fetches.filter((item) => item.method === "GET" && item.url.endsWith(suffix))
    .length >= count && window.__fetches.every((item) => item.consumed)"""
# A fixed fake start; the page clock is paused before any page script runs, so
# time moves only when a case advances it and no request deadline can fire.
CLOCK_START = 1_900_000_000_000


def pause_clock(page):
    page.clock.install(time=CLOCK_START)
    page.clock.pause_at(CLOCK_START + 1_000)


def summary_reads(page, run, home="default"):
    suffix = f"/runs/{run}?home={home}"
    fetched = page.evaluate("window.__fetches")
    return sum(
        1
        for item in fetched
        if item["method"] == "GET" and item["url"].endswith(suffix)
    )


def wait_reads(page, run, count, home="default"):
    page.wait_for_function(READS, arg=[f"/runs/{run}?home={home}", count])


def armed(page, seconds, failures=0):
    """The coordinator names the read it armed; this text is set where it arms it."""
    text = f"Next automatic read in {seconds} s"
    if failures:
        text += f" after {failures} failed read" + ("s" if failures > 1 else "")
    expect(page.get_by_text(text + ".")).to_be_visible()


def test_monitoring_is_finite_and_reads_nothing_while_hidden(
    live, page, review_gate
):
    service = live("monitor")
    instance, run = paused_run(service, review_gate)
    try:
        pause_clock(page)
        page.goto(service.url(target(instance, run), nonce=service.nonce()))
        expect(page.get_by_role("region", name="Status")).to_contain_text(run)
        armed(page, 2)
        before = summary_reads(page, run)
        page.clock.run_for(2_000)
        wait_reads(page, run, before + 1)

        page.evaluate(SET_HIDDEN, True)
        paused = page.get_by_text("Monitoring is paused while this tab is hidden.")
        expect(paused).to_be_visible()
        hidden = summary_reads(page, run)
        page.clock.run_for(20_000)
        assert summary_reads(page, run) == hidden  # a hidden tab reads nothing
        page.evaluate(SET_HIDDEN, False)
        armed(page, 2)

        page.clock.fast_forward(300_000)
        continue_ = page.get_by_role("button", name="Continue monitoring")
        expect(continue_).to_be_visible()
        stopped = summary_reads(page, run)
        page.clock.run_for(20_000)
        assert summary_reads(page, run) == stopped  # the window ended: no reads
        continue_.click()
        armed(page, 2)
        page.clock.run_for(2_000)
        wait_reads(page, run, stopped + 1)
        assert posts(page) == []
    finally:
        service.cancel(instance, run)


def test_failed_reads_back_off_5_15_30_seconds_then_stop_with_retry(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("backoff", router_client=fake_router, homes={"crowded": evidence})
    target_ = {"home": "crowded", "instance": instance, "run": run}
    pause_clock(page)
    page.goto(service.url(target_, nonce=service.nonce()))
    status = page.get_by_role("region", name="Status")
    expect(status).to_contain_text(run)
    armed(page, 2)
    fake_router.fail_summary = True
    count = summary_reads(page, run, "crowded")
    page.clock.run_for(2_000)
    wait_reads(page, run, count + 1, "crowded")
    for failures, seconds in ((1, 5), (2, 15), (3, 30)):
        armed(page, seconds, failures)
        count = summary_reads(page, run, "crowded")
        page.clock.run_for(seconds * 1_000 - 100)
        assert summary_reads(page, run, "crowded") == count  # still backing off
        page.clock.run_for(100)
        wait_reads(page, run, count + 1, "crowded")
    stopped = page.get_by_text("Automatic reads stopped after repeated failures.")
    expect(stopped).to_be_visible()
    count = summary_reads(page, run, "crowded")
    page.clock.run_for(120_000)
    assert summary_reads(page, run, "crowded") == count  # stopped until Retry
    expect(status).to_contain_text("Stale: the latest read failed")

    fake_router.fail_summary = False
    page.get_by_role("button", name="Retry").click()
    armed(page, 2)
    page.clock.run_for(2_000)
    wait_reads(page, run, count + 1, "crowded")
    expect(status).not_to_contain_text("Stale:")


def test_attestations_and_node_choices_never_outlive_their_identity(
    live, page, review_gate
):
    service = live("identity")
    instance, run = paused_run(service, review_gate)
    try:
        listing = service.http.get(
            f"/api/v1/router/views/instances/{instance}/runs/{run}",
            params={"home": "default", "section": "invocations"},
        )
        assert listing.status_code == 200, listing.text
        recorded_row = listing.json()["data"]["items"][0]
        page.goto(service.url(target(instance, run), nonce=service.nonce()))
        form = page.get_by_role("region", name="Control this run")
        form.get_by_role("combobox", name="Request").select_option(
            "reconcile_invocation"
        )
        invocation = form.get_by_role("textbox", name="Invocation")
        attest = form.get_by_role("checkbox")
        invocation.fill("c" * 32)
        attest.check()
        page.get_by_role("button", name="Refresh").click()
        expect(page.get_by_role("region", name="Status")).to_contain_text(run)
        expect(attest).to_be_checked()  # an unchanged summary read keeps it
        invocation.fill("d" * 32)
        expect(attest).not_to_be_checked()  # B never inherits A's attestation
        attest.check()

        page.get_by_role("button", name="Invocations").click()
        rows = page.get_by_role("table", name="Invocations, newest attempt first")
        rows.get_by_role(
            "button", name=recorded_row["invocation_id"], exact=True
        ).click()
        form.get_by_role("button", name="Use selected invocation").click()
        expect(invocation).to_have_value(recorded_row["invocation_id"])
        expect(attest).not_to_be_checked()
        form.get_by_role("textbox", name="Reason (required)").fill("writer stopped")
        form.get_by_role("button", name="Review request").click()
        expect(form.get_by_role("alert")).to_contain_text("attestation")
        attest.check()
        request = json.loads(review(form))["request"]
        assert request["payload"] == {
            "invocation_id": recorded_row["invocation_id"],
            "reason": "writer stopped",
            "stopped": True,
        }
        assert request["plane_id"] == recorded_row["plane_id"]

        form.get_by_role("combobox", name="Request").select_option("redirect_plane")
        plane = form.get_by_role("combobox", name="Plane")
        plane.select_option("main")
        node = form.get_by_role("combobox", name="Node (active revision)")
        node.select_option("check")
        expect(node).to_have_value("check")
        plane.select_option("side")
        expect(node).to_have_value("")  # no node is carried to another plane
        form.get_by_role("button", name="Review request").click()
        expect(form.get_by_role("alert")).to_contain_text("active captured revision")
        expect(form.locator(".request-body")).to_have_count(0)
        plane.select_option("main")
        expect(node).to_have_value("")  # nor restored on the way back
        assert posts(page) == []
    finally:
        service.cancel(instance, run)


# ---- shared read ownership and target-fenced monitoring --------------------------

# Every GET whose URL ends with `suffix` has been issued at least `count` times
# and each has been read to its end.
SETTLED = """([suffix, count]) => {
  const reads = window.__fetches.filter(
    (item) => item.method === "GET" && item.url.endsWith(suffix));
  return reads.length >= count && reads.every((item) => item.consumed);
}"""


class Paused(list):
    """Requests one handler holds; each is released or dropped at most once."""

    def __init__(self):
        super().__init__()
        self.resolved: set[int] = set()

    def release(self, index):
        self.resolved.add(index)
        self[index].continue_()

    def drop(self, index):
        self.resolved.add(index)
        self[index].abort()


@contextlib.contextmanager
def paused_requests(page, pattern, wanted):
    """Hold matching requests. On exit, even after a failure, remove this exact
    handler and abort whatever it still holds."""
    held = Paused()

    def handle(route):
        if wanted(route.request):
            held.append(route)
        else:
            route.continue_()

    page.route(pattern, handle)
    try:
        yield held
    finally:
        page.unroute(pattern, handle)
        for index, route in enumerate(held):
            if index not in held.resolved:
                route.abort()


def until_held(page, held, count):
    deadline = time.monotonic() + 4
    while len(held) < count:
        assert time.monotonic() < deadline, "the held request was never issued"
        page.wait_for_timeout(10)


def strict_reads(page, request_id):
    return sum(
        1
        for item in page.evaluate("window.__fetches")
        if item["method"] == "GET" and f"{STRICT}{request_id}?" in item["url"]
    )


def abort_posts(route):
    if route.request.method == "POST":
        route.abort()
    else:
        route.continue_()


def unknown_pause(page):
    """Send one reviewed pause request whose delivery stays unknown."""
    form = page.get_by_role("region", name="Control this run")
    page.route(POSTS, abort_posts)
    try:
        request = json.loads(review(form, "pause_run"))["request"]
        send(page, form)
    finally:
        page.unroute(POSTS, abort_posts)
    expect(item(page, request["request_id"])).to_contain_text("Delivery unknown")
    return request


def test_one_summary_read_per_target_is_shared_and_still_shown(
    live, page, review_gate
):
    service = live("shared-summary")
    instance, run = paused_run(service, review_gate)
    suffix = f"/runs/{run}?home=default"
    pattern = f"**/api/v1/router/views/instances/{instance}/runs/{run}*"
    try:
        pause_clock(page)  # the held read's own deadline cannot fire
        with paused_requests(
            page,
            pattern,
            lambda request: request.method == "GET" and request.url.endswith(suffix),
        ) as held:
            page.goto(service.url(target(instance, run), nonce=service.nonce()))
            until_held(page, held, 1)
            status = page.get_by_role("region", name="Status")
            expect(status).to_contain_text("Reading run summary")
            # Refresh, then leave this run and come back while the read is held.
            page.get_by_role("button", name="Refresh").click()
            page.get_by_role("navigation", name="Target").get_by_role(
                "link", name=instance
            ).click()
            expect(heading(page, "Instance")).to_be_visible()
            page.go_back()
            expect(heading(page, "Run")).to_be_visible()
            assert summary_reads(page, run) == 1 and len(held) == 1
            held.release(0)
            # The one read's result is shown to whoever is interested now.
            expect(status).to_contain_text("Run status")
            expect(status).not_to_contain_text("Reading run summary")
            page.wait_for_function(SETTLED, arg=[suffix, 1])
            assert summary_reads(page, run) == 1
    finally:
        service.cancel(instance, run)


def test_a_manual_observe_and_the_timer_share_one_receipt_read(
    live, page, review_gate
):
    service = live("shared-receipt")
    instance, run = paused_run(service, review_gate)
    suffix = f"/runs/{run}?home=default"
    try:
        pause_clock(page)
        page.goto(service.url(target(instance, run), nonce=service.nonce()))
        request = unknown_pause(page)
        entry = item(page, request["request_id"])
        armed(page, 1)  # this request's first receipt read
        fragment = f"{STRICT}{request['request_id']}?"
        with paused_requests(
            page,
            f"**{STRICT}{request['request_id']}*",
            lambda sent: sent.method == "GET" and fragment in sent.url,
        ) as held:
            entry.get_by_role("button", name="Observe").click()
            until_held(page, held, 1)
            before = summary_reads(page, run)
            page.clock.run_for(1_000)  # the timer's batch reads the summary ...
            page.wait_for_function(SETTLED, arg=[suffix, before + 1])
            assert strict_reads(page, request["request_id"]) == 1  # ... and shares
            held.release(0)
            expect(entry).to_contain_text("No observation is available (HTTP 404)")
        assert strict_reads(page, request["request_id"]) == 1
    finally:
        service.cancel(instance, run)


def test_a_reply_for_another_target_never_restarts_its_monitoring(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("fenced", router_client=fake_router, homes={"crowded": evidence})
    pause_clock(page)
    home = {"home": "crowded", "instance": instance}
    page.goto(service.url(home, nonce=service.nonce()))
    start = page.get_by_role("region", name="Start a run")
    with paused_requests(page, POSTS, lambda sent: sent.method == "POST") as held:
        request = json.loads(review(start))["request"]
        start.get_by_role("button", name="Send", exact=True).click()
        until_held(page, held, 1)
        # Select another target (this instance's run) while Start's answer waits.
        page.get_by_role("link", name=run, exact=True).click()
        expect(heading(page, "Run")).to_be_visible()
        armed(page, 2)
        fake_router.summary_budget = True
        page.clock.run_for(2_000)
        retry = page.get_by_role("button", name="Retry")
        expect(retry).to_be_visible()  # the run's reads stopped on the refusal
        reads = summary_reads(page, run, "crowded")
        held.drop(0)  # Start's answer is lost now, for the instance target
        expect(item(page, request["request_id"])).to_contain_text("Delivery unknown")
        expect(retry).to_be_visible()  # that reply restarted nothing here
        page.clock.run_for(20_000)
        assert summary_reads(page, run, "crowded") == reads
    assert strict_reads(page, request["request_id"]) == 0


def test_a_summary_budget_refusal_stops_before_any_receipt_read(
    live, page, evidence, fake_router, crowded_ids
):
    instance, run = crowded_ids
    service = live("budget", router_client=fake_router, homes={"crowded": evidence})
    target_ = {"home": "crowded", "instance": instance, "run": run}
    pause_clock(page)
    page.goto(service.url(target_, nonce=service.nonce()))
    expect(page.get_by_role("region", name="Status")).to_contain_text(run)
    request = unknown_pause(page)
    armed(page, 1)  # the batch would read the summary, then this receipt
    fake_router.summary_budget = True
    page.clock.run_for(1_000)
    refused = page.get_by_text("the bounded view refused this target")
    expect(refused).to_be_visible()
    assert strict_reads(page, request["request_id"]) == 0  # no receipt read followed
    reads = summary_reads(page, run, "crowded")
    page.clock.run_for(60_000)
    assert summary_reads(page, run, "crowded") == reads  # never retried by itself
    assert strict_reads(page, request["request_id"]) == 0
