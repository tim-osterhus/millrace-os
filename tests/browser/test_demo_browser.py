"""D1 composition on the same packaged stock shell and generic renderer."""

import copy

import pytest
from conftest import INSTANCE, PAGE_PROBES, RUN

expect = pytest.importorskip("playwright.sync_api").expect


def context(*, outputs=None):
    return {
        "format": "millrace-os-demo-context/v1",
        "home": "demo",
        "instance_id": INSTANCE,
        "run_id": RUN,
        "finish_available": True,
        "accepted_output_refs": outputs or [],
        "output_error": None,
    }


def pin(output_id="first-report", digest="1"):
    return {
        "reference": {
            "instance_id": INSTANCE,
            "run_id": RUN,
            "output_id": output_id,
            "record_sha256": digest * 64,
        },
        "report_sha256": "e" * 64,
        "json_sha256": None,
    }


def test_demo_uses_stock_shell_pins_refresh_and_only_matching_finish(
    live, fake_router, browser
):
    state = context(outputs=[pin()])
    finishes = []
    service = live(
        router_client=fake_router,
        reader=lambda: copy.deepcopy(state),
        finish_request=lambda: finishes.append(True),
    )
    with browser.new_context() as session:
        session.add_init_script(PAGE_PROBES)
        page = session.new_page()
        page.goto(
            service.url(
                {"home": "demo", "instance": INSTANCE, "run": RUN},
                nonce=service.nonce(),
            )
        )
        finish = page.get_by_role("button", name="Finish", exact=True)
        expect(finish).to_be_enabled()
        expect(page.get_by_text("ordinary service", exact=True)).to_have_count(0)
        assert page.url.split("?")[0].endswith("/ui/stock/")
        page.get_by_role("button", name="Reports", exact=True).click()
        expect(
            page.get_by_role("heading", name="Report first-report", exact=True)
        ).to_be_visible()
        # Discovery contains a second record, but it cannot replace an unpinned
        # main report. Context refresh, not discovery order, admits it.
        expect(
            page.get_by_role("button", name="second-report", exact=True)
        ).to_be_disabled()
        # Plus puts the accepted final report first, retaining the baseline pin.
        state["accepted_output_refs"].insert(0, pin("second-report", "2"))
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(
            page.get_by_role("heading", name="Report second-report", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="Control", exact=True).click()
        # The parent writable guard disables reviewed forms; the shared API
        # send guard also protects old retained Replay/validation/layout calls.
        assert (
            page.locator("main").get_by_role("button", name="Send", exact=True).count()
            == 0
            or not page.locator("main")
            .get_by_role("button", name="Send", exact=True)
            .is_enabled()
        )

        def mutations():
            return [
                item
                for item in page.evaluate("window.__fetches")
                if item["method"] in {"POST", "PUT"}
                and not item["url"].endswith("/auth/exchange")
            ]

        assert mutations() == []
        finish.click()
        expect(
            page.get_by_role("button", name="Finish requested", exact=True)
        ).to_be_disabled()
        expect(
            page.get_by_role("status").filter(
                has_text="Intent accepted. Shutdown, export and writer closure "
                "are still being observed by the terminal."
            )
        ).to_be_visible()
        assert finishes == [True]
        assert len(mutations()) == 1
        assert mutations()[0]["url"].endswith("/api/v1/demo/finish")
        assert service.http.get("/health").status_code == 200


def test_demo_context_digest_mismatch_never_renders_discovery_report(
    live, fake_router, browser
):
    accepted = pin()
    accepted["report_sha256"] = "f" * 64
    service = live(
        router_client=fake_router, reader=lambda: context(outputs=[accepted])
    )
    with browser.new_context() as session:
        page = session.new_page()
        page.goto(
            service.url(
                {"home": "demo", "instance": INSTANCE, "run": RUN},
                nonce=service.nonce(),
            )
        )
        page.get_by_role("button", name="Reports", exact=True).click()
        refusal = page.get_by_role("article", name="Output first-report").get_by_role(
            "status"
        )
        expect(refusal).to_contain_text("demo_output_mismatch")
        expect(refusal).to_contain_text(
            "The output does not match the coordinator's accepted reference and digest."
        )
        expect(
            page.get_by_role("heading", name="Report first-report", exact=True)
        ).to_have_count(0)


def test_pending_failed_and_mismatched_target_keep_finish_unavailable(
    live, fake_router, browser
):
    from millrace_os.service import DemoContextPending

    mode = ["pending"]

    def reader():
        if mode[0] == "pending":
            raise DemoContextPending
        if mode[0] == "failed":
            raise ValueError("private")
        return context()

    service = live(router_client=fake_router, reader=reader)
    with browser.new_context() as session:
        page = session.new_page()
        page.goto(
            service.url(
                {"home": "demo", "instance": INSTANCE, "run": "f" * 32},
                nonce=service.nonce(),
            )
        )
        expect(
            page.get_by_text("Service context is pending", exact=False)
        ).to_be_visible()
        expect(page.get_by_role("button", name="Finish", exact=True)).to_have_count(0)
        mode[0] = "failed"
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(
            page.get_by_role("status").filter(
                has_text="Service context is unknown (demo_context_failed)"
            )
        ).to_be_visible()
        mode[0] = "bound"
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(
            page.get_by_text("owned demo service for instance", exact=False)
        ).to_be_visible()
        expect(page.get_by_role("button", name="Finish", exact=True)).to_have_count(0)


@pytest.mark.parametrize("service_kind", ["demo", "ordinary"])
@pytest.mark.parametrize("failure_kind", ["pending", "failed"])
def test_failed_context_retains_exact_target_report_as_stale(
    live, fake_router, browser, service_kind, failure_kind
):
    from millrace_os.service import DemoContextPending

    mode = ["ready"]

    def reader():
        if mode[0] == "pending":
            raise DemoContextPending
        if mode[0] == "failed":
            raise ValueError("private")
        return context(outputs=[pin()])

    service = live(
        router_client=fake_router,
        reader=reader if service_kind == "demo" else None,
    )
    with browser.new_context() as session:
        session.add_init_script(PAGE_PROBES)
        page = session.new_page()
        page.goto(
            service.url(
                {"home": "demo", "instance": INSTANCE, "run": RUN},
                nonce=service.nonce(),
            )
        )
        page.get_by_role("button", name="Reports", exact=True).click()
        if service_kind == "ordinary":
            page.get_by_role("button", name="first-report", exact=True).click()
        report = page.get_by_role("heading", name="Report first-report", exact=True)
        expect(report).to_be_visible()
        reads = fake_router.calls.count("output:first-report")
        mode[0] = failure_kind
        if service_kind == "ordinary":
            # The fixed ordinary constructor has no reader. Fail its next
            # browser read without changing that constructor contract.
            page.route(
                "**/api/v1/demo/context",
                lambda route: route.fulfill(
                    status=503,
                    json={
                        "contract": "os.error.v1",
                        "code": "demo_context_" + failure_kind,
                    },
                ),
            )
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(
            page.get_by_role("article", name="Output first-report").get_by_role(
                "status"
            )
        ).to_have_text(
            "Service context is unavailable. Showing the last successfully "
            "read report for this target; it is stale."
        )
        expect(report).to_be_visible()
        assert fake_router.calls.count("output:first-report") == reads
        expect(page.get_by_role("button", name="Finish", exact=True)).to_have_count(0)
        if service_kind == "demo":
            expect(
                page.get_by_role("button", name="second-report", exact=True)
            ).to_be_disabled()
        assert not [
            item
            for item in page.evaluate("window.__fetches")
            if item["method"] in {"POST", "PUT"}
            and not item["url"].endswith("/auth/exchange")
        ]
        # A same-document target change must not show the retained data under
        # another run, including while context is still unavailable.
        page.evaluate("""() => {
          const next = new URL(location.href);
          next.searchParams.set('run', 'f'.repeat(32));
          history.pushState(history.state, '', next);
          dispatchEvent(new PopStateEvent('popstate', {state: history.state}));
        }""")
        page.get_by_role("button", name="Reports", exact=True).click()
        expect(
            page.get_by_role("table", name="Published outputs, by output ID")
        ).to_be_visible()
        expect(report).to_have_count(0)
        expect(page.get_by_role("article", name="Output first-report")).to_have_count(0)
