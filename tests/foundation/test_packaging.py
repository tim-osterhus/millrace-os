from __future__ import annotations

import importlib.util
import re
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest


def test_migration_is_package_data_for_wheel_install() -> None:
    migration = files("millrace_os.storage").joinpath("migrations/001_initial.sql")

    assert migration.is_file()
    assert "millrace_os_schema" in migration.read_text()


def test_project_declares_reproducible_development_checks() -> None:
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())[
        "project"
    ]

    assert set(project["optional-dependencies"]["dev"]) == {
        "pytest==9.0.2",
        "ruff==0.14.14",
        "httpx==0.28.1",
    }


def test_project_pins_the_p01_runtime_stack() -> None:
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())[
        "project"
    ]
    assert set(project["dependencies"]) == {
        "fastapi==0.116.1",
        "pydantic==2.11.9",
        "uvicorn==0.35.0",
    }


def test_release_identity_optional_router_and_generated_contracts(tmp_path) -> None:
    import json

    from millrace_os import __version__
    from millrace_os.api import export_artifacts
    from millrace_os.config import ApplicationConfig
    from millrace_os.contracts import Version
    from millrace_os.service import Service

    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert project["name"] == "millrace-os"
    assert project["version"] == __version__ == Version().version == "0.23.2"
    assert project["requires-python"] == ">=3.12"
    assert project["optional-dependencies"]["router"] == [
        "millrace-plus[router]>=0.23.2,<0.24.0"
    ]
    service = Service(
        ApplicationConfig(
            config_dir=tmp_path / "config",
            state_dir=tmp_path / "state",
            credential_path=tmp_path / "config/credentials.json",
        )
    )
    generated = export_artifacts(service.app)
    assert service.app.version == __version__
    assert json.loads(generated["openapi.json"])["info"]["version"] == __version__
    for name, content in generated.items():
        assert files("millrace_os").joinpath("schemas", name).read_bytes() == content


def test_router_optional_modules_are_loaded_only_on_use(tmp_path, monkeypatch) -> None:
    import importlib

    import pytest

    from millrace_os.config import ApplicationConfig
    from millrace_os.router_client import RouterUnavailable
    from millrace_os.service import Service

    original = importlib.import_module
    optional_imports = []

    def refuse_optional(name, *args, **kwargs):
        if name == "millrace_plus" or name.startswith(("millrace_plus.", "millrace.")):
            optional_imports.append(name)
            raise ImportError("optional router component unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", refuse_optional)
    service = Service(
        ApplicationConfig(
            config_dir=tmp_path / "config",
            state_dir=tmp_path / "state",
            credential_path=tmp_path / "config/credentials.json",
            router_state_homes={"main": tmp_path / "core"},
        )
    )
    assert not optional_imports
    with pytest.raises(RouterUnavailable) as failure:
        service.router_client.list_instances("main")
    assert failure.value.code == "router_unavailable"
    assert optional_imports == ["millrace_plus.router"]


def test_sdist_includes_tests_and_docs_but_excludes_publication_controls() -> None:
    root = Path(__file__).parents[2]
    backend = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["uv"][
        "build-backend"
    ]
    assert {"docs/**", "tests/**", "uv.lock"} <= set(backend["source-include"])
    assert {"/.github", "/.gitignore"} <= set(backend["source-exclude"])
    publication = root / ".github/workflows/publish-to-pypi.yml"
    # Release controls are omitted from the sdist, which must remain testable.
    if publication.exists():
        text = publication.read_text()
        assert "SOURCE_DATE_EPOCH: '1580601600'" in text
        assert "uv lock --check" in text
        assert "EXPECTED_WHEEL_SHA256" in text
        assert "EXPECTED_SDIST_SHA256" in text


def test_mcp_is_explicit_optional_and_has_dedicated_schemas() -> None:
    import json
    import subprocess
    import sys

    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert project["project"]["optional-dependencies"]["mcp"] == [
        "mcp==1.30.0; python_version < '3.14'",
        "PyJWT[crypto]>=2.10.1; python_version < '3.14'",
    ]
    assert project["tool"]["uv"]["environments"] == [
        "python_version < '3.14'",
        "python_version >= '3.14'",
    ]
    for name in ("mcp-config-v1.json", "mcp-tools-v1.json"):
        assert json.loads(files("millrace_os").joinpath("schemas", name).read_text())
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import millrace_os.cli; "
            "assert not any(x == 'mcp' or x.startswith('mcp.') for x in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_stock_dashboard_closure_is_exact_package_data() -> None:
    import hashlib
    import json

    from millrace_os.frontend import MANIFEST_NAME

    dashboard = files("millrace_os").joinpath("assets", "dashboard")
    manifest = json.loads(dashboard.joinpath(MANIFEST_NAME).read_bytes())
    listed = {item["path"]: item["sha256"] for item in manifest["assets"]}
    assert {"index.html", "THIRD_PARTY_NOTICES.txt", "dependency-inventory.json"} <= (
        set(listed)
    )
    for path, digest in listed.items():
        content = dashboard.joinpath(*path.split("/")).read_bytes()
        assert hashlib.sha256(content).hexdigest() == digest, path
        assert not path.endswith(".map")
    inventory = json.loads(dashboard.joinpath("dependency-inventory.json").read_bytes())
    assert inventory["format"] == "millrace-os-dependency-inventory/v1"
    notices = dashboard.joinpath("THIRD_PARTY_NOTICES.txt").read_text()
    for package in inventory["bundled"]:
        assert f"{package['name']}@{package['version']}" in notices
    assert not files("millrace_os").joinpath("assets", "bootstrap.js").is_file()
    assert not files("millrace_os").joinpath("assets", "bootstrap.html").is_file()
    assert files("millrace_os.recovery").joinpath("bootstrap.js").is_file()
    assert files("millrace_os").joinpath("assets", "recovery.html").is_file()


def test_frontend_sources_ship_in_the_sdist_without_node_artifacts() -> None:
    root = Path(__file__).parents[2]
    backend = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["uv"][
        "build-backend"
    ]
    assert {"frontend/**", "scripts/export_dashboard.py"} <= set(
        backend["source-include"]
    )
    assert {"frontend/node_modules", "frontend/dist", "*.map"} <= set(
        backend["source-exclude"]
    )
    package = (root / "frontend" / "package.json").read_text()
    assert '"private": true' in package


def test_browser_lane_pins_the_playwright_driver_of_its_chromium_build() -> None:
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert project["project"]["optional-dependencies"]["browser"] == [
        "playwright==1.63.0"
    ]


def _exporter():
    """The maintainer exporter, loaded from its file; it imports no product."""
    path = Path(__file__).parents[2] / "scripts" / "export_dashboard.py"
    spec = importlib.util.spec_from_file_location("export_dashboard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLOSURE = [
    "assets/graph-b.css",
    "assets/graph-b.js",
    "assets/index-a.css",
    "assets/index-a.js",
]


def _chunk(name: str, *lengths: object) -> dict:
    modules = [
        {"id": f"/f/src/{index}.ts", "renderedLength": length}
        for index, length in enumerate(lengths)
    ]
    return {"fileName": name, "modules": modules}


def _record(*chunks: object) -> dict:
    return {"format": "millrace-os-module-closure/v1", "chunks": list(chunks)}


GRAPH = _chunk("assets/graph-b.js", 0, 12)
INDEX = _chunk("assets/index-a.js", 40)


def test_exporter_accepts_records_covering_each_emitted_js_chunk_once() -> None:
    record = _record(GRAPH, INDEX)
    assert _exporter()._chunks(record, CLOSURE) == [GRAPH, INDEX]


@pytest.mark.parametrize(
    ("chunks", "refusal"),
    [
        ([], "missing ['assets/graph-b.js', 'assets/index-a.js'], extra []"),
        ([INDEX], "missing ['assets/graph-b.js'], extra []"),
        ([GRAPH, INDEX, INDEX], "duplicate ['assets/index-a.js']"),
        ([GRAPH, INDEX, _chunk("assets/x.js", 3)], "extra ['assets/x.js']"),
        ([GRAPH, _chunk("assets/graph-b.css", 3)], "extra ['assets/graph-b.css']"),
        ([GRAPH, _chunk("assets/index-a.js", 0, 0)], "records no emitted module"),
        ([GRAPH, _chunk("assets/index-a.js")], "records no emitted module"),
        ([GRAPH, {"fileName": "assets/index-a.js"}], "malformed chunk"),
        ([GRAPH, "assets/index-a.js"], "malformed chunk"),
        ([GRAPH, _chunk("assets/index-a.js", True)], "malformed module record"),
        ([GRAPH, _chunk("assets/index-a.js", -1)], "malformed module record"),
        ([GRAPH, _chunk("assets/index-a.js", 2.0)], "malformed module record"),
    ],
)
def test_exporter_refuses_records_that_miss_or_misstate_the_js_closure(
    chunks: list, refusal: str
) -> None:
    exporter = _exporter()
    with pytest.raises(exporter.ExportError, match=re.escape(refusal)):
        exporter._chunks(_record(*chunks), CLOSURE)


def test_exporter_never_derives_notices_from_an_empty_module_record() -> None:
    exporter = _exporter()
    with pytest.raises(exporter.ExportError, match="does not cover"):
        exporter._inventory(_record(), CLOSURE, {}, b"")
    with pytest.raises(exporter.ExportError, match="unknown format"):
        exporter._inventory({"chunks": [GRAPH, INDEX]}, CLOSURE, {}, b"")
