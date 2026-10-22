"""Command-only HTTP fixtures over the installed public Core/Plus controls."""

from __future__ import annotations

import base64
import json
import socket
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from millrace_os.config import ApplicationConfig
from millrace_os.credentials import CredentialStore
from millrace_os.service import Service, ServiceDependencies


def command_files(*, pause: bool = False, wait: bool = False) -> dict[str, str]:
    script = (
        "import json,time\nfrom pathlib import Path\n"
        + (
            "Path('started').write_text('ready')\n"
            "end=time.monotonic()+8\n"
            "while not Path('release').exists() and time.monotonic()<end:\n"
            " time.sleep(.01)\n"
            if wait
            else ""
        )
        + "Path('answer.txt').write_text('42\\n')\n"
        + "print(json.dumps({'outcome':'READY','refs':['answer.txt']}))\n"
    )
    review = (
        "from pathlib import Path\n"
        "assert Path('answer.txt').read_text()=='42\\n'\n"
        "Path('checked.txt').write_text('checked 42\\n')\n"
        'print(\'{"outcome":"ACCEPT","refs":["checked.txt"]}\')\n'
    )
    nodes = {}
    for name, file, outcome, action in (
        (
            "work",
            "work.py",
            "READY",
            {"pause": {"reason": "review", "resume": "review"}}
            if pause
            else {"goto": "review"},
        ),
        ("review", "review.py", "ACCEPT", {"finish": "succeeded"}),
    ):
        nodes[name] = {
            "invoke": {
                "binding": "python",
                "timeout_seconds": 10,
                "config": {"script": file},
            },
            "result": "json-v1",
            "outcomes": [outcome],
            "on": {outcome: action},
            "on_error": {"pause": {"reason": "execution_error", "resume": name}},
            "on_unknown": {"pause": {"reason": "unknown", "resume": name}},
        }
    graph = {
        "format": "millrace-router/v3",
        "limits": {
            "max_invocations": 8,
            "max_elapsed_seconds": 30,
            "max_parallel_invocations": 1,
        },
        "bindings": {
            "python": {
                "adapter": "command",
                "contract": "millrace-command/v1",
                "executable": sys.executable,
                "config": {},
            }
        },
        "control_files": ["work.py", "review.py"],
        "planes": {"main": {"workspace": ".", "start": "work", "nodes": nodes}},
    }
    return {"workflow.json": json.dumps(graph), "work.py": script, "review.py": review}


class RouterHarness:
    def __init__(self, root: Path, *, router_client=None, homes=None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.home = root / "core"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.config = ApplicationConfig(
            config_dir=root / "config",
            state_dir=root / "os-state",
            credential_path=root / "config/credentials.json",
            port=port,
            router_state_homes=homes or {"default": self.home},
        )
        self.dependencies = ServiceDependencies(router_client=router_client)
        self.projects = []
        self.start()

    def start(self):
        self.service = Service(self.config, dependencies=self.dependencies)
        self.service.start()
        self.http = httpx.Client(
            base_url=self.service.browser_endpoint,
            headers={
                "Authorization": "Bearer "
                + CredentialStore(self.config.credential_path).ensure()
            },
            timeout=3,
            trust_env=False,
        )

    def stop(self):
        for project in self.projects:
            (project / "release").write_text("test cleanup")
        self.service.stop()
        self.http.close()

    @staticmethod
    def entries(files):
        return [
            {"path": name, "content_base64": base64.b64encode(raw.encode()).decode()}
            for name, raw in files.items()
        ]

    def post(self, path, body):
        response = self.http.post("/api/v1/router/" + path, json=body)
        assert response.status_code in (200, 201, 202), response.text
        return response.json()

    def read(self, instance, run=None):
        path = f"instances/{instance}" + (f"/runs/{run}" if run else "")
        response = self.http.get("/api/v1/router/" + path, params={"home": "default"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["contract"] == "millrace-os-router-read/v1"
        assert (
            result["source_provider"] == "core"
            and result["authority"] == "core-state-home"
        )
        assert result["observed_at"] and result["home"] == "default"
        return result["data"]

    def observe(self, request_id, **targets):
        return self.http.get(
            "/api/v1/router/requests/" + request_id,
            params={"home": "default", **targets},
        )

    def until(self, predicate, *, timeout=5):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(0.01)
        raise AssertionError(f"condition did not become true: {last!r}")

    def send(self, request, *, wait=True):
        first = self.post("requests", {"home": "default", "request": request})
        assert first["contract"] == "millrace-os-router-call/v1"
        assert first["request_id"] == request["request_id"]
        if not wait:
            return first

        def final():
            response = self.observe(request["request_id"])
            assert response.status_code in (200, 202), response.text
            observation = response.json()
            receipt = observation["core_receipt"]
            return receipt if receipt and receipt["status"] != "queued" else None

        return self.until(final)

    def request(
        self,
        operation,
        instance=None,
        run=None,
        *,
        version=None,
        payload=None,
        plane=None,
        request_id=None,
    ):
        return {
            "format": "millrace-router-request/v2",
            "operation": operation,
            "instance_id": instance,
            "run_id": run,
            "plane_id": plane,
            "request_id": request_id or uuid4().hex,
            "expected_state_version": version,
            "payload": payload or {},
        }

    def flow(self, name="workflow", *, pause=False, wait=False):
        folder = self.root / name
        workflow, project = folder / "workflow", folder / "project"
        workflow.mkdir(parents=True)
        project.mkdir()
        self.projects.append(project)
        for name, content in command_files(pause=pause, wait=wait).items():
            (workflow / name).write_text(content)
        return workflow, project

    def create(self, workflow, project):
        request = self.request(
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
        receipt = self.send(request)
        assert receipt["status"] == "applied", receipt
        return receipt["instance_id"]

    def start_run(self, instance, *, mode="locked"):
        return self.send(
            self.request(
                "start_run",
                instance,
                version=self.read(instance)["state_version"],
                payload={"edit_mode": mode},
            )
        )

    def settled(self, instance, run, status):
        """Read stable quiescent state after this run's owned calls have joined."""

        def state():
            if any(
                not row["joined"]
                and row["observation"]["instance_id"] == instance
                and row["observation"]["run_id"] == run
                for row in self.service.router_workers.snapshot()
            ):
                return None
            value = self.read(instance, run)
            return value if value["status"] == status and value["quiescent"] else None

        return self.until(state)


@pytest.fixture
def router_harness(tmp_path):
    harness = RouterHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.stop()


@pytest.fixture
def router_factory(tmp_path):
    harnesses = []

    def make(name, **kwargs):
        value = RouterHarness(tmp_path / name, **kwargs)
        harnesses.append(value)
        return value

    yield make
    for harness in reversed(harnesses):
        harness.stop()


@pytest.fixture
def workflow_files():
    return command_files
