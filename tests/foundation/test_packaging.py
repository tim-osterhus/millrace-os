from __future__ import annotations

import tomllib
from importlib.resources import files
from pathlib import Path


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
    assert project["version"] == __version__ == Version().version == "0.23.0"
    assert project["requires-python"] == ">=3.12"
    assert project["optional-dependencies"]["router"] == [
        "millrace-plus[router]>=0.23.0,<0.24.0"
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
