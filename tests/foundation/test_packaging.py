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
