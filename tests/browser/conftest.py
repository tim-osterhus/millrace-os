"""Real bundled-browser lane for the stock client (Python Playwright 1.63.0).

The lane needs the optional `browser` extra and its matching Chromium build
(revision 1243). Without the extra it is skipped with that reason; acceptance
runs require zero skips. `MILLRACE_OS_BROWSER_EXECUTABLE` may name the exact
verified browser binary, and `MILLRACE_OS_BROWSER_EVIDENCE` a directory for
screenshots and violation logs. No test here calls a native provider: real
runs use the Core command adapter only.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="the optional browser extra is not installed"
)

from millrace_os.config import ApplicationConfig  # noqa: E402
from millrace_os.credentials import CredentialStore  # noqa: E402
from millrace_os.router_client import RouterUnavailable  # noqa: E402
from millrace_os.service import Service, ServiceDependencies  # noqa: E402

# Recorded in every page before its own scripts run (not subject to page CSP).
PAGE_PROBES = """
window.__csp = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__csp.push({directive: event.effectiveDirective, blocked: event.blockedURI,
    source: event.sourceFile, line: event.lineNumber, sample: event.sample});
});
window.__fetches = [];
const originalFetch = window.fetch;
// `consumed` turns true when the application's own reader of that response
// body returns done. Only that flag is recorded: no body, header or value is
// read, and the application gets the original promise, response and stream.
function observeConsumption(response, record) {
  const stream = response.body;
  if (stream === null) return;
  const getReader = stream.getReader;
  stream.getReader = function () {
    const reader = getReader.apply(this, arguments);
    const read = reader.read;
    reader.read = function () {
      const step = read.apply(this, arguments);
      step.then((result) => { if (result.done) record.consumed = true; }, () => {});
      return step;
    };
    return reader;
  };
}
window.fetch = function (input, init) {
  const record = {url: String(input), hash: location.hash,
    method: (init && init.method) || "GET", consumed: false};
  window.__fetches.push(record);
  const pending = originalFetch.apply(this, arguments);
  pending.then((response) => observeConsumption(response, record), () => {});
  return pending;
};
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def evidence(tmp_path_factory) -> Path:
    selected = os.environ.get("MILLRACE_OS_BROWSER_EVIDENCE")
    path = Path(selected) if selected else tmp_path_factory.mktemp("browser-evidence")
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def browser(evidence):
    with sync_api.sync_playwright() as playwright:
        executable = os.environ.get("MILLRACE_OS_BROWSER_EXECUTABLE") or None
        instance = playwright.chromium.launch(
            headless=True, executable_path=executable
        )
        (evidence / "browser.json").write_text(
            json.dumps(
                {
                    "browser": "chromium",
                    "version": instance.version,
                    "executable": executable,
                    "platform": sys.platform,
                },
                indent=1,
            )
        )
        try:
            yield instance
        finally:
            instance.close()


class Live:
    """One real OS service on a free loopback port, plus bearer API access."""

    def __init__(self, root: Path, *, homes=None, router_client=None, reader=None):
        self.root = root
        self.config = ApplicationConfig(
            config_dir=root / "config",
            state_dir=root / "os-state",
            credential_path=root / "config" / "credentials.json",
            port=_free_port(),
            router_state_homes=homes
            if homes is not None
            else {"default": root / "core"},
        )
        self.dependencies = ServiceDependencies(
            router_client=router_client, demo_context_reader=reader
        )
        self.projects: list[Path] = []
        self._start()

    def _start(self):
        self.service = Service(self.config, dependencies=self.dependencies)
        self.service.start()
        self.base = self.service.browser_endpoint.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base,
            headers={
                "Authorization": "Bearer "
                + CredentialStore(self.config.credential_path).ensure()
            },
            timeout=5,
            trust_env=False,
        )

    def restart(self):
        """The same config root on a new port: a new origin, the same saved data."""
        self.service.stop()
        self.http.close()
        self.config = replace(self.config, port=_free_port())
        self._start()

    def nonce(self, *, recovery: bool = False) -> str:
        response = self.http.post(
            "/api/v1/auth/bootstrap", json={"recovery": recovery}
        )
        assert response.status_code == 200, response.text
        return response.json()["nonce"]

    def url(self, target: dict[str, str] | None = None, *, nonce: str | None = None):
        query = "&".join(f"{key}={value}" for key, value in (target or {}).items())
        return (
            f"{self.base}/ui/stock/"
            + (f"?{query}" if query else "")
            + (f"#{nonce}" if nonce else "")
        )

    def stop(self):
        for project in self.projects:
            (project / "release").write_text("browser test cleanup")
        self.service.stop()
        self.http.close()

    # -- command-only Core controls through the public request route --------

    def request(
        self, operation, instance=None, run=None, *, version=None, payload=None
    ):
        return {
            "format": "millrace-router-request/v2",
            "operation": operation,
            "instance_id": instance,
            "run_id": run,
            "plane_id": None,
            "request_id": uuid4().hex,
            "expected_state_version": version,
            "payload": payload or {},
        }

    def send(self, request):
        response = self.http.post(
            "/api/v1/router/requests", json={"home": "default", "request": request}
        )
        assert response.status_code in (200, 202), response.text
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            observed = self.http.get(
                "/api/v1/router/requests/" + request["request_id"],
                params={"home": "default"},
            )
            assert observed.status_code in (200, 202), observed.text
            receipt = observed.json()["core_receipt"]
            if receipt and receipt["status"] != "queued":
                return receipt
            time.sleep(0.05)
        raise AssertionError("request did not settle: " + request["request_id"])

    def read(self, instance, run=None):
        path = f"/api/v1/router/instances/{instance}"
        path += f"/runs/{run}" if run else ""
        response = self.http.get(path, params={"home": "default"})
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def create_and_start(self, files_by_name: dict[str, str], *, workspaces=()):
        folder = self.root / f"workflow-{len(self.projects)}"
        workflow, project = folder / "workflow", folder / "project"
        workflow.mkdir(parents=True)
        project.mkdir()
        for name in workspaces:
            (project / name).mkdir(parents=True, exist_ok=True)
        self.projects.append(project)
        for name, content in files_by_name.items():
            (workflow / name).write_text(content)
        receipt = self.send(
            self.request(
                "create_instance",
                version=0,
                payload={
                    "workflow_file": str(workflow / "workflow.json"),
                    "project_root": str(project),
                    "path": [str(Path(sys.executable).parent), "/usr/bin", "/bin"],
                    "env_allowlist": [],
                    "host_paths": {},
                },
            )
        )
        assert receipt["status"] == "applied", receipt
        instance = receipt["instance_id"]
        started = self.send(
            self.request(
                "start_run",
                instance,
                version=self.read(instance)["state_version"],
                payload={"edit_mode": "locked"},
            )
        )
        assert started["status"] == "applied", started
        return instance, started["run_id"]

    def settled(self, instance, run, *, timeout=20, finished=True):
        """Quiescent (every writer stopped) and, when `finished`, not running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.read(instance, run)
            if value.get("quiescent") and (
                not finished or value.get("status") != "running"
            ):
                return value
            time.sleep(0.05)
        raise AssertionError("run did not settle")


def _python_binding() -> dict:
    return {
        "adapter": "command",
        "contract": "millrace-command/v1",
        "executable": sys.executable,
        "config": {},
    }


def review_gate_files() -> dict[str, str]:
    """Two-plane command graph: work pauses at a review gate in `main`."""
    work = (
        "import json\nfrom pathlib import Path\n"
        "Path('answer.txt').write_text('42\\n')\n"
        "print(json.dumps({'outcome':'READY','refs':['answer.txt']}))\n"
    )

    def node(script, outcomes, routes):
        return {
            "invoke": {
                "binding": "python",
                "timeout_seconds": 10,
                "config": {"script": script},
            },
            "result": "json-v1",
            "outcomes": outcomes,
            "on": routes,
            "on_error": {"pause": {"reason": "execution_error"}},
            "on_unknown": {"pause": {"reason": "unknown"}},
        }

    graph = {
        "format": "millrace-router/v3",
        "limits": {
            "max_invocations": 8,
            "max_elapsed_seconds": 60,
            "max_parallel_invocations": 1,
        },
        "bindings": {"python": _python_binding()},
        "control_files": ["work.py"],
        "planes": {
            "main": {
                "workspace": "main",
                "start": "work",
                "nodes": {
                    "work": node(
                        "work.py",
                        ["READY", "RETRY"],
                        {
                            "READY": {"pause": {"reason": "review", "resume": "check"}},
                            "RETRY": {
                                "retry": {"limit": 2, "exhausted": {"finish": "failed"}}
                            },
                        },
                    ),
                    "check": node(
                        "work.py", ["READY"], {"READY": {"finish": "succeeded"}}
                    ),
                },
            },
            "side": {
                "workspace": "side",
                "start": "note",
                "nodes": {
                    "note": node(
                        "work.py", ["READY"], {"READY": {"pause": {"reason": "idle"}}}
                    )
                },
            },
        },
    }
    return {"workflow.json": json.dumps(graph), "work.py": work}


def lad_topology_files() -> tuple[dict[str, str], tuple[str, ...]]:
    """The installed Plus `bundled:lad` topology with every node made a no-op.

    Node IDs, outcomes, routes, error/unknown actions, retries, terminal
    dependencies, planes and seams stay exactly as published; only invocation
    bindings become one command no-op and explicit test bounds are set. Its
    printed text is not a result, so each node routes through `on_unknown`.
    """
    graph = json.loads(
        files("millrace_plus").joinpath("workflows", "lad", "workflow.json").read_text()
    )
    graph["bindings"] = {"python": _python_binding()}
    graph["defaults"] = {"timeout_seconds": 10}
    graph["limits"] = {
        "max_invocations": 4,
        "max_elapsed_seconds": 60,
        "max_parallel_invocations": 1,
    }
    graph["control_files"] = ["noop.py"]
    for plane in graph["planes"].values():
        for node in plane["nodes"].values():
            node["invoke"] = {
                "binding": "python",
                "timeout_seconds": 10,
                "config": {"script": "noop.py"},
            }
    workspaces = tuple(plane["workspace"] for plane in graph["planes"].values())
    files_by_name = {"workflow.json": json.dumps(graph), "noop.py": "print('no-op')\n"}
    return files_by_name, workspaces


@pytest.fixture
def live(tmp_path):
    services: list[Live] = []

    def make(name="service", **kwargs):
        value = Live(tmp_path / name, **kwargs)
        services.append(value)
        return value

    yield make
    for value in reversed(services):
        value.stop()


@pytest.fixture
def page_errors() -> list[str]:
    return []


# Chromium logs every fetch answered with HTTP 404 as this console error.
NOT_FOUND = (
    "Failed to load resource: the server responded with a status of 404 (Not Found)"
)
# An ordinary service answers this read with its typed 404; that is the client's
# only proof of an ordinary context, so the line it logs is expected.
DEMO_CONTEXT = "/api/v1/demo/context"


def _typed_demo_context(response, *, evidence: dict | None = None) -> bool:
    """A GET answered with application/json os.error.v1 demo_context_unavailable."""
    if response.request.method != "GET":
        return False
    if response.headers.get("content-type") != "application/json":
        return False
    try:
        body = response.json()
    except (sync_api.Error, ValueError) as error:
        if evidence is not None:
            evidence["body_read_exception_type"] = type(error).__name__
        return False
    return (
        isinstance(body, dict)
        and body.get("contract") == "os.error.v1"
        and body.get("code") == "demo_context_unavailable"
    )


@pytest.fixture
def page(browser, evidence, request, page_errors):
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    context.add_init_script(PAGE_PROBES)
    page = context.new_page()
    # One console 404 line is exempt only when its exact source URL pairs with
    # one observed typed demo-context 404 response. Either event may arrive
    # first, so the second completes the pair; anything unpaired stays an error.
    typed, logged = [], []
    resource_errors = []

    def console(message):
        if message.type != "error":
            return
        source = message.location.get("url", "")
        resource_errors.append(
            {
                "kind": "console",
                "message_class": "resource_not_found"
                if message.text == NOT_FOUND
                else "console_error",
                "source_path": urlsplit(source).path,
            }
        )
        if message.text == NOT_FOUND and urlsplit(source).path == DEMO_CONTEXT:
            if source in typed:
                typed.remove(source)
                return
            logged.append(source)
        page_errors.append(message.text)

    def response(reply):
        if reply.status != 404:
            return
        record = {
            "kind": "response",
            "method": reply.request.method,
            "path": urlsplit(reply.url).path,
            "status": reply.status,
            "content_type": reply.headers.get("content-type"),
        }
        resource_errors.append(record)

    def request_finished(finished):
        if urlsplit(finished.url).path != DEMO_CONTEXT:
            return
        record = {
            "kind": "requestfinished",
            "method": finished.method,
            "path": urlsplit(finished.url).path,
            "strict_demo_context": False,
        }
        resource_errors.append(record)
        try:
            reply = finished.response()
        except sync_api.Error as error:
            record["response_lookup_exception_type"] = type(error).__name__
            return
        if reply is None:
            record["response_missing"] = True
            return
        record["status"] = reply.status
        record["content_type"] = reply.headers.get("content-type")
        if reply.status != 404:
            return
        record["strict_demo_context"] = _typed_demo_context(reply, evidence=record)
        if not record["strict_demo_context"]:
            return
        if reply.url in logged:
            logged.remove(reply.url)
            page_errors.remove(NOT_FOUND)
        else:
            typed.append(reply.url)

    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", console)
    page.on("response", response)
    page.on("requestfinished", request_finished)
    try:
        yield page
    finally:
        try:
            violations = (
                page.evaluate("window.__csp || []") if not page.is_closed() else []
            )
            name = request.node.name.replace("/", "_")
            (evidence / f"{name}.csp.json").write_text(json.dumps(violations, indent=1))
            (evidence / f"{name}.resource-errors.json").write_text(
                json.dumps(resource_errors, indent=1)
            )
        finally:
            context.close()


# ---- a fake router client for crowded and uncertain states --------------------

LONG = "plane-" + "x" * 58
INSTANCE = "1" * 32
RUN = "2" * 32
REVISION = "3" * 64
CANDIDATE_REVISION = "4" * 64


def _result(kind, data, *, items=(), next_cursor=None, run_id=RUN):
    return {
        "format": "millrace-router-inspection/v1",
        "kind": kind,
        "instance_id": INSTANCE,
        "run_id": run_id,
        "state_version": 41,
        "items": list(items),
        "next_cursor": next_cursor,
        "data": data,
    }


def crowded_summary() -> dict:
    planes = {}
    for index in range(12):
        plane_id = f"{LONG[:60]}{index:02d}"
        planes[plane_id] = {
            "status": ["running", "paused", "ready", "cancelled"][index % 4],
            "cursor": f"node_{'y' * 50}_{index}",
            "active_invocation": f"{index:032x}" if index % 4 == 0 else None,
            "pause_reason": "operator_decision_required" if index % 4 == 1 else None,
            "resume_target": f"node_{index}" if index % 4 == 1 else None,
            "dependencies": [{"plane_id": "other", "status": "succeeded"}]
            if index % 3 == 0
            else [],
            "selected": {},
            "progress": {},
            "pending_completion_ids": [f"{index + 100:032x}"] if index == 5 else [],
            "last_event": None,
        }
    # One plane whose projection lacks most keys: every missing fact is unknown.
    planes["sparse"] = {"status": "running"}
    return {
        "run_id": RUN,
        "status": "running",
        "mode": "locked",
        "state_version": 41,
        "run_gate": "learning_ack",
        "pause_reason": None,
        "cancel_requested": False,
        "invocation_count": 97,
        "limits": {
            "max_invocations": None,
            "max_elapsed_seconds": None,
            "startup_max_invocations": None,
            "startup_max_elapsed_seconds": None,
            "started_at": "2026-11-08T00:00:00+00:00",
            "deadline": None,
            "startup_deadline": None,
        },
        "startup_revision_id": REVISION,
        "active_revision_id": REVISION,
        "pending_candidate": {
            "seq": 3,
            "candidate_id": "5" * 32,
            "revision_id": CANDIDATE_REVISION,
            "diagnostic": "invalid_graph",
            "changed_paths": ["workflow.json", "prompts/" + "deep/" * 20 + "x.md"],
            "withdrawn": False,
            "supersedes": None,
            "version": 40,
            "request_id": "6" * 32,
        },
        "quiescent": False,
        "inflight_invocations": [f"{index:032x}" for index in range(0, 12, 4)],
        "writer_counts": {
            "recorded": {"stopped": 80, "may_live": 1},
            "effective": {"stopped": 90, "may_live": 1, "unknown": 2, "running": 3},
        },
        "planes": planes,
        "seams": {},
    }


def invocation_row(invocation_id: str) -> dict:
    """One frozen-key invocation projection whose node names its own ID."""
    return {
        "invocation_id": invocation_id,
        "attempt": 1,
        "plane_id": "main",
        "node_id": f"node_{invocation_id[-6:]}",
        "status": "active",
        "producing_revision_id": REVISION,
        "created_at": "2026-11-08T00:00:00+00:00",
        "deadline": None,
        "run_deadline": None,
        "started_version": 3,
        "completed_version": None,
        "parsed_version": None,
        "result_contract": "json-v1",
        "uncertainty_diagnostic": None,
        "writer": {"original": "running", "effective": "may_live"},
        "completion": None,
        "reconciliation": None,
        "settings": None,
        "unavailable_reason": {},
    }


OUTPUTS = ("first-report", "second-report")


def output_item(output_id: str, index: int) -> dict:
    return {
        "output_id": output_id,
        "availability": "available",
        "reason": None,
        "record_sha256": str(index + 1) * 64,
        "producer": {"id": "example.exporter", "version": "1.0.0"},
        "label": f"Label {output_id}",
        "published_at": "2026-11-08T12:00:00Z",
        "invocation": {
            "plane_id": "main",
            "node_id": "work",
            "invocation_id": "a" * 32,
            "producing_revision_id": REVISION,
        },
        "representations": {
            "report": {
                "format": "millrace-report-display/v1",
                "media_type": "application/json",
                "sha256": "e" * 64,
            },
            "json": None,
        },
    }


class FakeRouter:
    """Serves fixed projections whose rows name the target they were read for.

    `fail_summary` simulates a later read failure; `reconciled` reports a run
    whose recorded writer is still `may_live` but whose effective writers are
    all stopped after an operator reconciliation.
    """

    def __init__(self):
        self.fail_summary = False
        self.reconciled = False
        self.calls: list[str] = []

    def _control(self):
        return self

    def _inspection(self):
        return self

    def _views(self):
        return self

    def _catalog(self):
        return self

    def list_homes(self, *, limit=50, cursor=None):
        return {"items": ["crowded", "z" * 90], "next_cursor": None}

    def view_instances(self, home, *, limit=50, cursor=None):
        self.calls.append("instances")
        item = {
            "instance_id": INSTANCE,
            "settings_version": 3,
            "project_root": "/very/long/" + "path/" * 30 + f"{home}-project",
            "workflow_file": "/very/long/" + "path/" * 30 + "workflow.json",
            "store_format": "millrace-router-store/v7",
            "error_code": None,
        }
        refused = dict.fromkeys(item) | {
            "instance_id": "7" * 32,
            "error_code": "store_integrity_error",
        }
        return _result("instances", {}, items=[item, refused], run_id=None)

    def view_instance(self, home, instance_id, *, limit=50, cursor=None):
        self.calls.append("instance")
        data = {
            "instance_id": instance_id,
            "project_root": f"/p/{instance_id}",
            "workflow_file": f"/p/{instance_id}/workflow.json",
            "settings_version": 3,
            "runtime_defaults": {"timeout_seconds": 1800},
            "operator_policy": {},
        }
        run = {
            "run_id": RUN,
            "status": "running",
            "mode": "locked",
            "state_version": 41,
            "active_revision_id": REVISION,
            "pause_reason": None,
        }
        return _result("instance", data, items=[run], run_id=None)

    def view_run(self, home, instance_id, run_id, **selectors):
        section = selectors.get("section", "summary")
        self.calls.append("run:" + section)
        if section == "summary":
            if self.fail_summary:
                raise RouterUnavailable("router_read_busy", 429)
            summary = crowded_summary()
            if self.reconciled:
                summary["quiescent"] = True
                summary["inflight_invocations"] = []
                summary["writer_counts"] = {
                    "recorded": {"stopped": 80, "may_live": 1},
                    "effective": {"stopped": 81},
                }
            return _result("run-summary", summary)
        if section == "invocations":
            exact = selectors.get("invocation_id")
            rows = [invocation_row(exact)] if exact else [invocation_row("a" * 32)]
            return _result("invocations", {}, items=rows)
        return _result(section, {})

    def view_revision(self, home, instance_id, run_id, revision_id):
        self.calls.append("graph")
        raise RouterUnavailable("core_inspection_too_large", 409)

    def list_outputs(self, home, instance_id, run_id, *, limit=50, cursor=None):
        self.calls.append("outputs")
        return {
            "instance_id": instance_id,
            "run_id": run_id,
            "items": [output_item(name, index) for index, name in enumerate(OUTPUTS)],
            "next_cursor": None,
        }

    def read_output(
        self, home, instance_id, run_id, output_id, record_sha256, representation
    ):
        self.calls.append("output:" + output_id)
        item = output_item(output_id, OUTPUTS.index(output_id))
        report = {
            "title": f"Report {output_id}",
            "paragraphs": [f"Produced for {output_id}."],
            "facts": [],
            "tables": [],
            "sources": [],
        }
        return {
            "reference": {
                "instance_id": instance_id,
                "run_id": run_id,
                "output_id": output_id,
                "record_sha256": record_sha256,
            },
            "producer": item["producer"],
            "label": item["label"],
            "published_at": item["published_at"],
            "invocation": item["invocation"],
            "representation": {
                "name": representation,
                "format": "millrace-report-display/v1",
                "media_type": "application/json",
                "sha256": "e" * 64,
                "data": report,
            },
        }


@pytest.fixture
def fake_router() -> FakeRouter:
    return FakeRouter()


@pytest.fixture
def crowded_ids() -> tuple[str, str]:
    return INSTANCE, RUN


@pytest.fixture
def review_gate() -> dict[str, str]:
    return review_gate_files()


@pytest.fixture
def lad_topology() -> tuple[dict[str, str], tuple[str, ...]]:
    return lad_topology_files()
