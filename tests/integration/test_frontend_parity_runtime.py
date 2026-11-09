"""C1 frontend choice through a real service: HTTP and the CLI agree.

The CLI talks to the running service over loopback HTTP exactly as an operator
would, and its answers equal the API's. Invalid CLI arguments refuse before
any request. No Core request is made and nothing is built or installed.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import httpx
import pytest

from millrace_os import frontend
from millrace_os import presentation as p
from millrace_os.cli import main
from millrace_os.config import ApplicationConfig
from millrace_os.contracts import schema_artifacts
from millrace_os.credentials import CredentialStore
from millrace_os.service import Service

FRONTEND = "/api/v1/client/frontend"
FORMAT = "example.board-layout/v1"
NAMESPACE = {"frontend_id": "example.board", "presentation_format": FORMAT}


def bundle(root: Path, *, default=None, version="1.0.0") -> str:
    """A complete local closure with explicit modes; returns its key."""
    files = {
        "index.html": b'<!doctype html><script type="module" src="./app.js"></script>',
        "app.js": b"export {};\n",
    }
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    for name, content in files.items():
        (root / name).write_bytes(content)
        (root / name).chmod(0o644)
    digests = {e.contract_id: e.sha256 for e in schema_artifacts()[1].schemas}
    manifest = {
        "format": frontend.MANIFEST_FORMAT,
        "frontend_id": "example.board",
        "frontend_version": version,
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
        "presentation_format": None if default is None else FORMAT,
        "default_presentation": default,
    }
    (root / frontend.MANIFEST_NAME).write_text(json.dumps(manifest))
    (root / frontend.MANIFEST_NAME).chmod(0o644)
    return hashlib.sha256(p.canonical_json_bytes(manifest)).hexdigest()


@pytest.fixture
def running(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credentials.json",
        port=port,
    )
    service = Service(config)
    service.start()
    api = httpx.Client(
        base_url=service.browser_endpoint.rstrip("/"),
        headers={
            "Authorization": "Bearer "
            + CredentialStore(config.credential_path).ensure()
        },
        timeout=5,
        trust_env=False,
    )
    try:
        yield service, api
    finally:
        api.close()
        service.stop()


def cli(service, capsys, *argv: str) -> tuple[int, object]:
    """Run one CLI command against the running service; parse its output."""
    config = service.config
    code = main(
        [
            *argv,
            "--config-dir", str(config.config_dir),
            "--state-dir", str(config.state_dir),
            "--port", str(config.port),
        ]
    )
    out = capsys.readouterr().out
    return code, json.loads(out)


def without_time(state: dict) -> dict:
    return {k: v for k, v in state.items() if k != "observed_at"}


def test_cli_and_http_report_the_same_choice_results_and_refusals(
    running, tmp_path, capsys, monkeypatch
):
    service, api = running

    def no_core(*_args, **_kwargs):
        raise AssertionError("a frontend choice never calls Core")

    monkeypatch.setattr(service.router_client, "_invoke", no_core)
    root = tmp_path / "bundle"
    bundle_key = bundle(root)
    code, state = cli(service, capsys, "frontend", "inspect")
    assert code == 0
    assert without_time(state) == without_time(api.get(FRONTEND).json())
    assert state["status"] == "absent" and state["version"] is None
    code, validated = cli(service, capsys, "frontend", "validate", "--path", str(root))
    assert code == 0
    direct = api.post(FRONTEND + "/validate", json={"path": str(root)})
    assert validated == direct.json()
    assert validated["bundle_sha256"] == bundle_key
    code, chosen = cli(
        service, capsys, "frontend", "select", "--path", str(root),
        "--bundle-sha256", bundle_key, "--expected-version", "null",
    )
    assert code == 0, chosen
    assert chosen["effective"]["base"] == f"/ui/local/{bundle_key}/"
    assert without_time(chosen) == without_time(api.get(FRONTEND).json())
    version = chosen["version"]
    # The same stale write refuses identically through both surfaces.
    code, refused = cli(
        service, capsys, "frontend", "select", "--path", str(root),
        "--bundle-sha256", bundle_key, "--expected-version", "null",
    )
    body = {
        "expected_version": None,
        "desired": {"kind": "local", "path": str(root), "bundle_sha256": bundle_key},
    }
    direct = api.put(FRONTEND, json=body)
    assert code == 1 and direct.status_code == 409
    assert refused == direct.json() and refused["code"] == "frontend_conflict"
    assert api.get(FRONTEND).json()["version"] == version
    missing = str(tmp_path / "absent")
    code, refused = cli(service, capsys, "frontend", "validate", "--path", missing)
    direct = api.post(FRONTEND + "/validate", json={"path": missing})
    assert code == 1 and direct.status_code == 503
    assert refused == direct.json() and refused["code"] == "frontend_unavailable"
    # Reset needs no working local client: the bundle is broken first.
    (root / "app.js").write_bytes(b"throw new Error('broken');\n")
    code, reset = cli(
        service, capsys, "frontend", "reset", "--expected-version", version
    )
    assert code == 0, reset
    assert reset["desired"] == {"kind": "stock"} and reset["version"] != version
    assert without_time(reset) == without_time(api.get(FRONTEND).json())


def test_open_resolves_the_exact_effective_base_before_the_nonce(
    running, tmp_path, monkeypatch, capsys
):
    service, api = running
    opened: list[str] = []
    monkeypatch.setattr(
        "millrace_os.cli.webbrowser.open", lambda url: opened.append(url) or True
    )
    endpoint = service.browser_endpoint.rstrip("/")
    root = tmp_path / "bundle"
    bundle_key = bundle(root)
    body = {
        "expected_version": None,
        "desired": {"kind": "local", "path": str(root), "bundle_sha256": bundle_key},
    }
    version = api.put(FRONTEND, json=body).json()["version"]
    base = f"/ui/local/{bundle_key}/"
    for extra, path in (
        ((), base),
        (("--home", "default"), base + "?home=default"),
        (("--stock", "--home", "default"), "/ui/stock/?home=default"),
        (("--client",), "/client"),
    ):
        opened.clear()
        code, result = cli(service, capsys, "open", *extra)
        assert (code, result) == (0, {"status": "browser_opened"})
        (url,) = opened
        location, fragment = url.split("#")
        assert location == endpoint + path
        assert len(fragment) >= 43  # the one-use nonce only in the fragment
    reset = {"expected_version": version, "desired": {"kind": "stock"}}
    assert api.put(FRONTEND, json=reset).status_code == 200
    opened.clear()
    assert cli(service, capsys, "open")[0] == 0
    assert opened[0].split("#")[0] == endpoint + "/ui/stock/"


@pytest.mark.parametrize(
    "argv",
    [
        ("frontend", "validate", "--path", "relative/bundle"),
        ("frontend", "validate", "--path", "/tmp/bundle/"),
        ("frontend", "select", "--path", "/tmp/a/../b", "--bundle-sha256", "c" * 64,
         "--expected-version", "null"),
        ("frontend", "select", "--path", "/tmp/bundle", "--bundle-sha256", "short",
         "--expected-version", "null"),
        ("frontend", "select", "--path", "/tmp/bundle", "--bundle-sha256", "c" * 64,
         "--expected-version", "NULL"),
        ("frontend", "select", "--path", "/tmp/bundle", "--bundle-sha256", "c" * 64),
        ("frontend", "reset"),
        ("frontend", "reset", "--expected-version", "C" * 32),
    ],
)
def test_invalid_frontend_arguments_refuse_before_any_request(
    tmp_path, monkeypatch, argv
):
    def contacted(*_handlers):
        raise AssertionError("invalid arguments must not contact the service")

    monkeypatch.setattr("millrace_os.cli.urllib.request.build_opener", contacted)
    with pytest.raises(SystemExit) as exit_:
        main([*argv, "--config-dir", str(tmp_path / "config"),
              "--state-dir", str(tmp_path / "state")])
    assert exit_.value.code == 2


@pytest.mark.parametrize(
    "extra",
    [
        ("--stock", "--client"),
        ("--recovery", "--stock"),
        ("--recovery", "--client"),
        ("--client", "--home", "default"),
        ("--stock", "--home", "default", "--run", "a" * 32),
    ],
)
def test_invalid_open_choices_refuse_before_any_request(
    tmp_path, monkeypatch, capsys, extra
):
    def contacted(*_handlers):
        raise AssertionError("invalid arguments must not contact the service")

    monkeypatch.setattr("millrace_os.cli.urllib.request.build_opener", contacted)
    monkeypatch.setattr("millrace_os.cli.webbrowser.open", contacted)
    code = main(["open", *extra, "--config-dir", str(tmp_path / "config"),
                 "--state-dir", str(tmp_path / "state")])
    assert code == 2
    assert json.loads(capsys.readouterr().out) == {"error": "invalid_target"}


def test_presentation_reset_uses_only_the_effective_bundle_default(
    running, tmp_path, capsys
):
    service, api = running
    default = {"columns": 3, "labels": ["a", "é"]}
    root = tmp_path / "bundle"
    bundle_key = bundle(root, default=default)
    body = {
        "expected_version": None,
        "desired": {"kind": "local", "path": str(root), "bundle_sha256": bundle_key},
    }
    version = api.put(FRONTEND, json=body).json()["version"]
    namespace = ("--frontend", "example.board", "--format", FORMAT)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"columns": 9}))
    code, state = cli(service, capsys, "presentation", "create", *namespace,
                      "--name", "Edited", "--payload-file", str(payload))
    assert code == 0, state
    (board,) = state["store"]["namespaces"]
    config_id = board["configs"][0]["id"]
    code, state = cli(service, capsys, "presentation", "reset", *namespace,
                      "--config", config_id)
    assert code == 0, state
    assert state["store"]["namespaces"][0]["configs"][0]["payload"] == default
    defaults = [item for item in state["bundled_configs"] if item["id"] == "default"]
    assert [item["bundle_sha256"] for item in defaults] == [bundle_key]
    # Back to stock: the default is gone and reset refuses without writing.
    reset = {"expected_version": version, "desired": {"kind": "stock"}}
    assert api.put(FRONTEND, json=reset).status_code == 200
    store = Path(service.config.config_dir) / "presentation.json"
    saved = store.read_bytes()
    code, refused = cli(service, capsys, "presentation", "reset", *namespace,
                        "--config", config_id)
    assert (code, refused) == (1, {"error": "presentation_incompatible"})
    assert store.read_bytes() == saved
    listed = api.get("/api/v1/client/presentation").json()["bundled_configs"]
    assert [item["id"] for item in listed] == ["precision", "command", "studio"]
