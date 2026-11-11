"""Headless parity reads (M1): capabilities, local calls, publication status,
draft manifest/file reads and their CLI peers. Each reads existing OS facts."""

import base64
import hashlib
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse

import pytest
from test_auth import setup

from millrace_os.api import export_artifacts
from millrace_os.contracts import schema_artifacts
from millrace_os.paths import ensure_directory
from millrace_os.router_client import RouterUnavailable
from millrace_os.router_workers import RouterWorkers

INSTANCE, RUN, DRAFT, FROZEN, REQUEST, OTHER = (c * 32 for c in "abcdef")
# OS-G0 7.1 plus the three C1 frontend operations, spelled out independently
# of the implementation's table.
EXPECTED_OPERATIONS = [
    "actions.invoke",
    "actions.list",
    "client.capabilities.read",
    "client.frontend.read",
    "client.frontend.validate",
    "client.frontend.write",
    "client.presentation.read",
    "client.presentation.write",
    "contracts.read",
    "contracts.schema.read",
    "diagnostics.read",
    "events.availability",
    "health.read",
    "identity.read",
    "providers.list",
    "router.catalog.copy",
    "router.catalog.init",
    "router.catalog.list",
    "router.draft.apply",
    "router.draft.create",
    "router.draft.manifest",
    "router.draft.put_file",
    "router.draft.read",
    "router.draft.read_file",
    "router.draft.replace_files",
    "router.draft.validate",
    "router.homes.list",
    "router.instance.read",
    "router.instances.read",
    "router.local_call.read",
    "router.outputs.list",
    "router.outputs.publish",
    "router.outputs.read",
    "router.publication.repair",
    "router.publication.status",
    "router.request.capabilities",
    "router.request.observe",
    "router.request.submit",
    "router.run.read",
    "router.validate",
    "router.views.instance",
    "router.views.instances",
    "router.views.request",
    "router.views.revision",
    "router.views.run",
    "router.views.validate",
    "version.read",
]
PROBES = ("_control", "_inspection", "_views", "_catalog")
CAPABILITIES = "/api/v1/client/capabilities"
LOCAL = "/api/v1/router/local-calls/"
PUBLICATION = f"/api/v1/router/publications/{INSTANCE}"
NAN = float("nan")


def forbidden(*args, **kwargs):
    raise AssertionError("this path must not be reached")


def headless(tmp_path):
    service, client, bearer = setup(tmp_path)
    service.router_client.homes = {"default": tmp_path / "core"}
    return service, client, bearer


def refusal(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    return response.json()


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def ref(schema):
    return schema.get("$ref", "").rsplit("/", 1)[-1]


def test_capabilities_list_every_real_headless_operation(tmp_path, monkeypatch):
    service, client, bearer = headless(tmp_path)
    for name in PROBES:
        monkeypatch.setattr(service.router_client, name, lambda: None)
    for name in ("list_instances", "inspect_instance", "inspect_run", "catalog"):
        monkeypatch.setattr(service.router_client, name, forbidden)
    assert client.get(CAPABILITIES).status_code == 401
    refusal(client.get(CAPABILITIES + "?x=1", headers=bearer), 422, "invalid_request")
    response = client.get(CAPABILITIES, headers=bearer)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["contract"], body["api_version"]) == (
        "millrace-os-client-capabilities/v1",
        "v1",
    )
    operations = body["operations"]
    assert [item["operation_id"] for item in operations] == EXPECTED_OPERATIONS
    routes = {
        (method, route.path)
        for route in service.app.routes
        for method in (getattr(route, "methods", None) or ())
    }
    _, registry = schema_artifacts()
    registered = {entry.contract_id for entry in registry.schemas}
    document = json.loads(export_artifacts(service.app)["openapi.json"])
    for item in operations:
        assert (item["method"], item["path"]) in routes, item
        for key in ("request_contract", "response_contract"):
            assert item[key] is None or item[key] in registered, item
        invoke = item["operation_id"] == "actions.invoke"
        assert item["available"] is not invoke, item
        assert item["reason"] == ("action_unavailable" if invoke else None), item
        operation = document["paths"][item["path"]][item["method"].lower()]
        request = operation.get("requestBody", {}).get("content", {})
        if item["request_contract"] is None:
            assert not request, item
        elif "-" not in item["request_contract"]:
            schema = request["application/json"]["schema"]
            assert ref(schema) == item["request_contract"], item
        success = operation["responses"].get("200", {}).get("content", {})
        schema = success.get("application/json", {}).get("schema", {})
        contract = item["response_contract"]
        if "$ref" in schema and contract and "-" not in contract and not invoke:
            assert ref(schema) == contract, item
    # Only lazy import checks ran: no Core read, no state home created.
    assert not (tmp_path / "core").exists()


def test_capability_availability_reflects_lazy_import_checks_only(
    tmp_path, monkeypatch
):
    service, client, bearer = headless(tmp_path)

    def missing(code):
        def probe():
            raise RouterUnavailable(code)

        return probe

    client_ = service.router_client
    monkeypatch.setattr(client_, "_control", missing("router_unavailable"))
    monkeypatch.setattr(
        service.router_client,
        "_inspection",
        missing("router_inspection_unavailable"),
    )
    monkeypatch.setattr(service.router_client, "_views", lambda: None)
    monkeypatch.setattr(client_, "_catalog", missing("catalog_unavailable"))
    body = client.get(CAPABILITIES, headers=bearer).json()
    reasons = {item["operation_id"]: item["reason"] for item in body["operations"]}
    for item in body["operations"]:
        assert item["available"] is (item["reason"] is None)
    for operation in ("router.instances.read", "router.request.submit"):
        assert reasons[operation] == "router_unavailable"
    # Apply and repair need both control and catalog; control is reported first.
    assert reasons["router.draft.apply"] == "router_unavailable"
    assert reasons["router.publication.repair"] == "router_unavailable"
    assert reasons["router.catalog.list"] == "catalog_unavailable"
    assert reasons["router.views.run"] == "router_inspection_unavailable"
    assert reasons["router.outputs.list"] is None
    assert reasons["router.outputs.publish"] == "router_inspection_unavailable"
    for operation in (
        "client.presentation.write",
        "router.draft.create",
        "router.draft.manifest",
        "router.homes.list",
        "router.local_call.read",
        "router.publication.status",
        "router.request.capabilities",
    ):
        assert reasons[operation] is None, operation
    assert not (tmp_path / "core").exists()


def local_request(request_id, *, format="millrace-os-local-request/v1"):
    return {
        "format": format,
        "operation": "catalog_init",
        "instance_id": None,
        "run_id": None,
        "plane_id": None,
        "request_id": request_id,
        "expected_state_version": None,
        "payload": {},
    }


def finished(service, request_id, operation, **options):
    call = service.router_workers.submit(
        "default", local_request(request_id, **options), operation=operation
    )
    assert call.done.wait(5)
    for thread in (call.thread, call.watcher):
        thread.join(5)
    return call


def test_local_call_reports_only_retained_os_local_calls(tmp_path, monkeypatch):
    service, client, bearer = headless(tmp_path)
    monkeypatch.setattr(service.router_client, "_control", forbidden)

    def read(request_id):
        params = {"home": "default"}
        return client.get(LOCAL + request_id, params=params, headers=bearer)

    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(5)
        return {"copied": True}

    call = service.router_workers.submit(
        "default", local_request(REQUEST), operation=slow, bypass_guard=True
    )
    try:
        assert entered.wait(5)
        active = read(REQUEST)
        assert active.status_code == 202, active.text
        body = active.json()
        assert body["contract"] == "millrace-os-router-local-call/v1"
        assert (body["phase"], body["result"], body["error"]) == (
            "call_active",
            None,
            None,
        )
        assert body["operation"] == "catalog_init" and body["holder_joined"] is False
        assert body["request_digest"] == call.digest
    finally:
        release.set()
    returned = until(lambda: read(REQUEST).json()["holder_joined"] and read(REQUEST))
    assert returned.status_code == 200
    assert returned.json()["phase"] == "call_returned"
    assert returned.json()["result"] == {"copied": True}

    def refused():
        raise RouterUnavailable("catalog_destination_exists", 409)

    finished(service, OTHER, refused)
    failed = until(lambda: read(OTHER).json()["holder_joined"] and read(OTHER))
    assert failed.status_code == 200
    body = failed.json()
    assert (body["phase"], body["result"]) == ("call_failed", None)
    assert body["error"]["code"] == "catalog_destination_exists"
    # A Core-format call, an unknown ID or a bad ID is never a local result.
    finished(service, DRAFT, lambda: {}, format="millrace-router-request/v2")
    refusal(read(DRAFT), 404, "local_call_unavailable")
    refusal(read(RUN), 404, "local_call_unavailable")
    refusal(read("A" * 32), 422, "invalid_request")
    response = client.get(LOCAL + REQUEST, params={"home": "x"}, headers=bearer)
    refusal(response, 422, "unknown_router_home")
    # Retention: the latest 128 joined calls; older ones are unknown.
    for index in range(128):
        finished(service, f"{index:032x}", lambda: {})
    service.router_workers.snapshot()
    refusal(read(REQUEST), 404, "local_call_unavailable")
    assert read(f"{127:032x}").status_code == 200
    # 300 s after finishing (here zero), and after any restart: unknown.
    service.router_workers.observation_ttl = 0
    refusal(read(f"{127:032x}"), 404, "local_call_unavailable")
    finished(service, FROZEN, lambda: {})
    service.router_workers = RouterWorkers(service.router_client)
    refusal(read(FROZEN), 404, "local_call_unavailable")


def marker(**changes):
    record = {
        "format": "millrace-os-publication/v1",
        "home": "default",
        "instance_id": INSTANCE,
        "run_id": RUN,
        "base_state_version": 3,
        "draft_id": DRAFT,
        "frozen_token": FROZEN,
        "files_sha256": "1" * 64,
        "request_id": REQUEST,
        "receipts": [],
        "candidate_id": None,
        "run_state_version": None,
        "pause_request": None,
        "submit_request": None,
    }
    record.update(changes)
    return record


def plant(tmp_path, name, value):
    folder = ensure_directory(tmp_path / "core/os-client/publications" / INSTANCE)
    path = folder / name
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return path


def visible(record):
    return {k: v for k, v in record.items() if k != "frozen_token"}


def test_publication_status_reports_exact_markers_without_side_effects(
    tmp_path, monkeypatch
):
    service, client, bearer = headless(tmp_path)
    for name in ("acquire", "_publish", "repair", "apply"):
        monkeypatch.setattr(service.workflow_drafts, name, forbidden)
    monkeypatch.setattr(service.router_client, "_control", forbidden)

    def status(request_id=REQUEST):
        params = {"home": "default", "request_id": request_id}
        return client.get(PUBLICATION, params=params, headers=bearer)

    refusal(status(), 404, "publication_not_found")
    refusal(status("x"), 422, "invalid_request")
    progress = marker(pause_request=None)
    plant(tmp_path, "in-progress.json", progress)
    body = status().json()
    assert body["contract"] == "millrace-os-router-publication/v1"
    assert (body["in_progress"], body["completed"]) == (visible(progress), None)
    # The legal window while both markers exist: shared fields agree.
    completed = plant(tmp_path, "completed.json", marker(status="paused_pending"))
    both = status().json()
    assert both["in_progress"] == visible(progress)
    assert both["completed"] == visible(marker(status="paused_pending"))
    plant(tmp_path, "completed.json", marker(status="paused_pending", run_id=OTHER))
    refusal(status(), 409, "publication_evidence_invalid")
    # A later Apply replaced completed.json: the older request is unknown.
    (completed.parent / "in-progress.json").unlink()
    later = marker(
        request_id=OTHER,
        status="terminal_files_ready",
        terminal_candidate_id="candidate",
        terminal_evidence={"status": "cancelled", "state_version": 9},
    )
    plant(tmp_path, "completed.json", later)
    refusal(status(), 404, "publication_not_found")
    body = status(OTHER).json()
    assert (body["in_progress"], body["completed"]) == (None, visible(later))
    # Every present marker is strictly checked, even one for another request.
    for name, bad in (
        ("in-progress.json", b"{not json"),
        ("in-progress.json", marker(request_id=FROZEN, extra=1)),
        ("in-progress.json", marker(status="paused_pending")),
        ("completed.json", marker(request_id=OTHER, status="terminal_files_ready")),
        ("completed.json", marker(request_id=OTHER)),
        ("completed.json", marker(request_id=OTHER, home="other", status="x")),
        # Parsed strictly: a duplicate key or a nonfinite value is invalid.
        (
            "in-progress.json",
            json.dumps(marker(request_id=FROZEN))[:-1].encode()
            + b', "home": "default"}',
        ),
        (
            "in-progress.json",
            json.dumps(
                marker(request_id=FROZEN, receipts=[{"request_id": "x", "n": NAN}])
            ).encode(),
        ),
    ):
        path = plant(tmp_path, name, bad)
        raw = path.read_bytes()
        refusal(status(OTHER), 409, "publication_evidence_invalid")
        assert path.read_bytes() == raw
        if name == "in-progress.json":
            path.unlink()
        else:
            plant(tmp_path, name, later)
    assert not (completed.parent / ".lock").exists()


def test_repair_keeps_its_exact_marker_refusals(tmp_path):
    service, _, _ = headless(tmp_path)
    for bad, code, status in (
        (b"[]", "publication_evidence_invalid", 409),
        (marker(extra=1), "publication_evidence_invalid", 409),
        (marker(status="paused_pending"), "publication_evidence_invalid", 409),
        (marker(home="other"), "publication_evidence_invalid", 409),
        (marker(receipts=[{"request_id": 1}]), "publication_evidence_invalid", 409),
        (marker(draft_id="x"), "draft_not_found", 404),
    ):
        plant(tmp_path, "in-progress.json", bad)
        with pytest.raises(RouterUnavailable) as error:
            service.workflow_drafts.repair("default", INSTANCE)
        assert (error.value.code, error.value.status) == (code, status), bad


def entries(files):
    return [
        {"path": name, "content_base64": base64.b64encode(raw).decode()}
        for name, raw in files.items()
    ]


def test_draft_manifest_pages_bind_the_draft_and_file_reads_are_guarded(
    tmp_path, monkeypatch
):
    service, client, bearer = headless(tmp_path)
    monkeypatch.setattr(service.router_client, "_control", forbidden)
    files = {"a.txt": b"alpha", "b/c.json": b'{"x": 1}', "z.bin": bytes(range(256))}
    created = client.post(
        "/api/v1/router/drafts",
        headers=bearer,
        json={"home": "default", "files": entries(files)},
    )
    assert created.status_code == 200, created.text
    draft = created.json()
    base = "/api/v1/router/drafts/" + draft["draft_id"]

    def manifest(**params):
        params = {"home": "default", **params}
        return client.get(base + "/manifest", params=params, headers=bearer)

    def read(path):
        params = {"home": "default", "path": path}
        return client.get(base + "/file", params=params, headers=bearer)

    def item(name):
        raw = files[name]
        digest = hashlib.sha256(raw).hexdigest()
        return {"path": name, "sha256": digest, "size": len(raw)}

    first = manifest(limit=2).json()
    assert first["contract"] == "millrace-os-draft-manifest/v1"
    assert first["sha256"] == draft["sha256"]
    assert first["items"] == [item("a.txt"), item("b/c.json")]
    cursor = first["next_cursor"]
    assert cursor == "d" + draft["sha256"][:40] + "_2"
    second = manifest(cursor=cursor).json()
    assert (second["items"], second["next_cursor"]) == ([item("z.bin")], None)
    assert manifest().json()["items"] == [item(n) for n in sorted(files)]
    body = read("z.bin").json()
    assert body["contract"] == "millrace-os-draft-file/v1"
    assert base64.b64decode(body["content_base64"]) == files["z.bin"]
    assert (body["path"], body["sha256"], body["size"]) == (
        "z.bin",
        item("z.bin")["sha256"],
        256,
    )
    # A changed draft invalidates every older cursor.
    changed = client.put(
        base + "/file",
        headers=bearer,
        json={
            "home": "default",
            "path": "a.txt",
            "content_base64": base64.b64encode(b"changed").decode(),
        },
    )
    assert changed.status_code == 200
    refusal(manifest(cursor=cursor), 409, "draft_changed")
    refusal(manifest(cursor="x"), 422, "invalid_request")
    beyond = "d" + changed.json()["sha256"][:40] + "_9"
    refusal(manifest(cursor=beyond), 422, "invalid_request")
    response = client.get(
        base + "/manifest", params={"home": "default", "extra": "1"}, headers=bearer
    )
    refusal(response, 422, "invalid_request")
    refusal(read("missing.txt"), 404, "draft_file_not_found")
    refusal(read("../x"), 422, "invalid_draft_path")
    refusal(read("b/.millrace-router-owner"), 422, "invalid_draft_path")
    folder = service.config.state_dir / "drafts" / draft["draft_id"] / "files"
    large = folder / "large.bin"
    large.write_bytes(b"\0" * 3_000_000)
    os.chmod(large, 0o600)
    inode = large.stat().st_ino
    real_stat, real_read, reads = os.stat, os.read, []

    def stale(path, *args, **kwargs):
        # The file looks small when checked, but is large when read.
        info = real_stat(path, *args, **kwargs)
        if os.fspath(path).endswith("large.bin"):
            values = list(info)
            values[6] = 5  # st_size
            return os.stat_result(values)
        return info

    def recording(fd, size):
        chunk = real_read(fd, size)
        if os.fstat(fd).st_ino == inode:
            reads.append(len(chunk))
        return chunk

    monkeypatch.setattr(os, "stat", stale)
    monkeypatch.setattr(os, "read", recording)
    refusal(read("large.bin"), 409, "draft_file_too_large")
    monkeypatch.setattr(os, "stat", real_stat)
    monkeypatch.setattr(os, "read", real_read)
    # The read stopped one byte past the 786,432 bytes that fit the limit.
    assert sum(reads) == 786_433
    large.unlink()
    (folder / "link.txt").symlink_to(folder / "a.txt")
    refusal(read("link.txt"), 422, "unsafe_path")


def test_openapi_documents_parity_reads_and_the_apply_202(tmp_path):
    service, _, _ = setup(tmp_path)
    paths = json.loads(export_artifacts(service.app)["openapi.json"])["paths"]
    apply = paths["/api/v1/router/drafts/{draft_id}/apply"]["post"]["responses"]
    assert ref(apply["202"]["content"]["application/json"]["schema"]) == (
        "RouterLocalRead"
    )
    local = paths["/api/v1/router/local-calls/{request_id}"]["get"]["responses"]
    assert {"200", "202"} <= set(local)
    for path in (
        CAPABILITIES,
        "/api/v1/router/publications/{instance_id}",
        "/api/v1/router/drafts/{draft_id}/manifest",
        "/api/v1/router/drafts/{draft_id}/file",
    ):
        assert "get" in paths[path], path
    _, registry = schema_artifacts()
    names = {entry.contract_id for entry in registry.schemas}
    assert {
        "ClientCapabilities",
        "RouterLocalCall",
        "RouterPublication",
        "DraftManifest",
        "DraftFileRead",
    } <= names
    assert len(names) == 66  # D1 adds the fixed Finish input/acknowledgement


def run_cli(monkeypatch, tmp_path, *argv):
    from millrace_os.cli import main

    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            return Response()

    monkeypatch.setattr(
        "millrace_os.cli.urllib.request.build_opener", lambda *handlers: Opener()
    )
    paths = ["--config-dir", str(tmp_path / "config")]
    assert main([*argv, *paths, "--state-dir", str(tmp_path / "state")]) == 0
    return captured[-1]


def test_cli_parity_peers_select_exact_routes(tmp_path, monkeypatch):
    origin = "http://127.0.0.1:8731"
    router = origin + "/api/v1/router"
    for argv, method, url in (
        (
            ("router", "local-call", "--home", "h", "--request-id", "q"),
            "GET",
            router + "/local-calls/q?home=h",
        ),
        (
            (
                "router",
                "draft",
                "manifest",
                "--home",
                "h",
                "--draft",
                "d",
                "--limit",
                "2",
                "--cursor",
                "c_1",
            ),
            "GET",
            router + "/drafts/d/manifest?home=h&limit=2&cursor=c_1",
        ),
        (
            (
                "router",
                "draft",
                "read-file",
                "--home",
                "h",
                "--draft",
                "d",
                "--path",
                "b/c.json",
            ),
            "GET",
            router + "/drafts/d/file?home=h&path=b%2Fc.json",
        ),
        (
            (
                "router",
                "draft",
                "publication-status",
                "--home",
                "h",
                "--instance",
                "i",
                "--request-id",
                "q",
            ),
            "GET",
            router + "/publications/i?home=h&request_id=q",
        ),
        (
            ("client", "inspect", "--kind", "capabilities"),
            "GET",
            origin + "/api/v1/client/capabilities",
        ),
        (
            ("client", "inspect", "--kind", "observation", "--home", "h"),
            "OPTIONS",
            router + "/requests?home=h",
        ),
        (
            (
                "client",
                "inspect",
                "--kind",
                "schema",
                "--contract-id",
                "Error",
                "--digest",
                "f" * 64,
            ),
            "GET",
            origin + "/api/v1/contracts/Error/1/" + "f" * 64,
        ),
        (
            ("client", "inspect", "--kind", "availability"),
            "GET",
            origin + "/api/v1/events",
        ),
    ):
        request = run_cli(monkeypatch, tmp_path, *argv)
        assert (request.get_method(), request.full_url) == (method, url), argv
        assert request.data is None
    first, second = tmp_path / "first.txt", tmp_path / "second.bin"
    first.write_bytes(b"alpha")
    second.write_bytes(b"\x00\x01")
    request = run_cli(
        monkeypatch,
        tmp_path,
        "router",
        "draft",
        "replace-files",
        "--home",
        "h",
        "--draft",
        "d",
        "--entry",
        f"a.txt={first}",
        "--entry",
        f"b/c.bin={second}",
    )
    target = (request.get_method(), request.full_url)
    assert target == ("PUT", router + "/drafts/d/files")
    assert json.loads(request.data) == {
        "home": "h",
        "files": [
            {"path": "a.txt", "content_base64": "YWxwaGE="},
            {"path": "b/c.bin", "content_base64": "AAE="},
        ],
    }
    for version in ("v1", "v2"):
        request = run_cli(
            monkeypatch,
            tmp_path,
            "router",
            "request",
            "--home",
            "h",
            "--request-id",
            "q",
            "--observation",
            version,
        )
        media = f"application/vnd.millrace.router-call.{version}+json"
        assert request.get_header("Accept") == media
    request = run_cli(
        monkeypatch, tmp_path, "router", "request", "--home", "h", "--request-id", "q"
    )
    assert request.get_header("Accept") is None  # unchanged default
    for argv in (
        ("client", "inspect", "--kind", "schema"),
        ("client", "inspect", "--kind", "observation"),
        ("client", "inspect", "--kind", "health", "--home", "h"),
        ("client", "inspect", "--kind", "frontend"),
        (
            "router",
            "draft",
            "replace-files",
            "--home",
            "h",
            "--draft",
            "d",
            "--entry",
            "missing-separator",
        ),
    ):
        with pytest.raises(SystemExit) as exit_:
            run_cli(monkeypatch, tmp_path, *argv)
        assert exit_.value.code == 2, argv


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def wire_cli(monkeypatch, client):
    """Send the CLI's urllib requests to the in-process service; record them."""
    sent = []

    class Response:
        def __init__(self, response):
            self.status, self.body = response.status_code, response.content

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return self.body[:limit]

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            url = urllib.parse.urlsplit(request.full_url)
            response = client.request(
                request.get_method(),
                url.path + ("?" + url.query if url.query else ""),
                content=request.data,
                headers=dict(request.header_items()),
            )
            if response.status_code >= 400:
                raise urllib.error.HTTPError(
                    request.full_url,
                    response.status_code,
                    "refused",
                    None,
                    io.BytesIO(response.content),
                )
            return Response(response)

    monkeypatch.setattr(
        "millrace_os.cli.urllib.request.build_opener", lambda *handlers: Opener()
    )
    return sent


def test_replace_files_cli_sends_compact_utf8_within_the_body_policy(
    tmp_path, monkeypatch, capsys
):
    from millrace_os.cli import main

    service, client, bearer = headless(tmp_path)
    created = client.post(
        "/api/v1/router/drafts", headers=bearer, json={"home": "default", "files": []}
    )
    assert created.status_code == 200, created.text
    sent = wire_cli(monkeypatch, client)
    name = "é/ü.txt"
    shell = {"home": "default", "files": [{"path": name, "content_base64": ""}]}
    quads = (1_048_576 - len(compact(shell))) // 4
    source = tmp_path / "upload.bin"
    source.write_bytes(b"\0" * (3 * quads))
    argv = [
        "router",
        "draft",
        "replace-files",
        "--home",
        "default",
        "--draft",
        created.json()["draft_id"],
        "--entry",
        f"{name}={source}",
        "--config-dir",
        str(tmp_path / "config"),
        "--state-dir",
        str(tmp_path / "state"),
    ]
    assert main(argv) == 0
    (request,) = sent
    data = request.data
    # The exact API encoding: compact, with the path as direct UTF-8.
    assert data == compact(json.loads(data)) and name.encode() in data
    assert 1_048_576 - 4 < len(data) <= 1_048_576
    # The earlier spaced, ASCII-escaped form of this body would not fit.
    assert len(json.dumps(json.loads(data)).encode()) > 1_048_576
    body = json.loads(capsys.readouterr().out)
    assert body["contract"] == "millrace-os-draft/v1"
    assert [item["path"] for item in body["files"]] == [name]
