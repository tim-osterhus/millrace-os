"""UI2 stock shell, manifest closure, trust reload and demo-context contracts."""

import hashlib
import json
from importlib.resources import files as package_files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_auth import bootstrap

import millrace_os.frontend as frontend
from millrace_os.api import CLIENT_OPERATIONS, DEFAULT_CSP, export_artifacts
from millrace_os.auth import COOKIE
from millrace_os.config import ApplicationConfig
from millrace_os.contracts import schema_artifacts
from millrace_os.credentials import CredentialStore
from millrace_os.service import DemoContextPending, Service, ServiceDependencies

INSTANCE = "a" * 32
RUN = "b" * 32
ORIGIN = "http://127.0.0.1:8731"
BASE_INSTANCE = f"/ui/stock/?home=main&instance={INSTANCE}"
BASE_RUN = f"{BASE_INSTANCE}&run={RUN}"
CONTEXT_HEADERS = {"X-Millrace-Session-Context": "1", "Sec-Fetch-Site": "same-origin"}
STOCK_CONTRACTS = {
    "ClientCapabilities",
    "DemoContext",
    "Error",
    "ExchangeInput",
    "PresentationState",
    "PresentationWrite",
    "RouterHomes",
    "RouterView",
    "SessionContext",
    "SessionOutput",
    "WorkflowOutput",
    "WorkflowOutputs",
}
STOCK_CAPABILITIES = [
    "client.capabilities.read",
    "client.presentation.read",
    "client.presentation.write",
]


def _service(tmp_path, *, reader=None):
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credentials.json",
    )
    config.ensure_directories()
    dependencies = ServiceDependencies(demo_context_reader=reader)
    service = Service(config, dependencies=dependencies)
    service.trust.reset()
    service._state = object()
    service._lifecycle = "running"
    client = TestClient(service.app, base_url=ORIGIN)
    bearer = {
        "Authorization": "Bearer " + CredentialStore(config.credential_path).ensure()
    }
    return service, client, bearer


def _dashboard():
    return package_files("millrace_os").joinpath("assets", "dashboard")


def _manifest():
    return json.loads(_dashboard().joinpath(frontend.MANIFEST_NAME).read_bytes())


def _registry():
    return {entry.contract_id: entry.sha256 for entry in schema_artifacts()[1].schemas}


def _unconditional_operations():
    return [item[0] for item in CLIENT_OPERATIONS if item[5] == ()]


def _synthetic_closure(root: Path, *, mutate=None):
    """A minimal valid stock closure under `root/assets/dashboard` plus manifest."""
    directory = root / "assets" / "dashboard"
    (directory / "assets").mkdir(parents=True)
    files = {
        "index.html": b'<!doctype html><script type="module" '
        b'src="/ui/stock/assets/a.js"></script>',
        "assets/a.js": b"export {};\n",
        "assets/a.css": b"body{}\n",
    }
    media = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
    }
    for name, content in files.items():
        (directory / name).write_bytes(content)
    registry = _registry()
    manifest = {
        "format": frontend.MANIFEST_FORMAT,
        "frontend_id": "stock",
        "frontend_version": "0.23.1",
        "entry": "index.html",
        "assets": [
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "media_type": media["." + name.rsplit(".", 1)[1]],
            }
            for name, content in sorted(files.items())
        ],
        "api_version": "v1",
        "required_contracts": [
            {
                "contract_id": "RouterView",
                "version": 1,
                "sha256": registry["RouterView"],
            }
        ],
        "required_capabilities": ["client.presentation.read"],
        "presentation_format": None,
        "default_presentation": None,
    }
    if mutate is not None:
        mutate(manifest, directory)
    (directory / frontend.MANIFEST_NAME).write_text(json.dumps(manifest))
    return directory, files


def _use_root(monkeypatch, root: Path):
    monkeypatch.setattr(frontend, "files", lambda package: root)


def test_shell_serves_the_exact_packaged_entry_under_the_shell_policy(tmp_path):
    _, client, _ = _service(tmp_path)
    entry = _dashboard().joinpath("index.html").read_bytes()
    for query in (
        "",
        "?home=main",
        f"?home=main&instance={INSTANCE}",
        f"?home=main&instance={INSTANCE}&run={RUN}",
    ):
        response = client.get("/ui/stock/" + query)
        assert response.status_code == 200, query
        assert response.content == entry
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert response.headers["content-security-policy"] == frontend.SHELL_CSP
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "set-cookie" not in response.headers
    text = entry.decode()
    for marker in ("csrf", "nonce", "Bearer", "instance_id", "<style", " style="):
        assert marker not in text
    assert "<script>" not in text and "script-src" not in text


@pytest.mark.parametrize(
    "query",
    [
        "?unknown=1",
        "?home=main&home=other",
        "?home=-bad",
        f"?instance={INSTANCE}",
        f"?home=main&run={RUN}",
        "?home=main&instance=ABC",
        f"?home=main&instance={INSTANCE}&run={RUN.upper()}",
        f"?home=main&instance={INSTANCE}&run={RUN}&run={RUN}",
    ],
)
def test_shell_and_redirect_targets_refuse_invalid_queries(tmp_path, query):
    _, client, _ = _service(tmp_path)
    for path in ("/ui/stock/", "/", "/ui/stock"):
        response = client.get(path + query, follow_redirects=False)
        assert response.status_code == 422, (path, query)
        assert response.json()["code"] == "invalid_request"


def test_every_manifest_asset_is_served_with_exact_bytes_and_media_type(tmp_path):
    _, client, _ = _service(tmp_path)
    manifest = _manifest()
    for item in manifest["assets"]:
        response = client.get("/ui/stock/" + item["path"])
        expected = _dashboard().joinpath(*item["path"].split("/")).read_bytes()
        assert response.status_code == 200, item["path"]
        assert response.content == expected
        assert hashlib.sha256(response.content).hexdigest() == item["sha256"]
        assert response.headers["content-type"] == item["media_type"]
        assert response.headers["cache-control"] == "no-store"
        if item["path"] != "index.html":
            assert response.headers["content-security-policy"] == DEFAULT_CSP


def test_unlisted_paths_are_json_404_and_never_html(tmp_path):
    _, client, _ = _service(tmp_path)
    for path in (
        "/ui/stock/missing.js",
        "/ui/stock/" + frontend.MANIFEST_NAME,
        "/ui/stock/assets/",
        "/ui/stock/assets/missing.css",
        "/ui/stock/Index.html",
        "/ui/stock/%2e%2e/api/v1/version",
        "/ui/local/" + "c" * 64 + "/",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"] == "application/json"
        assert response.json()["code"] == "route_not_found"


def test_the_stock_manifest_is_a_complete_compatible_closure():
    manifest = _manifest()
    parsed, assets = frontend.validate_manifest(
        _dashboard().joinpath(frontend.MANIFEST_NAME).read_bytes(),
        registry=_registry(),
        available_operations=_unconditional_operations(),
        stock=True,
    )
    assert parsed == manifest and manifest["frontend_id"] == "stock"
    assert manifest["frontend_version"] == "0.23.1"
    assert {item["contract_id"] for item in manifest["required_contracts"]} == (
        STOCK_CONTRACTS
    )
    assert manifest["required_capabilities"] == STOCK_CAPABILITIES
    assert manifest["presentation_format"] is None
    assert {"index.html", "THIRD_PARTY_NOTICES.txt", "dependency-inventory.json"} <= (
        set(assets)
    )
    on_disk = set()
    stack = [("", _dashboard())]
    while stack:
        prefix, node = stack.pop()
        for child in node.iterdir():
            name = prefix + child.name
            if child.is_dir():
                stack.append((name + "/", child))
            else:
                on_disk.add(name)
    # Exactly the listed closure plus the (unserved) manifest: nothing else.
    assert on_disk == set(assets) | {frontend.MANIFEST_NAME}
    for path, asset in assets.items():
        content = _dashboard().joinpath(*path.split("/")).read_bytes()
        assert hashlib.sha256(content).hexdigest() == asset.sha256
        assert not path.endswith(".map")
        if asset.media_type.startswith(("text/javascript", "text/css")):
            assert b"sourceMappingURL" not in content


def test_changed_missing_or_invalid_closures_are_unavailable(tmp_path, monkeypatch):
    _, client, _ = _service(tmp_path)
    root = tmp_path / "package"
    directory, files = _synthetic_closure(root)
    _use_root(monkeypatch, root)
    assert client.get("/ui/stock/").content == files["index.html"]
    assert client.get("/ui/stock/assets/a.js").content == files["assets/a.js"]
    (directory / "assets" / "a.js").write_bytes(b"export const changed = 1;\n")
    response = client.get("/ui/stock/assets/a.js")
    assert response.status_code == 503
    assert response.json()["code"] == "frontend_unavailable"
    (directory / "assets" / "a.js").unlink()
    assert client.get("/ui/stock/assets/a.js").json()["code"] == "frontend_unavailable"
    (directory / "index.html").write_bytes(b"<!doctype html>changed")
    assert client.get("/ui/stock/").status_code == 503
    assert client.get("/recovery").status_code == 200


def test_symlinked_resources_are_refused_even_with_matching_bytes(
    tmp_path, monkeypatch
):
    _, client, _ = _service(tmp_path)
    root = tmp_path / "package"
    directory, files = _synthetic_closure(root)
    _use_root(monkeypatch, root)
    outside = tmp_path / "outside.js"
    outside.write_bytes(files["assets/a.js"])
    (directory / "assets" / "a.js").unlink()
    (directory / "assets" / "a.js").symlink_to(outside)
    response = client.get("/ui/stock/assets/a.js")
    assert (response.status_code, response.json()["code"]) == (
        503,
        "frontend_unavailable",
    )
    manifest = directory / frontend.MANIFEST_NAME
    moved = tmp_path / "manifest.json"
    manifest.rename(moved)
    manifest.symlink_to(moved)
    assert client.get("/ui/stock/").json()["code"] == "frontend_unavailable"
    assert client.get("/recovery").status_code == 200


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m, d: m.update(frontend_id="custom"),
        lambda m, d: m.update(format="millrace-os-frontend-bundle/v2"),
        lambda m, d: m.update(extra=True),
        lambda m, d: m["assets"].reverse(),
        lambda m, d: m["assets"].append(dict(m["assets"][0], path="assets/A.css")),
        lambda m, d: m["assets"][0].update(media_type="text/plain; charset=utf-8"),
        lambda m, d: m["assets"].append(
            {"path": "assets/x.svg", "sha256": "0" * 64, "media_type": "image/svg+xml"}
        ),
        lambda m, d: m["assets"].append(
            {"path": "instances/x.js", "sha256": "0" * 64,
             "media_type": "text/javascript; charset=utf-8"}
        ),
        lambda m, d: m["assets"].append(
            {"path": "assets/other.html", "sha256": "0" * 64,
             "media_type": "text/html; charset=utf-8"}
        ),
        lambda m, d: m["required_contracts"][0].update(sha256="0" * 64),
        # An unknown contract can never match, not even with a null digest.
        lambda m, d: m["required_contracts"].append(
            {"contract_id": "NotInstalled", "version": 1, "sha256": None}
        ),
        lambda m, d: m["required_contracts"].append(
            {"contract_id": "NotInstalled", "version": 1, "sha256": "0" * 64}
        ),
        lambda m, d: m["required_contracts"][0].update(version=2),
        lambda m, d: m.update(required_capabilities=["router.views.run"]),
        lambda m, d: m.update(presentation_format="x/v1"),
        lambda m, d: m.update(entry="main.html"),
    ],
)
def test_manifest_validation_refuses_incompatible_closures(
    tmp_path, monkeypatch, mutate
):
    _, client, _ = _service(tmp_path)
    root = tmp_path / "package"
    _synthetic_closure(root, mutate=mutate)
    _use_root(monkeypatch, root)
    response = client.get("/ui/stock/")
    assert response.status_code == 503
    assert response.json()["code"] == "frontend_unavailable"


def test_validate_manifest_reserves_stock_for_the_packaged_closure(tmp_path):
    directory, _ = _synthetic_closure(tmp_path)
    raw = (directory / frontend.MANIFEST_NAME).read_bytes()
    kwargs = dict(
        registry=_registry(), available_operations=_unconditional_operations()
    )
    frontend.validate_manifest(raw, stock=True, **kwargs)
    with pytest.raises(frontend.ManifestError):
        frontend.validate_manifest(raw, stock=False, **kwargs)
    duplicate = raw.replace(b'"api_version"', b'"api_version": "v1", "api_version"')
    with pytest.raises(frontend.ManifestError):
        frontend.validate_manifest(duplicate, stock=True, **kwargs)


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/", "/ui/stock/"),
        ("/?home=main", "/ui/stock/?home=main"),
        (f"/?home=main&instance={INSTANCE}&run={RUN}", BASE_RUN),
        ("/ui/stock", "/ui/stock/"),
        ("/ui/stock?home=main", "/ui/stock/?home=main"),
        (f"/instances/{INSTANCE}?home=main", BASE_INSTANCE),
        (f"/instances/{INSTANCE}/runs/{RUN}?home=main", BASE_RUN),
        (f"/ui/stock/instances/{INSTANCE}?home=main", BASE_INSTANCE),
        (f"/ui/stock/instances/{INSTANCE}/runs/{RUN}?home=main", BASE_RUN),
    ],
)
def test_redirects_keep_validated_targets_and_never_a_fragment(
    tmp_path, path, location
):
    _, client, _ = _service(tmp_path)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == location
    assert "#" not in response.headers["location"]


@pytest.mark.parametrize(
    "path",
    [
        f"/instances/{INSTANCE}",
        f"/instances/{INSTANCE}?home=main&instance={INSTANCE}",
        "/instances/not-an-id?home=main",
        f"/instances/{INSTANCE}/runs/short?home=main",
        f"/ui/stock/instances/{INSTANCE}?home=main&run={RUN}",
    ],
)
def test_legacy_redirects_refuse_invalid_targets(tmp_path, path):
    _, client, _ = _service(tmp_path)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_retired_bootstrap_route_and_unchanged_recovery_and_api_policy(tmp_path):
    _, client, bearer = _service(tmp_path)
    for retired in ("bootstrap.js", "bootstrap.html"):
        assert not package_files("millrace_os").joinpath("assets", retired).is_file()
    assert client.get("/assets/bootstrap.js").status_code == 401
    assert client.get("/assets/bootstrap.js", headers=bearer).status_code == 404
    shell = client.get("/recovery")
    recovery_html = package_files("millrace_os").joinpath("assets", "recovery.html")
    assert shell.text == recovery_html.read_text()
    assert shell.headers["content-security-policy"] == DEFAULT_CSP
    script = client.get("/recovery/bootstrap.js")
    recovery_js = package_files("millrace_os.recovery").joinpath("bootstrap.js")
    assert script.text == recovery_js.read_text()
    assert script.headers["content-security-policy"] == DEFAULT_CSP
    api = client.get("/api/v1/version", headers=bearer)
    assert api.headers["content-security-policy"] == DEFAULT_CSP


def _session(client, bearer):
    _, csrf = bootstrap(client, bearer)
    return csrf, client.cookies.get(COOKIE)


def test_session_context_restores_the_same_csrf_without_renewal(tmp_path):
    service, client, bearer = _service(tmp_path)
    csrf, sid = _session(client, bearer)
    before = service.trust.sessions[sid].expires
    response = client.get("/api/v1/auth/session-context", headers=CONTEXT_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "contract": "os.session-context.v1",
        "csrf": csrf,
        "expires_in": body["expires_in"],
    }
    assert type(body["expires_in"]) is int and 1 <= body["expires_in"] <= 900
    assert "set-cookie" not in response.headers
    assert service.trust.sessions[sid].expires == before
    assert sid not in response.text and service.trust.token not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_session_context_refuses_bearer_recovery_headers_and_queries(tmp_path):
    _, client, bearer = _service(tmp_path)
    _session(client, bearer)
    path = "/api/v1/auth/session-context"
    response = client.get(path, headers={**CONTEXT_HEADERS, **bearer})
    assert (response.status_code, response.json()["code"]) == (403, "session_required")
    for headers in (
        {},
        {"Sec-Fetch-Site": "same-origin"},
        {"X-Millrace-Session-Context": "1"},
        {"X-Millrace-Session-Context": "true", "Sec-Fetch-Site": "same-origin"},
        {"X-Millrace-Session-Context": "1", "Sec-Fetch-Site": "same-site"},
        {"X-Millrace-Session-Context": "1", "Sec-Fetch-Site": "none"},
        {"X-Millrace-Session-Context": "1", "Sec-Fetch-Site": "cross-site"},
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 403, headers
        assert response.json()["code"] == "session_context_forbidden"
    repeated = client.get(
        path,
        headers=[
            ("X-Millrace-Session-Context", "1"),
            ("X-Millrace-Session-Context", "1"),
            ("Sec-Fetch-Site", "same-origin"),
        ],
    )
    assert repeated.json()["code"] == "session_context_forbidden"
    assert client.get(path + "?csrf=1", headers=CONTEXT_HEADERS).status_code == 422
    wrong_origin = client.get(
        path, headers={**CONTEXT_HEADERS, "Origin": "http://evil.example"}
    )
    assert wrong_origin.status_code == 403
    wrong_host = client.get(path, headers={**CONTEXT_HEADERS, "Host": "evil.example"})
    assert wrong_host.status_code == 403
    client.cookies.clear()
    assert client.get(path, headers=CONTEXT_HEADERS).status_code == 401
    bootstrap(client, bearer, recovery=True)
    response = client.get(path, headers=CONTEXT_HEADERS)
    assert (response.status_code, response.json()["code"]) == (
        403,
        "recovery_session_only",
    )


def test_session_context_expiry_and_nonpositive_remaining_time_are_401(tmp_path):
    service, client, bearer = _service(tmp_path)
    now = [1000.0]
    service.trust.clock = lambda: now[0]
    _session(client, bearer)
    path = "/api/v1/auth/session-context"
    now[0] += 899.2
    response = client.get(path, headers=CONTEXT_HEADERS)
    assert response.json()["expires_in"] == 1
    now[0] += 1
    assert client.get(path, headers=CONTEXT_HEADERS).status_code == 401
    # The session is still valid during both authentication prunes, but the
    # handler's own remaining-time computation observes expiry: never clamp.
    _session(client, bearer)
    issued = now[0]
    readings = iter([issued + 10, issued + 10, issued + 900])
    service.trust.clock = lambda: next(readings)
    response = client.get(path, headers=CONTEXT_HEADERS)
    assert response.status_code == 401


def _raising(error):
    def reader():
        raise error

    return reader


def _reference():
    return {
        "reference": {
            "instance_id": INSTANCE,
            "run_id": RUN,
            "output_id": "report",
            "record_sha256": "c" * 64,
        },
        "report_sha256": "d" * 64,
        "json_sha256": None,
    }


def _context(**changes):
    value = {
        "format": "millrace-os-demo-context/v1",
        "home": "demo",
        "instance_id": INSTANCE,
        "run_id": RUN,
        "finish_available": False,
        "accepted_output_refs": [_reference()],
        "output_error": None,
    }
    value.update(changes)
    return value


def test_ordinary_service_has_only_the_typed_demo_context_sentinel(tmp_path):
    _, client, bearer = _service(tmp_path)
    assert client.get("/api/v1/demo/context").status_code == 401
    response = client.get("/api/v1/demo/context", headers=bearer)
    assert response.status_code == 404
    assert response.json()["contract"] == "os.error.v1"
    assert response.json()["code"] == "demo_context_unavailable"
    assert client.get("/api/v1/demo/context?x=1", headers=bearer).status_code == 422


@pytest.mark.parametrize(
    ("reader", "status", "code"),
    [
        (_raising(DemoContextPending()), 503, "demo_context_pending"),
        (_raising(RuntimeError("PRIVATE path")), 503, "demo_context_failed"),
        (lambda: _context(home="other"), 503, "demo_context_failed"),
        (lambda: _context(extra=1), 503, "demo_context_failed"),
        (lambda: _context(accepted_output_refs=[_reference()] * 40), 503,
         "demo_context_failed"),
        (lambda: "not a mapping", 503, "demo_context_failed"),
    ],
)
def test_demo_context_reader_failures_are_typed_and_sanitized(
    tmp_path, reader, status, code
):
    _, client, bearer = _service(tmp_path, reader=reader)
    response = client.get("/api/v1/demo/context", headers=bearer)
    assert (response.status_code, response.json()["code"]) == (status, code)
    assert "PRIVATE" not in response.text


def test_demo_context_reader_value_is_returned_strictly(tmp_path):
    _, client, bearer = _service(tmp_path, reader=lambda: _context())
    response = client.get("/api/v1/demo/context", headers=bearer)
    assert response.status_code == 200
    assert response.json() == _context()


def test_exported_contracts_document_session_context_and_match_every_profile(tmp_path):
    default, _, _ = _service(tmp_path / "default")
    owned, _, _ = _service(tmp_path / "owned", reader=lambda: _context())
    exported = export_artifacts(default.app)
    assert exported == export_artifacts(owned.app)
    for name, content in exported.items():
        assert package_files("millrace_os").joinpath("schemas", name).read_bytes() == (
            content
        )
    document = json.loads(exported["openapi.json"])
    operation = document["paths"]["/api/v1/auth/session-context"]["get"]
    assert operation["security"] == [{"Session": []}]
    headers = {item["name"]: item for item in operation["parameters"]}
    assert headers["X-Millrace-Session-Context"]["required"] is True
    assert headers["X-Millrace-Session-Context"]["schema"]["enum"] == ["1"]
    assert headers["Sec-Fetch-Site"]["schema"]["enum"] == ["same-origin"]
    assert "/api/v1/demo/context" in document["paths"]
    for path in ("/", "/ui/stock/", "/recovery", "/instances/{instance_id}"):
        assert path not in document["paths"]
    registry = json.loads(exported["registry.json"])["schemas"]
    names = {entry["contract_id"] for entry in registry}
    assert {"SessionContext", "DemoContext"} <= names
    assert len(registry) == 60 <= 64
