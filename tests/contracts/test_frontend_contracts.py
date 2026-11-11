"""C1 complete-frontend contracts: one operator-selected local closure.

Manifest identity, path and file safety, compatibility, the owner-only
selection with its version check, desired/effective fallback, exact serving,
the package-owned launcher and the selected bundle's presentation default.
Everything runs in process against a real `Service` app; nothing is built.
"""

import fcntl
import hashlib
import json
import os
import stat
import threading
from importlib.resources import files as package_files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_auth import bootstrap

import millrace_os.frontend as frontend
from millrace_os import presentation as p
from millrace_os.api import DEFAULT_CSP, export_artifacts
from millrace_os.config import ApplicationConfig
from millrace_os.contracts import schema_artifacts
from millrace_os.credentials import CredentialStore
from millrace_os.router_client import RouterUnavailable
from millrace_os.service import Service

ORIGIN = "http://127.0.0.1:8731"
FRONTEND = "/api/v1/client/frontend"
PRESENTATION = "/api/v1/client/presentation"
INSTANCE, RUN = "a" * 32, "b" * 32
FORMAT = "example.board-layout/v1"
FILES = {
    "index.html": b'<!doctype html><script type="module" src="./assets/app.js">'
    b'</script><link rel="stylesheet" href="./assets/app.css"><h1>Board</h1>',
    "assets/app.js": b"export const board = 1;\n",
    "assets/app.css": b"h1{color:rgb(1,2,3)}\n",
}


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def key(manifest: dict) -> str:
    return sha(p.canonical_json_bytes(manifest))


def registry() -> dict[str, str]:
    return {entry.contract_id: entry.sha256 for entry in schema_artifacts()[1].schemas}


def write_bundle(root: Path, files=FILES, *, default=None, **changes) -> dict:
    """A complete local closure under `root`; returns its manifest.

    Modes are explicit, so the fixture is safe under any umask.
    """
    for name, content in files.items():
        target = root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o644)
        for folder in target.parents:
            folder.chmod(0o755)
            if folder == root:
                break
    manifest = {
        "format": frontend.MANIFEST_FORMAT,
        "frontend_id": "example.board",
        "frontend_version": "1.0.0",
        "entry": "index.html",
        "assets": [
            {
                "path": name,
                "sha256": sha(content),
                "media_type": frontend.MEDIA_TYPES["." + name.rsplit(".", 1)[1]],
            }
            for name, content in sorted(files.items())
        ],
        "api_version": "v1",
        "required_contracts": [
            {
                "contract_id": "FrontendState",
                "version": 1,
                "sha256": registry()["FrontendState"],
            }
        ],
        "required_capabilities": ["client.frontend.read"],
        "presentation_format": None if default is None else FORMAT,
        "default_presentation": default,
    } | changes
    (root / frontend.MANIFEST_NAME).write_text(json.dumps(manifest, indent=1))
    (root / frontend.MANIFEST_NAME).chmod(0o644)
    return manifest


def service(tmp_path):
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credentials.json",
    )
    config.ensure_directories()
    owned = Service(config)
    owned.trust.reset()
    owned._state = object()
    owned._lifecycle = "running"
    client = TestClient(owned.app, base_url=ORIGIN)
    bearer = {
        "Authorization": "Bearer " + CredentialStore(config.credential_path).ensure()
    }
    return owned, client, bearer


def local(path: Path, bundle_sha256: str) -> dict:
    return {"kind": "local", "path": str(path), "bundle_sha256": bundle_sha256}


def select(client, bearer, version, desired):
    body = {"expected_version": version, "desired": desired}
    return client.put(FRONTEND, headers=bearer, json=body)


def validate(client, bearer, path):
    return client.post(FRONTEND + "/validate", headers=bearer, json={"path": str(path)})


def refusal(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.text


def selected(tmp_path, *, default=None, files=FILES):
    """A service with one validated and selected local closure."""
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    manifest = write_bundle(root, files, default=default)
    bundle_key = validate(client, bearer, root).json()["bundle_sha256"]
    response = select(client, bearer, None, local(root, bundle_key))
    assert response.status_code == 200, response.text
    return owned, client, bearer, root, manifest, bundle_key


def selection_file(owned) -> Path:
    return Path(owned.config.config_dir) / frontend.SELECTION_NAME


def test_validation_reports_the_canonical_key_and_writes_nothing(tmp_path):
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    manifest = write_bundle(root)
    before = sorted(os.listdir(owned.config.config_dir))
    response = validate(client, bearer, root)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "contract": "millrace-os-frontend-validation/v1",
        "path": str(root),
        "bundle_sha256": key(manifest),
        "manifest": manifest,
    }
    # Canonical bytes: sorted keys, compact separators, UTF-8, no newline.
    assert (
        p.canonical_json_bytes(manifest)
        == json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    # Validation selects, copies and installs nothing.
    assert sorted(os.listdir(owned.config.config_dir)) == before
    state = client.get(FRONTEND, headers=bearer).json()
    assert (state["status"], state["version"], state["desired"]) == (
        "absent",
        None,
        {"kind": "stock"},
    )
    # A cosmetic re-encoding keeps the key; any changed value changes it.
    reordered = dict(reversed(list(manifest.items())))
    (root / frontend.MANIFEST_NAME).write_text(json.dumps(reordered, indent=4))
    assert validate(client, bearer, root).json()["bundle_sha256"] == key(manifest)
    changed = write_bundle(root, frontend_version="1.0.1")
    assert validate(client, bearer, root).json()["bundle_sha256"] == key(changed)
    assert key(changed) != key(manifest)
    # Files the manifest does not list are ignored, never part of the closure.
    (root / "notes.txt").write_text("not an asset")
    assert validate(client, bearer, root).json()["bundle_sha256"] == key(changed)


@pytest.mark.parametrize(
    "spelling, selection_status, selection_code",
    [
        ("relative/bundle", 422, "invalid_request"),
        ("{root}/", 422, "invalid_request"),
        ("{root}/.", 422, "invalid_request"),
        ("{parent}/./bundle", 422, "invalid_request"),
        ("{root}/../bundle", 422, "invalid_request"),
        ("/{root}", 422, "invalid_request"),
        ("{root}\n", 422, "invalid_request"),
        ("{root}\x00", 422, "invalid_request"),
        ("/" + "a" * 3968, 413, "request_too_large"),
    ],
)
def test_paths_must_be_absolute_and_spelled_one_way(
    tmp_path, spelling, selection_status, selection_code
):
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    write_bundle(root)
    path = spelling.format(root=root, parent=tmp_path)
    refusal(validate(client, bearer, path), 422, "invalid_request")
    desired = {"kind": "local", "path": path, "bundle_sha256": "c" * 64}
    refusal(select(client, bearer, None, desired), selection_status, selection_code)
    assert not selection_file(owned).exists()
    with pytest.raises(frontend.FrontendError):
        frontend.bundle_root(path)


def test_the_longest_admitted_path_fits_the_default_body_policy(tmp_path):
    _, client, bearer = service(tmp_path)
    longest = "/" + "a" * 3967
    assert len(json.dumps({"path": longest})) <= 4096
    # Admitted by shape, then simply absent on disk.
    refusal(validate(client, bearer, longest), 503, "frontend_unavailable")


def test_unsafe_missing_and_changed_files_refuse(tmp_path):
    _, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    write_bundle(root)
    link = tmp_path / "link"
    link.symlink_to(root)
    refusal(validate(client, bearer, link), 422, "unsafe_path")
    script = root / "assets" / "app.js"
    moved = tmp_path / "app.js"
    script.rename(moved)
    script.symlink_to(moved)  # identical bytes behind a link
    refusal(validate(client, bearer, root), 422, "unsafe_path")
    script.unlink()
    moved.rename(script)
    assert validate(client, bearer, root).status_code == 200
    for target, mode in ((script, 0o664), (root / "assets", 0o775)):
        original = target.stat().st_mode & 0o777
        target.chmod(mode)
        refusal(validate(client, bearer, root), 422, "unsafe_path")
        target.chmod(original)
    script.write_bytes(b"export const board = 2;\n")
    refusal(validate(client, bearer, root), 409, "frontend_conflict")
    script.unlink()
    refusal(validate(client, bearer, root), 503, "frontend_unavailable")
    refusal(validate(client, bearer, tmp_path / "absent"), 503, "frontend_unavailable")


def _mutations():
    svg = {"path": "logo.svg", "sha256": "0" * 64, "media_type": "image/svg+xml"}
    return [
        {"frontend_id": "stock"},
        {"format": "millrace-os-frontend-bundle/v2"},
        {"api_version": "v2"},
        {"entry": "main.html"},
        {"extra": True},
        {
            "required_contracts": [
                {"contract_id": "FrontendState", "version": 1, "sha256": "0" * 64}
            ]
        },
        {
            "required_contracts": [
                {"contract_id": "NoSuchContract", "version": 1, "sha256": "0" * 64}
            ]
        },
        {"required_capabilities": ["client.frontend.nonexistent"]},
        {"presentation_format": FORMAT},
        {"assets": [svg]},
    ]


@pytest.mark.parametrize("change", _mutations())
def test_manifests_and_requirements_must_match_positively(tmp_path, change):
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    write_bundle(root, **change)
    refusal(validate(client, bearer, root), 409, "frontend_incompatible")
    raw = (root / frontend.MANIFEST_NAME).read_bytes()
    duplicate = raw.replace(b'"api_version"', b'"api_version": "v1", "api_version"', 1)
    (root / frontend.MANIFEST_NAME).write_bytes(duplicate)
    refusal(validate(client, bearer, root), 409, "frontend_incompatible")


def test_required_capabilities_must_be_available_now(tmp_path, monkeypatch):
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    write_bundle(root, required_capabilities=["router.views.run"])
    monkeypatch.setattr(owned.router_client, "_inspection", lambda: None)
    assert validate(client, bearer, root).status_code == 200

    def unavailable():
        raise RouterUnavailable("router_inspection_unavailable")

    monkeypatch.setattr(owned.router_client, "_inspection", unavailable)
    refusal(validate(client, bearer, root), 409, "frontend_incompatible")


def test_selection_is_owner_only_and_compares_its_version(tmp_path):
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    manifest = write_bundle(root)
    bundle_key = key(manifest)
    response = select(client, bearer, None, local(root, bundle_key))
    assert response.status_code == 200, response.text
    state = response.json()
    version = state["version"]
    assert state["status"] == "ready" and len(version) == 32
    assert state["desired"] == local(root, bundle_key)
    assert state["effective"] == {
        "kind": "local",
        "base": f"/ui/local/{bundle_key}/",
        "bundle_sha256": bundle_key,
    }
    assert state["manifest"] == {
        "frontend_id": "example.board",
        "frontend_version": "1.0.0",
        "bundle_sha256": bundle_key,
    }
    assert state["reason"] is None
    record = selection_file(owned)
    assert record.stat().st_mode & 0o777 == 0o600
    assert record.read_bytes() == p.canonical_json_bytes(
        {
            "format": "millrace-os-frontend-selection/v1",
            "version": version,
            "desired": local(root, bundle_key),
        }
    )
    lock = Path(owned.config.config_dir) / frontend.SELECTION_LOCK
    assert lock.stat().st_mode & 0o777 == 0o600
    saved = record.read_bytes()
    # A stale or absent-expecting version refuses and writes nothing.
    for stale in (None, "c" * 32):
        refusal(
            select(client, bearer, stale, {"kind": "stock"}), 409, "frontend_conflict"
        )
        assert record.read_bytes() == saved
    # Reads never write.
    for _ in range(2):
        assert client.get(FRONTEND, headers=bearer).json()["version"] == version
        client.get("/", follow_redirects=False)
        client.get(f"/ui/local/{bundle_key}/")
    assert record.read_bytes() == saved
    assert not [
        name
        for name in os.listdir(owned.config.config_dir)
        if name.startswith(".frontend-")
    ]
    # Reset is the stock selection with the same version check.
    reset = select(client, bearer, version, {"kind": "stock"})
    assert reset.status_code == 200, reset.text
    after = reset.json()
    assert after["desired"] == {"kind": "stock"} and after["version"] != version
    assert after["effective"]["base"] == "/ui/stock/"


def test_select_revalidates_the_exact_bytes_before_committing(tmp_path):
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    manifest = write_bundle(root)
    bundle_key = key(manifest)
    (root / "assets" / "app.js").write_bytes(b"export const board = 2;\n")
    refusal(
        select(client, bearer, None, local(root, bundle_key)), 409, "frontend_conflict"
    )
    write_bundle(root, frontend_version="2.0.0")
    refusal(
        select(client, bearer, None, local(root, bundle_key)), 409, "frontend_conflict"
    )
    write_bundle(root, required_capabilities=["client.frontend.nonexistent"])
    refusal(
        select(client, bearer, None, local(root, bundle_key)),
        409,
        "frontend_incompatible",
    )
    assert not selection_file(owned).exists()


def test_a_concurrent_selection_refuses_instead_of_waiting(tmp_path):
    owned, client, bearer = service(tmp_path)
    lock = Path(owned.config.config_dir) / frontend.SELECTION_LOCK
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        refusal(
            select(client, bearer, None, {"kind": "stock"}), 409, "frontend_conflict"
        )
    finally:
        os.close(descriptor)
    assert not selection_file(owned).exists()
    assert select(client, bearer, None, {"kind": "stock"}).status_code == 200


def test_unreadable_and_unsafe_selections_use_stock_and_are_never_rewritten(
    tmp_path,
):
    owned, client, bearer = service(tmp_path)
    record = selection_file(owned)
    record.write_bytes(b'{"format": "millrace-os-frontend-selection/v9"}')
    record.chmod(0o600)
    state = client.get(FRONTEND, headers=bearer).json()
    assert (state["status"], state["version"], state["desired"]) == (
        "invalid",
        None,
        None,
    )
    assert state["effective"]["base"] == "/ui/stock/"
    assert state["reason"] == "frontend_selection_invalid"
    refusal(select(client, bearer, None, {"kind": "stock"}), 409, "frontend_conflict")
    assert record.read_bytes() == b'{"format": "millrace-os-frontend-selection/v9"}'
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui/stock/"
    record.chmod(0o644)  # readable by others: never trusted
    refusal(client.get(FRONTEND, headers=bearer), 422, "unsafe_path")
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui/stock/"
    assert client.get("/client").status_code == 200


def test_only_the_effective_closure_is_served_exactly(tmp_path):
    _, client, bearer, root, _, bundle_key = selected(tmp_path)
    base = f"/ui/local/{bundle_key}/"
    shell = client.get(base)
    assert shell.status_code == 200
    assert shell.content == FILES["index.html"]
    assert shell.headers["content-type"] == "text/html; charset=utf-8"
    assert shell.headers["content-security-policy"] == frontend.SHELL_CSP
    assert shell.headers["cache-control"] == "no-store"
    assert client.get(base + f"?home=main&instance={INSTANCE}").status_code == 200
    for name in ("assets/app.js", "assets/app.css"):
        response = client.get(base + name)
        assert response.status_code == 200, name
        assert response.content == FILES[name]
        assert (
            response.headers["content-type"]
            == frontend.MEDIA_TYPES["." + name.rsplit(".", 1)[1]]
        )
        assert response.headers["content-security-policy"] == DEFAULT_CSP
        assert response.headers["cache-control"] == "no-store"
    (root / "notes.txt").write_text("undeclared")
    for path in (
        "notes.txt",
        "missing.js",
        frontend.MANIFEST_NAME,
        "assets/",
        "Index.html",
        "%2e%2e/api/v1/version",
    ):
        response = client.get(base + path)
        assert response.status_code == 404, path
        assert response.headers["content-type"] == "application/json"
        assert response.json()["code"] == "route_not_found"
    for query in ("?home=main&run=" + RUN, "?home=main&home=main", "?x=1"):
        refusal(client.get(base + query), 422, "invalid_request")
    refusal(client.get(base + "assets/app.js?home=main"), 422, "invalid_request")
    # Any other digest: typed unavailability, never stock HTML.
    stock_entry = (
        package_files("millrace_os")
        .joinpath("assets", "dashboard", "index.html")
        .read_bytes()
    )
    for other in ("c" * 64, "not-a-digest"):
        page = client.get(f"/ui/local/{other}/")
        assert page.status_code == 503
        assert page.headers["content-type"].startswith("text/html")
        assert page.content != stock_entry and b"frontend_unavailable" in page.content
        for link in (b'href="/ui/stock/"', b'href="/client"', b'href="/recovery"'):
            assert link in page.content
        refusal(
            client.get(f"/ui/local/{other}/assets/app.js"), 503, "frontend_unavailable"
        )
    assert client.get("/ui/stock/").content == stock_entry
    assert client.get("/ui/stock", follow_redirects=False).headers["location"] == (
        "/ui/stock/"
    )
    # Shells and assets carry no operational data; the API still needs trust.
    assert client.get(FRONTEND).status_code == 401


@pytest.mark.parametrize(
    ("path", "suffix"),
    [
        ("/", ""),
        ("/?home=main", "?home=main"),
        (f"/instances/{INSTANCE}?home=main", f"?home=main&instance={INSTANCE}"),
        (
            f"/ui/stock/instances/{INSTANCE}/runs/{RUN}?home=main",
            f"?home=main&instance={INSTANCE}&run={RUN}",
        ),
        (
            "/ui/local/{key}/instances/" + INSTANCE + "?home=main",
            f"?home=main&instance={INSTANCE}",
        ),
    ],
)
def test_root_and_legacy_forms_redirect_to_the_effective_base(tmp_path, path, suffix):
    _, client, bearer, root, _, bundle_key = selected(tmp_path)
    response = client.get(path.format(key=bundle_key), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/local/{bundle_key}/" + suffix
    assert "#" not in response.headers["location"]
    (root / "assets" / "app.css").unlink()
    response = client.get(path.format(key=bundle_key), follow_redirects=False)
    assert response.headers["location"] == "/ui/stock/" + suffix


def test_failures_fall_back_to_stock_and_keep_the_desired_choice(tmp_path, monkeypatch):
    owned, client, bearer, root, _, bundle_key = selected(tmp_path)
    desired = local(root, bundle_key)
    base = f"/ui/local/{bundle_key}/"
    css = root / "assets" / "app.css"
    identity = {
        "frontend_id": "example.board",
        "frontend_version": "1.0.0",
        "bundle_sha256": bundle_key,
    }

    def state():
        return client.get(FRONTEND, headers=bearer).json()

    def effective_stock(reason, manifest):
        current = state()
        assert current["desired"] == desired
        assert current["effective"] == {
            "kind": "stock",
            "base": "/ui/stock/",
            "bundle_sha256": None,
        }
        assert (current["reason"], current["manifest"]) == (reason, manifest)
        assert client.get("/", follow_redirects=False).headers["location"] == (
            "/ui/stock/"
        )
        assert client.get(base).status_code == 503
        # An intact listed asset of a closure that is not effective refuses too.
        refusal(client.get(base + "assets/app.js"), 503, "frontend_unavailable")

    css.unlink()
    effective_stock("frontend_unavailable", identity)
    css.write_bytes(b"h1{color:red}\n")
    css.chmod(0o644)  # a new file: its mode must not depend on the umask
    effective_stock("frontend_conflict", identity)
    css.write_bytes(FILES["assets/app.css"])
    assert state()["effective"]["base"] == base  # effective again, unselected
    original = (root / frontend.MANIFEST_NAME).read_bytes()
    write_bundle(root, frontend_version="1.0.1")
    effective_stock("frontend_conflict", None)
    (root / frontend.MANIFEST_NAME).write_bytes(original)
    (root / "assets").chmod(0o777)
    effective_stock("unsafe_path", identity)
    (root / "assets").chmod(0o755)

    # A capability the selected bundle needs becomes unavailable: stock.
    def unavailable():
        raise RouterUnavailable("router_inspection_unavailable")

    monkeypatch.setattr(owned.router_client, "_inspection", lambda: None)
    needs = key(write_bundle(root, required_capabilities=["router.views.run"]))
    version = state()["version"]
    assert select(client, bearer, version, local(root, needs)).status_code == 200
    desired, base = local(root, needs), f"/ui/local/{needs}/"
    monkeypatch.setattr(owned.router_client, "_inspection", unavailable)
    current = state()
    assert (current["reason"], current["manifest"]) == ("frontend_incompatible", None)
    assert current["desired"] == desired and current["effective"]["kind"] == "stock"
    monkeypatch.setattr(owned.router_client, "_inspection", lambda: None)
    assert state()["effective"]["base"] == base
    # A restarted service rereads the same saved choice and revalidates it.
    again = Service(owned.config)
    again.trust.reset()
    again._state = object()
    again._lifecycle = "running"
    monkeypatch.setattr(again.router_client, "_inspection", lambda: None)
    restarted = TestClient(again.app, base_url=ORIGIN)
    assert restarted.get(FRONTEND, headers=bearer).json()["effective"]["base"] == base


def test_the_launcher_and_recovery_never_depend_on_the_selection(tmp_path):
    owned, client, bearer, root, _, bundle_key = selected(tmp_path)
    for broken in (False, True):
        if broken:
            (root / "assets" / "app.js").write_bytes(b"throw new Error('broken');\n")
        launcher = client.get("/client")
        assert launcher.status_code == 200
        assert (
            launcher.text
            == package_files("millrace_os")
            .joinpath("assets", "client.html")
            .read_text()
        )
        assert launcher.headers["content-security-policy"] == DEFAULT_CSP
        assert launcher.headers["cache-control"] == "no-store"
        for link in ('href="/ui/stock/"', 'href="/recovery"'):
            assert link in launcher.text
        assert "/ui/local/" not in launcher.text
        script = client.get("/client/client.js")
        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
        assert (
            script.content
            == package_files("millrace_os").joinpath("assets", "client.js").read_bytes()
        )
        assert client.get("/recovery").status_code == 200
        assert client.get("/recovery/bootstrap.js").status_code == 200
        assert client.get("/ui/stock/").status_code == 200
    refusal(client.get("/client?home=main"), 422, "invalid_request")


def test_frontend_routes_need_trust_and_a_session_write_needs_csrf(tmp_path):
    _, client, bearer = service(tmp_path)
    body = {"expected_version": None, "desired": {"kind": "stock"}}
    assert client.get(FRONTEND).status_code == 401
    assert client.post(FRONTEND + "/validate", json={"path": "/x"}).status_code == 401
    assert client.put(FRONTEND, json=body).status_code == 401
    _, csrf = bootstrap(client, bearer)
    assert client.get(FRONTEND).status_code == 200  # the session cookie
    refusal(
        client.put(FRONTEND, json=body, headers={"Origin": ORIGIN}), 403, "invalid_csrf"
    )
    written = client.put(
        FRONTEND, json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
    )
    assert written.status_code == 200, written.text
    for extra in (
        {"desired": {"kind": "stock", "path": "/x"}},
        {"expected_version": "C" * 32},
        {"unknown": 1},
    ):
        refusal(
            client.put(FRONTEND, headers=bearer, json=body | extra),
            422,
            "invalid_request",
        )
    refusal(
        client.put(FRONTEND, headers=bearer, json={"desired": {"kind": "stock"}}),
        422,
        "invalid_request",
    )


def test_exactly_four_registered_documents_and_three_capabilities(tmp_path):
    owned, client, bearer = service(tmp_path)
    exported = export_artifacts(owned.app)
    names = {
        entry["contract_id"]
        for entry in json.loads(exported["registry.json"])["schemas"]
    }
    new = {
        "FrontendState",
        "FrontendValidation",
        "FrontendValidateInput",
        "FrontendWrite",
    }
    assert new <= names and len(names) == 66
    assert not any(name.startswith("Frontend") for name in names - new)
    paths = json.loads(exported["openapi.json"])["paths"]
    for method, route in (
        ("get", FRONTEND),
        ("post", FRONTEND + "/validate"),
        ("put", FRONTEND),
    ):
        assert method in paths[route], (method, route)
    for route in (FRONTEND + "/validate", FRONTEND):
        operation = paths[route]["post" if "validate" in route else "put"]
        assert operation["requestBody"]["x-max-body-bytes"] == 4096
    for route in ("/client", "/ui/local/{bundle_sha256}/"):
        assert route not in paths
    body = client.get("/api/v1/client/capabilities", headers=bearer).json()
    listed = {item["operation_id"]: item for item in body["operations"]}
    for operation_id, method, request_contract, response_contract in (
        ("client.frontend.read", "GET", None, "FrontendState"),
        (
            "client.frontend.validate",
            "POST",
            "FrontendValidateInput",
            "FrontendValidation",
        ),
        ("client.frontend.write", "PUT", "FrontendWrite", "FrontendState"),
    ):
        item = listed[operation_id]
        assert (
            item["method"],
            item["request_contract"],
            item["response_contract"],
            item["available"],
        ) == (method, request_contract, response_contract, True)


def presentation_store(client, bearer):
    return client.get(PRESENTATION, headers=bearer).json()


def put_write(client, bearer, write):
    response = client.put(PRESENTATION, headers=bearer, json=write)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_selected_bundle_default_is_its_namespace_reset_base_only(tmp_path):
    default = {"columns": 3, "nested": {"keep": [1.5, None, "text"]}}
    _, client, bearer, root, _, bundle_key = selected(tmp_path, default=default)
    namespace = {"frontend_id": "example.board", "presentation_format": FORMAT}
    state = presentation_store(client, bearer)
    stock = [item for item in state["bundled_configs"] if item["id"] != "default"]
    assert stock == p.bundled_configs()
    (custom,) = [item for item in state["bundled_configs"] if item["id"] == "default"]
    assert custom == {
        **namespace,
        "id": "default",
        "name": "Default",
        "payload": default,
        "payload_sha256": sha(p.canonical_json_bytes(default)),
        "bundle_sha256": bundle_key,
    }
    # One edited custom config and one stock config; reset touches only one.
    stock_ns = {"frontend_id": "stock", "presentation_format": p.STOCK_FORMAT}
    precision = p.bundled_configs()[0]["payload"]
    state = put_write(
        client, bearer, p.create(state, stock_ns, "Mine", precision | {"theme": "dark"})
    )
    state = put_write(
        client, bearer, p.create(state, namespace, "Edited", {"columns": 9})
    )
    edited = next(
        item
        for item in state["store"]["namespaces"]
        if item["frontend_id"] == "example.board"
    )["configs"][0]["id"]
    before_stock = [
        ns for ns in state["store"]["namespaces"] if ns["frontend_id"] == "stock"
    ]
    state = put_write(client, bearer, p.reset(state, namespace, edited))
    (board,) = [
        ns
        for ns in state["store"]["namespaces"]
        if ns["frontend_id"] == "example.board"
    ]
    assert board["configs"][0]["payload"] == default
    assert [
        ns for ns in state["store"]["namespaces"] if ns["frontend_id"] == "stock"
    ] == before_stock
    # Not effective: no default is listed and reset refuses without a write.
    (root / "assets" / "app.css").unlink()
    state = presentation_store(client, bearer)
    assert [item["id"] for item in state["bundled_configs"]] == [
        "precision",
        "command",
        "studio",
    ]
    store_file = Path(tmp_path / "config" / "presentation.json")
    saved = store_file.read_bytes()
    with pytest.raises(p.PresentationError) as refused:
        p.reset(state, namespace, edited)
    assert refused.value.code == "presentation_incompatible"
    assert store_file.read_bytes() == saved


def test_a_default_that_would_overflow_the_presentation_read_refuses(tmp_path):
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    manifest = write_bundle(root, default={"blob": "x" * p.STATE_LIMIT})
    refusal(
        select(client, bearer, None, local(root, key(manifest))),
        409,
        "frontend_incompatible",
    )
    assert not selection_file(owned).exists()
    smaller = write_bundle(root, default={"blob": "x" * 1024})
    response = select(client, bearer, None, local(root, key(smaller)))
    assert response.status_code == 200, response.text
    assert p.state_fits(owned.config.config_dir, None)


def test_a_held_store_write_and_a_selection_never_commit_an_oversized_pair(
    tmp_path, monkeypatch
):
    owned, client, bearer = service(tmp_path)
    root = tmp_path / "bundle"
    bundle_key = key(write_bundle(root, default={"blob": "x" * 600_000}))
    namespace = {"frontend_id": "example.board", "presentation_format": FORMAT}
    # Each half fits the 1 MiB presentation read alone; together they do not.
    large = p.create(
        presentation_store(client, bearer), namespace, "Large", {"blob": "y" * 600_000}
    )
    entered, release = threading.Event(), threading.Event()
    admit = p._admit

    def held(*args):
        entered.set()
        assert release.wait(30)
        return admit(*args)

    monkeypatch.setattr(p, "_admit", held)
    other = TestClient(owned.app, base_url=ORIGIN)
    replies = {}
    writer = threading.Thread(
        target=lambda: replies.update(
            store=other.put(PRESENTATION, headers=bearer, json=large)
        )
    )
    writer.start()
    try:
        # The store write resolved the (absent) default under its lock and now
        # holds it: the selection that would add a default cannot commit.
        assert entered.wait(30)
        refusal(
            select(client, bearer, None, local(root, bundle_key)),
            409,
            "frontend_conflict",
        )
    finally:
        release.set()
        writer.join(30)
    assert replies["store"].status_code == 200, replies["store"].text
    assert not selection_file(owned).exists()
    monkeypatch.setattr(p, "_admit", admit)
    # The committed pair is checked as a pair: this default no longer fits.
    refusal(
        select(client, bearer, None, local(root, bundle_key)),
        409,
        "frontend_incompatible",
    )
    assert client.get(PRESENTATION, headers=bearer).status_code == 200


def test_an_unconfirmed_directory_fsync_is_reported_uncertain(tmp_path, monkeypatch):
    _, client, bearer = service(tmp_path)
    original = os.fsync

    def fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed")
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    refusal(
        select(client, bearer, None, {"kind": "stock"}), 503, "frontend_write_uncertain"
    )
    monkeypatch.setattr(os, "fsync", original)
    # The replace happened and is not rolled back: inspect, then select again
    # with the version now saved. Nothing is replayed.
    state = client.get(FRONTEND, headers=bearer).json()
    assert (state["status"], state["desired"]) == ("ready", {"kind": "stock"})
    refusal(select(client, bearer, None, {"kind": "stock"}), 409, "frontend_conflict")
    again = select(client, bearer, state["version"], {"kind": "stock"})
    assert again.status_code == 200, again.text


def test_pure_shell_helpers_recognize_both_bases_only():
    digest = "d" * 64
    assert frontend.is_shell("/ui/stock/") and frontend.is_shell(f"/ui/local/{digest}/")
    for path in (
        "/ui/stock",
        f"/ui/local/{digest}",
        f"/ui/local/{digest}/a.js",
        "/ui/local/D" + "d" * 63 + "/",
        "/client",
        "/",
    ):
        assert not frontend.is_shell(path), path
    assert frontend.query_keys(f"/ui/local/{digest}/") == frozenset(
        frontend.TARGET_KEYS
    )
    assert frontend.legacy_target(f"/ui/local/{digest}/instances/{INSTANCE}") == (
        INSTANCE,
        None,
    )
    assert frontend.legacy_target("/ui/local/short/instances/" + INSTANCE) is None
    assert frontend.shell_location({"home": "main"}, f"/ui/local/{digest}/") == (
        f"/ui/local/{digest}/?home=main"
    )
