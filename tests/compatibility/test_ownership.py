from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_foundation_does_not_import_legacy_private_runtime_storage() -> None:
    source_root = ROOT / "src/millrace_os"
    forbidden = ("millrace.db", "private runtime", "pane typing", "millmux.db")
    source = "\n".join(path.read_text() for path in source_root.rglob("*.py"))

    for term in forbidden:
        assert term not in source.lower()
