from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_legacy_ledger_has_required_dispositions_and_boundaries() -> None:
    text = (ROOT / "docs/architecture/legacy-disposition.md").read_text()

    for term in (
        "retain",
        "adapt",
        "preserve as evidence",
        "retire",
        "Millracer",
        "second-runtime",
        "Millmux",
        "Tauri",
        "v0.20",
        "v0.21",
        "redaction",
        "recovery",
        "Mission Control",
    ):
        assert term.lower() in text.lower()


def test_contract_inventory_pins_identities_digests_and_refusals() -> None:
    text = (ROOT / "docs/compatibility/release-1-contract-inventory.md").read_text()

    for term in (
        "Millrace",
        "Millrace Plus",
        "LAD",
        "installed package",
        "stock tmux",
        "public reads",
        "actions",
        "events",
        "freshness",
        "stable identity",
        "invocation",
        "pause",
        "resume",
        "daemon",
        "readiness",
        "graceful",
        "refusal",
        "sha256:",
    ):
        assert term.lower() in text.lower()

    assert len(re.findall(r"sha256:[0-9a-f]{64}", text)) >= 5
    assert "private" in text.lower()
    assert "pane typing" in text.lower()


def test_frozen_release_artifact_history_remains_explicit() -> None:
    text = (ROOT / "docs/compatibility/release-1-contract-inventory.md").read_text()

    for relative_path in (
        "pyproject.toml",
        "uv.lock",
        "src/millrace_os/storage/migrations/001_initial.sql",
    ):
        assert relative_path in text

    # The inventory is a frozen historical record.  P01 changes the source
    # bytes and records fresh candidate hashes in its handoff instead of
    # pretending the old inventory still describes the candidate.
    assert "frozen" in text.lower()
    assert len(re.findall(r"sha256:[0-9a-f]{64}", text)) >= 5
