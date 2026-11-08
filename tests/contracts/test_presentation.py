"""Shared presentation store: files, CAS, normalization, faults, API and CLI.

Most tests exercise `millrace_os.presentation` directly. The API and CLI tests
at the end cover only the wiring boundaries: trust, raw parsing, sizes,
preserved files, busy/uncertain responses and the CLI's GET -> helper -> PUT.
"""

import copy
import errno
import fcntl
import hashlib
import inspect
import io
import json
import os
import stat
import urllib.error
import urllib.parse

import pytest
from pydantic import ValidationError
from test_auth import bootstrap, setup

from millrace_os import presentation as p
from millrace_os.api import (
    DEFAULT_BODY_LIMIT,
    RESPONSE_LIMIT,
    body_limit,
    export_artifacts,
)
from millrace_os.contracts import (
    PresentationConfig,
    PresentationState,
    PresentationWrite,
    schema_artifacts,
)
from millrace_os.paths import UnsafePathError

STOCK = {"frontend_id": "stock", "presentation_format": p.STOCK_FORMAT}
BOARD = {
    "frontend_id": "example.board",
    "presentation_format": "example.board-layout/v1",
}
OTHER = {"frontend_id": "example.other", "presentation_format": "example.other/v1"}
STOCK_KEY = ("stock", p.STOCK_FORMAT)
BOARD_KEY = ("example.board", "example.board-layout/v1")
SHARED, SECOND, MISSING = "a" * 32, "b" * 32, "c" * 32
WRITE = "millrace-os-presentation-write/v1"


def refused(code, call, *args, **kwargs):
    with pytest.raises(p.PresentationError) as error:
        call(*args, **kwargs)
    assert (error.value.code, error.value.status) == (code, p.STATUS[code])


def stock_payload(preset="precision", **changes):
    payload = next(
        copy.deepcopy(item["payload"])
        for item in p.bundled_configs()
        if item["id"] == preset
    )
    payload.update(changes)
    return payload


def bundled(identity):
    return {"kind": "bundled", "id": identity}


def user(identity):
    return {"kind": "user", "id": identity}


def config(identity, name, payload):
    return {"id": identity, "name": name, "payload": payload}


def namespace(key, default, *configs):
    ordered = sorted(configs, key=lambda item: item["id"])
    return {**key, "default_ref": default, "configs": ordered}


def store(*namespaces):
    ordered = sorted(
        namespaces, key=lambda item: (item["frontend_id"], item["presentation_format"])
    )
    return {"format": p.STORE_FORMAT, "namespaces": ordered}


def compare(value, expected):
    return {
        "format": WRITE,
        "mode": "compare",
        "expected_version": expected,
        "store": value,
    }


def stock_only(default):
    return store(namespace(STOCK, default))


@pytest.fixture
def home(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    return directory


def stored(home):
    return (home / "presentation.json").read_bytes()


def temporaries(home):
    return sorted(
        path.name for path in home.iterdir() if path.name.startswith(".presentation-")
    )


def plant(home, raw, mode=0o600):
    path = home / "presentation.json"
    path.write_bytes(raw)
    os.chmod(path, mode)
    return raw


def spaces(body):
    """Namespaces of a write body or GET state, keyed by their exact pair."""
    return {
        (item["frontend_id"], item["presentation_format"]): item
        for item in body["store"]["namespaces"]
    }


def test_canonical_rule_and_strict_parse() -> None:
    value = {"b": [1, 2.5, None, True, False], "a": "é", "c": {"z": "", "y": -0.0}}
    encoded = p.canonical_json_bytes(value)
    assert encoded == (
        b'{"a":"\xc3\xa9","b":[1,2.5,null,true,false],"c":{"y":-0.0,"z":""}}'
    )
    # The same vector as the Plus reference function.
    assert hashlib.sha256(encoded).hexdigest() == (
        "dc49001709042b1d9e4250deb4bdb47b17ab2967ea3613e961fec9aa21b82fa3"
    )
    assert p.canonical_json_bytes("é") != p.canonical_json_bytes("é")
    for invalid in ({1: "x"}, float("nan"), [float("inf")], "\ud800", (1,), {1.5}):
        with pytest.raises(ValueError):
            p.canonical_json_bytes(invalid)
    for raw in (
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b"[Infinity]",
        b"[-Infinity]",
        b"\xef\xbb\xbf{}",
        b'"\xff"',
        b"{} trailing",
    ):
        with pytest.raises(ValueError):
            p.strict_loads(raw)
    assert p.strict_loads(b'{"a":[1,"\\u00e9"]}') == {"a": [1, "é"]}


def test_bundled_stock_presets_are_exact_and_immutable() -> None:
    presets = p.bundled_configs()
    assert [(item["id"], item["name"]) for item in presets] == [
        ("precision", "Precision"),
        ("command", "Command"),
        ("studio", "Studio"),
    ]
    shown = dict.fromkeys(p._PANELS, True)
    halves = dict.fromkeys(p._SPLITS, 0.5)
    expected = {
        "precision": ("comfortable", "detail-split", shown, halves),
        "command": (
            "compact",
            "status-stack",
            {**shown, "graph": False, "inspector": False, "revisions": False},
            halves,
        ),
        "studio": (
            "comfortable",
            "graph-split",
            {**shown, "planes": False, "current-work": False, "revisions": False},
            {**halves, "graph-split": 0.65},
        ),
    }
    for item in presets:
        density, arrangement, panels, ratios = expected[item["id"]]
        assert item["payload"] == {
            "preset": item["id"],
            "theme": "system",
            "density": density,
            "arrangement": arrangement,
            "panels": panels,
            "split_ratios": ratios,
        }
        assert (item["frontend_id"], item["presentation_format"]) == STOCK_KEY
        digest = hashlib.sha256(p.canonical_json_bytes(item["payload"])).hexdigest()
        assert item["payload_sha256"] == digest
        assert item["bundle_sha256"] is None
    presets[0]["payload"]["theme"] = "dark"
    assert p.bundled_configs()[0]["payload"]["theme"] == "system"


def test_absent_create_stale_tokens_and_restart(home) -> None:
    absent = p.read_state(home)
    assert absent["contract"] == "millrace-os-presentation-state/v1"
    assert (absent["status"], absent["version"], absent["file_sha256"]) == (
        "absent",
        None,
        None,
    )
    assert absent["store"] is None and len(absent["bundled_configs"]) == 3
    assert not (home / "presentation.json").exists()  # nothing written on read
    initial = stock_only(bundled("command"))
    first = p.write_state(home, compare(initial, None))
    assert first["status"] == "ready" and len(first["version"]) == 32
    assert first["store"] == {**initial, "version": first["version"]}
    raw = stored(home)
    assert raw == p.canonical_json_bytes(first["store"]) and not raw.endswith(b"\n")
    assert first["file_sha256"] == hashlib.sha256(raw).hexdigest()
    for name in ("presentation.json", ".presentation.lock"):
        assert stat.S_IMODE((home / name).stat().st_mode) == 0o600
    assert temporaries(home) == []
    changed = stock_only(bundled("studio"))
    refused("presentation_conflict", p.write_state, home, compare(changed, None))
    refused("presentation_conflict", p.write_state, home, compare(changed, "f" * 32))
    assert stored(home) == raw
    second = p.write_state(home, compare(changed, first["version"]))
    assert second["version"] != first["version"]
    # A fresh read (as after a restart) keeps the saved token and store.
    reread = p.read_state(home)
    for key in ("status", "version", "file_sha256", "store"):
        assert reread[key] == second[key], key
    stale = compare(initial, first["version"])
    refused("presentation_conflict", p.write_state, home, stale)


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b'{"format":"millrace-os-presentation-store/v2","format":"x"}',
        b'{"format":"millrace-os-presentation-store/v2"}',
        b'{"format":"millrace-os-presentation-store/v1","namespaces":[]}',
        b'{"value":NaN}',
        b"\xef\xbb\xbf{}",
        b"[]",
    ],
)
def test_invalid_files_are_preserved_and_refuse_ordinary_writes(home, raw) -> None:
    plant(home, raw)
    state = p.read_state(home)
    assert (state["status"], state["version"]) == ("invalid", None)
    assert state["store"] is None
    assert state["file_sha256"] == hashlib.sha256(raw).hexdigest()
    value = stock_only(bundled("precision"))
    refused("presentation_integrity_error", p.write_state, home, compare(value, None))
    assert stored(home) == raw and temporaries(home) == []


def test_newer_format_is_incompatible_and_replacement_needs_the_exact_digest(
    home,
) -> None:
    previous = p.write_state(home, compare(stock_only(bundled("command")), None))
    newer = plant(
        home,
        p.canonical_json_bytes(
            {"format": "millrace-os-presentation-store/v3", "anything": [1]}
        ),
    )
    state = p.read_state(home)
    assert (state["status"], state["version"]) == ("incompatible", None)
    digest = hashlib.sha256(newer).hexdigest()
    assert state["file_sha256"] == digest
    value = stock_only(bundled("studio"))
    refused("presentation_incompatible", p.write_state, home, compare(value, None))
    wrong = p.HELPERS["replace_unreadable"](state, "0" * 64, value)
    refused("presentation_conflict", p.write_state, home, wrong)
    assert stored(home) == newer
    body = p.HELPERS["replace_unreadable"](state, digest, value)
    assert body == {
        "format": WRITE,
        "mode": "replace_unreadable",
        "observed_file_sha256": digest,
        "store": value,
    }
    replaced = p.write_state(home, body)
    assert replaced["status"] == "ready"
    # A fresh token, never a reset counter: the pre-damage token stays stale.
    assert replaced["version"] != previous["version"]
    stale = compare(value, previous["version"])
    refused("presentation_conflict", p.write_state, home, stale)
    # Replacing a readable store is not this operation.
    again = p.HELPERS["replace_unreadable"](replaced, replaced["file_sha256"], value)
    refused("presentation_conflict", p.write_state, home, again)


@pytest.mark.parametrize(
    "suffix,status",
    [
        ("3", "incompatible"),
        ("9" * 5000, "incompatible"),
        ("1", "invalid"),
        ("02", "invalid"),
    ],
)
def test_store_format_versions_compare_as_digit_text(home, suffix, status) -> None:
    raw = plant(
        home,
        p.canonical_json_bytes({"format": "millrace-os-presentation-store/v" + suffix}),
    )
    state = p.read_state(home)
    assert (state["status"], state["version"], state["store"]) == (status, None, None)
    assert state["file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert stored(home) == raw


def test_huge_integer_split_ratio_is_a_typed_refusal(home) -> None:
    ratios = {**stock_payload()["split_ratios"], "graph-split": 10**400}
    payload = stock_payload(split_ratios=ratios)
    value = store(namespace(STOCK, user(SHARED), config(SHARED, "Huge", payload)))
    refused("invalid_request", p.write_state, home, compare(value, None))
    assert not (home / "presentation.json").exists()
    raw = plant(home, p.canonical_json_bytes({**value, "version": "d" * 32}))
    state = p.read_state(home)
    assert (state["status"], state["version"], state["store"]) == (
        "invalid",
        None,
        None,
    )
    assert state["file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert stored(home) == raw


def test_unsafe_targets_refuse_without_a_digest(home, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(b"{}")
    target = home / "presentation.json"
    value = stock_only(bundled("precision"))

    def unsafe():
        with pytest.raises(UnsafePathError):
            p.read_state(home)
        with pytest.raises(UnsafePathError):
            p.write_state(home, compare(value, None))

    target.symlink_to(elsewhere)
    unsafe()
    assert elsewhere.read_bytes() == b"{}" and target.is_symlink()
    target.unlink()
    plant(home, b"{}", mode=0o640)
    unsafe()
    target.unlink()
    target.mkdir()
    unsafe()
    target.rmdir()
    os.mkfifo(target, 0o600)  # nonregular: refused without blocking
    unsafe()
    assert temporaries(home) == []


def test_oversized_regular_file_is_hashed_in_chunks(home, monkeypatch) -> None:
    raw = plant(home, b"x" * (3 * p.STATE_LIMIT))
    requested = []
    real = os.read

    def recording(descriptor, size):
        requested.append(size)
        return real(descriptor, size)

    monkeypatch.setattr(os, "read", recording)
    state = p.read_state(home)
    assert (state["status"], state["store"]) == ("invalid", None)
    assert state["file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert requested and max(requested) <= 65_536


def test_write_bodies_and_stores_are_strict(home) -> None:
    board_payload = {"nested": [{"any": None}], "n": 1.5}
    good = store(
        namespace(STOCK, user(SHARED), config(SHARED, "Mine", stock_payload())),
        namespace(BOARD, None, config(SHARED, "Board", board_payload)),
    )
    p.validate_store(good, versioned=False)
    board_ns, stock_ns = good["namespaces"]

    def ordered(*namespaces):
        return {**good, "namespaces": list(namespaces)}

    def with_stock(**changes):
        return ordered(board_ns, {**stock_ns, **changes})

    def with_board(**changes):
        return ordered({**board_ns, **changes}, stock_ns)

    def stock_config(payload, name="Mine"):
        return with_stock(configs=[config(SHARED, name, payload)])

    def board_config(payload):
        return with_board(configs=[config(SHARED, "B", payload)])

    ratios = stock_payload()["split_ratios"]
    panels = stock_payload()["panels"]
    unpanelled = {k: v for k, v in stock_payload().items() if k != "panels"}
    reversed_ids = [
        config(SECOND, "x", stock_payload()),
        config(SHARED, "y", stock_payload()),
    ]
    invalid_stores = [
        {**good, "version": "d" * 32},
        {**good, "format": "millrace-os-presentation-store/v3"},
        ordered(stock_ns, board_ns),
        ordered(board_ns, board_ns),
        with_stock(configs=reversed_ids),
        with_stock(configs=[config(SHARED, "x", stock_payload())] * 2),
        with_stock(default_ref=user(SECOND)),
        with_stock(default_ref=None),
        with_stock(default_ref=bundled("default")),
        with_board(default_ref=bundled("precision")),
        stock_config(stock_payload(theme="neon")),
        stock_config({**stock_payload(), "preset": "custom"}),
        stock_config(stock_payload(split_ratios={**ratios, "graph-split": 1})),
        stock_config(stock_payload(split_ratios={**ratios, "graph-split": 0})),
        stock_config(stock_payload(split_ratios={**ratios, "graph-split": True})),
        stock_config(stock_payload(panels={**panels, "graph": 1})),
        stock_config(unpanelled),
        stock_config({**stock_payload(), "status": True}),
        with_stock(presentation_format="example.other/v1"),
        with_board(frontend_id="Example"),
        stock_config(stock_payload(), name="two\nlines"),
        stock_config(stock_payload(), name=""),
        stock_config(stock_payload(), name="x" * 257),
        board_config([1]),
        board_config({"x": float("nan")}),
        board_config({"x": "\ud800"}),
    ]
    for value in invalid_stores:
        refused("invalid_request", p.write_state, home, compare(value, None))
    body = compare(good, None)
    replace = {"format": WRITE, "mode": "replace_unreadable", "store": good}
    invalid_bodies = [
        {**body, "format": "millrace-os-presentation-write/v2"},
        {**body, "mode": "overwrite"},
        {key: value for key, value in body.items() if key != "expected_version"},
        {**body, "observed_file_sha256": "0" * 64},
        compare(good, "short"),
        {**replace, "observed_file_sha256": "x"},
        replace,
        [],
    ]
    for invalid in invalid_bodies:
        refused("invalid_request", p.write_state, home, invalid)
    assert not (home / "presentation.json").exists()
    written = p.write_state(home, body)
    assert written["store"]["namespaces"] == good["namespaces"]


def test_get_envelope_over_the_limit_refuses_the_write(home) -> None:
    def board(blob):
        return store(namespace(BOARD, None, config(SHARED, "Blob", {"blob": blob})))

    refused(
        "presentation_too_large",
        p.write_state,
        home,
        compare(board("x" * p.STATE_LIMIT), None),
    )
    assert not (home / "presentation.json").exists()
    fits = p.write_state(home, compare(board("x" * 100_000), None))
    assert fits["status"] == "ready"


def test_a_held_lock_refuses_busy_without_writing(home) -> None:
    value = stock_only(bundled("precision"))
    descriptor = os.open(home / ".presentation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        refused("presentation_busy", p.write_state, home, compare(value, None))
        assert not (home / "presentation.json").exists()
    finally:
        os.close(descriptor)
    assert p.write_state(home, compare(value, None))["status"] == "ready"


@pytest.mark.parametrize("stage", ["temporary_fsync", "target_recheck", "replace"])
def test_failures_before_or_at_replace_keep_previous_bytes(
    home, monkeypatch, stage
) -> None:
    first = p.write_state(home, compare(stock_only(bundled("command")), None))
    before = stored(home)
    changed = stock_only(bundled("studio"))
    if stage == "temporary_fsync":
        real = os.fsync

        def failing(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, "injected temporary fsync failure")
            real(descriptor)

        monkeypatch.setattr(os, "fsync", failing)
        expected = OSError
    elif stage == "target_recheck":
        real_validate = p.validate_file_target

        def recheck(path, *args, **kwargs):
            if path.name == "presentation.json" and temporaries(home):
                raise UnsafePathError("injected target change before replace")
            return real_validate(path, *args, **kwargs)

        monkeypatch.setattr(p, "validate_file_target", recheck)
        expected = UnsafePathError
    else:

        def failing_replace(*args, **kwargs):
            raise OSError(errno.EIO, "injected replace failure")

        monkeypatch.setattr(os, "replace", failing_replace)
        expected = OSError
    with pytest.raises(expected):
        p.write_state(home, compare(changed, first["version"]))
    monkeypatch.undo()
    assert stored(home) == before and temporaries(home) == []
    assert p.read_state(home)["version"] == first["version"]


def test_directory_fsync_failure_after_replace_is_uncertain(home, monkeypatch) -> None:
    first = p.write_state(home, compare(stock_only(bundled("command")), None))
    changed = stock_only(bundled("studio"))
    real = os.fsync

    def failing(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real(descriptor)

    monkeypatch.setattr(os, "fsync", failing)
    body = compare(changed, first["version"])
    refused("presentation_write_uncertain", p.write_state, home, body)
    monkeypatch.undo()
    # No rollback: a GET shows the new complete store with its fresh token.
    state = p.read_state(home)
    assert state["status"] == "ready" and state["version"] != first["version"]
    assert state["store"]["namespaces"] == changed["namespaces"]
    assert stored(home) == p.canonical_json_bytes(state["store"])
    assert temporaries(home) == []


def two_namespaces(home):
    value = store(
        namespace(
            STOCK, user(SHARED), config(SHARED, "Mine", stock_payload("command"))
        ),
        namespace(
            BOARD,
            None,
            config(SHARED, "Board", {"columns": 3}),
            config(SECOND, "Board", {"columns": 4}),
        ),
    )
    return p.write_state(home, compare(value, None))


def test_two_namespaces_with_overlapping_ids_stay_isolated(home) -> None:
    state = two_namespaces(home)
    helpers = p.HELPERS
    stock_read = helpers["read"](state, STOCK, user(SHARED))["result"]
    assert stock_read["payload"] == stock_payload("command")
    board_read = helpers["read"](state, BOARD, user(SHARED))["result"]
    assert board_read["payload"] == {"columns": 3}
    renamed = helpers["rename"](state, STOCK, SHARED, "Renamed")
    assert (renamed["format"], renamed["mode"]) == (WRITE, "compare")
    assert renamed["expected_version"] == state["version"]
    before = spaces(state)
    assert spaces(renamed)[STOCK_KEY]["configs"][0]["name"] == "Renamed"
    assert spaces(renamed)[BOARD_KEY] == before[BOARD_KEY]
    updated = spaces(helpers["update"](state, BOARD, SHARED, {"rows": 2}))
    assert updated[BOARD_KEY]["configs"][0]["payload"] == {"rows": 2}
    assert updated[STOCK_KEY] == before[STOCK_KEY]
    deleted = spaces(helpers["delete"](state, BOARD, SHARED))
    assert [item["id"] for item in deleted[BOARD_KEY]["configs"]] == [SECOND]
    assert deleted[STOCK_KEY] == before[STOCK_KEY]
    # A missing namespace or config ID refuses before any PUT.
    for name, args in (
        ("read", (OTHER, user(SHARED))),
        ("read", (BOARD, user(MISSING))),
        ("rename", (OTHER, SHARED, "x")),
        ("update", (BOARD, MISSING, {})),
        ("delete", (OTHER, SHARED)),
        ("reset", (BOARD, MISSING)),
        ("list", (OTHER,)),
    ):
        refused("presentation_config_not_found", helpers[name], state, *args)
    # Caller expected_version: omitted skips the check; null means absent.
    for mismatch in ("f" * 32, None):
        refused(
            "presentation_conflict",
            helpers["rename"],
            state,
            STOCK,
            SHARED,
            "x",
            mismatch,
        )
    assert helpers["rename"](state, STOCK, SHARED, "x", state["version"])
    # Names are labels: an existing name is accepted unchanged, never deduped.
    created = spaces(helpers["create"](state, BOARD, "Board", {"columns": 5}))
    names = [item["name"] for item in created[BOARD_KEY]["configs"]]
    assert names.count("Board") == 3
    # Two clients: the second body comes from the same, now stale, GET.
    first = helpers["rename"](state, STOCK, SHARED, "First")
    second = helpers["rename"](state, STOCK, SHARED, "Second")
    assert p.write_state(home, first)["status"] == "ready"
    refused("presentation_conflict", p.write_state, home, second)


def test_delete_default_normalization_and_reset(home) -> None:
    state = two_namespaces(home)
    helpers = p.HELPERS
    deleted = spaces(helpers["delete"](state, STOCK, SHARED))
    assert deleted[STOCK_KEY]["default_ref"] == bundled("precision")
    custom = p.write_state(home, helpers["set_default"](state, BOARD, user(SHARED)))
    deleted = spaces(helpers["delete"](custom, BOARD, SHARED))
    assert deleted[BOARD_KEY]["default_ref"] is None
    refused("presentation_incompatible", helpers["reset"], custom, BOARD, SHARED)
    # With a selected compatible custom bundle default, both fall back to it.
    with_bundle = copy.deepcopy(custom)
    base = {"columns": 1}
    with_bundle["bundled_configs"].append(
        {
            **BOARD,
            "id": "default",
            "name": "Board default",
            "payload": base,
            "payload_sha256": hashlib.sha256(p.canonical_json_bytes(base)).hexdigest(),
            "bundle_sha256": "d" * 64,
        }
    )
    deleted = spaces(helpers["delete"](with_bundle, BOARD, SHARED))
    assert deleted[BOARD_KEY]["default_ref"] == bundled("default")
    reset = spaces(helpers["reset"](with_bundle, BOARD, SHARED))
    assert reset[BOARD_KEY]["configs"][0] == config(SHARED, "Board", base)
    # Stock reset copies the bundled base named by the payload's preset.
    studio = stock_payload("studio", density="compact")
    edited = p.write_state(home, helpers["update"](custom, STOCK, SHARED, studio))
    reset = spaces(helpers["reset"](edited, STOCK, SHARED))
    assert reset[STOCK_KEY]["configs"][0] == config(
        SHARED, "Mine", stock_payload("studio")
    )


def test_absent_defaults_duplicate_and_import_export(home) -> None:
    helpers = p.HELPERS
    absent = p.read_state(home)
    default = helpers["read_default"](absent, STOCK)
    assert default["status"] == "absent"
    assert (default["result"]["kind"], default["result"]["id"]) == (
        "bundled",
        "precision",
    )
    assert helpers["read_default"](absent, BOARD)["result"] is None
    copied = helpers["duplicate"](absent, STOCK, bundled("command"), "Mine")
    assert copied["expected_version"] is None
    (stock_ns,) = copied["store"]["namespaces"]
    assert stock_ns["default_ref"] == bundled("precision")
    (made,) = stock_ns["configs"]
    assert (made["name"], made["payload"]) == ("Mine", stock_payload("command"))
    state = p.write_state(home, copied)
    exported = helpers["export"](state, STOCK, user(made["id"]))["result"]
    assert exported == {
        "format": "millrace-os-presentation-config/v2",
        "frontend_id": "stock",
        "presentation_format": p.STOCK_FORMAT,
        "name": "Mine",
        "payload": stock_payload("command"),
    }
    imported = spaces(helpers["import"](state, exported))
    ids = [item["id"] for item in imported[STOCK_KEY]["configs"]]
    assert len(ids) == 2 and made["id"] in ids  # a new ID; nothing overwritten
    renamed = spaces(helpers["import"](state, exported, "Imported"))
    names = sorted(item["name"] for item in renamed[STOCK_KEY]["configs"])
    assert names == ["Imported", "Mine"]
    board_document = {**exported, **BOARD, "payload": {"columns": 2}}
    created = spaces(helpers["import"](state, board_document))
    assert created[BOARD_KEY]["default_ref"] is None
    for document in (
        {**exported, "format": "millrace-os-presentation-config/v1"},
        {**exported, "extra": True},
        {key: value for key, value in exported.items() if key != "payload"},
        {**exported, "payload": {"preset": "precision"}},
    ):
        refused("invalid_request", helpers["import"], state, document)


def test_list_and_default_references_are_strict(home) -> None:
    helpers = p.HELPERS
    state = p.write_state(
        home,
        helpers["duplicate"](p.read_state(home), STOCK, bundled("studio"), "Mine"),
    )
    (made,) = state["store"]["namespaces"][0]["configs"]
    listing = helpers["list"](state)["result"]
    assert listing == [
        {
            **STOCK,
            "default_ref": bundled("precision"),
            "configs": [{"id": made["id"], "name": "Mine"}],
        }
    ]
    assert helpers["list"](state, STOCK)["result"] == listing
    for ref in (None, bundled("default"), user("x")):
        refused("invalid_request", helpers["set_default"], state, STOCK, ref)
    refused(
        "invalid_request", helpers["set_default"], state, BOARD, bundled("precision")
    )
    refused(
        "presentation_config_not_found",
        helpers["set_default"],
        state,
        STOCK,
        user(MISSING),
    )
    started = spaces(helpers["set_default"](state, BOARD, None))
    assert started[BOARD_KEY] == {**BOARD, "default_ref": None, "configs": []}
    refused(
        "presentation_config_not_found",
        helpers["duplicate"],
        state,
        STOCK,
        user(MISSING),
        "x",
    )
    for wrong in (
        {"frontend_id": "stock", "presentation_format": "example.other/v1"},
        {"frontend_id": "Bad", "presentation_format": "example.other/v1"},
        {**STOCK, "extra": 1},
    ):
        refused("invalid_request", helpers["read_default"], state, wrong)


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"{broken", "presentation_integrity_error"),
        (
            b'{"format":"millrace-os-presentation-store/v9"}',
            "presentation_incompatible",
        ),
    ],
)
def test_unreadable_state_refuses_derived_reads_and_mutations(home, raw, code) -> None:
    plant(home, raw)
    state = p.read_state(home)
    assert p.HELPERS["get"](state) is state
    document = {"format": p.CONFIG_FORMAT, **BOARD, "name": "x", "payload": {}}
    for name, args in (
        ("list", ()),
        ("read", (STOCK, bundled("precision"))),
        ("read_default", (STOCK,)),
        ("export", (STOCK, bundled("precision"))),
        ("create", (BOARD, "x", {})),
        ("set_default", (BOARD, None)),
        ("import", (document,)),
    ):
        refused(code, p.HELPERS[name], state, *args)
    assert stored(home) == raw


def test_helper_table_names_exactly_the_fifteen_alternatives() -> None:
    expected = {
        "get": [],
        "put": ["write"],
        "replace_unreadable": ["observed_file_sha256", "store"],
        "list": ["namespace"],
        "read": ["namespace", "ref"],
        "read_default": ["namespace"],
        "export": ["namespace", "ref"],
        "create": ["namespace", "name", "payload", "expected_version"],
        "update": ["namespace", "config_id", "payload", "expected_version"],
        "rename": ["namespace", "config_id", "name", "expected_version"],
        "duplicate": ["namespace", "source", "name", "expected_version"],
        "delete": ["namespace", "config_id", "expected_version"],
        "set_default": ["namespace", "default", "expected_version"],
        "reset": ["namespace", "config_id", "expected_version"],
        "import": ["config", "name", "expected_version"],
    }
    assert set(p.HELPERS) == set(expected)
    for operation, arguments in expected.items():
        parameters = list(inspect.signature(p.HELPERS[operation]).parameters)
        assert parameters == ["state", *arguments], operation
    body = compare(store(), None)
    assert p.HELPERS["put"](None, body) is body
    refused("invalid_request", p.HELPERS["put"], None, {**body, "mode": "x"})


def test_broken_presentation_file_does_not_affect_startup_or_recovery(
    tmp_path,
) -> None:
    for damaged in (False, True):
        root = tmp_path / ("recovery" if damaged else "normal")
        (root / "config").mkdir(parents=True, mode=0o700)
        raw = plant(root / "config", b"{not a store")
        service, client, bearer = setup(root, damaged=damaged)
        # Health is unchanged: ready normally, not ready in storage recovery.
        assert client.get("/health").status_code == (503 if damaged else 200)
        response = client.get("/api/v1/recovery/diagnostics", headers=bearer)
        assert response.status_code == 200, response.text
        assert stored(root / "config") == raw
        assert p.read_state(service.config.config_dir)["status"] == "invalid"


# API and CLI wiring. The store rules above are not re-tested here.

ROUTE = "/api/v1/client/presentation"
STOCK_NS = ("--frontend", "stock", "--format", p.STOCK_FORMAT)
BOARD_NS = (
    "--frontend",
    BOARD["frontend_id"],
    "--format",
    BOARD["presentation_format"],
)


def refusal(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    return response.json()


def test_api_round_trips_through_the_config_dir_with_normal_trust(tmp_path) -> None:
    service, client, bearer = setup(tmp_path)
    home = service.config.config_dir
    body = compare(stock_only(bundled("command")), None)
    assert client.get(ROUTE).status_code == 401
    assert client.put(ROUTE, json=body).status_code == 401
    absent = client.get(ROUTE, headers=bearer).json()
    assert (absent["contract"], absent["status"], absent["version"]) == (
        p.STATE_CONTRACT,
        "absent",
        None,
    )
    assert absent["bundled_configs"] == p.bundled_configs()
    assert not (home / "presentation.json").exists()  # reads never write
    refusal(client.get(ROUTE + "?status=ready", headers=bearer), 422, "invalid_request")
    created = client.put(ROUTE, headers=bearer, json=body)
    assert created.status_code == 200, created.text
    state = created.json()
    assert state["status"] == "ready"
    assert state["store"]["namespaces"] == body["store"]["namespaces"]
    assert stored(home) == p.canonical_json_bytes(state["store"])
    assert stat.S_IMODE(os.stat(home / "presentation.json").st_mode) == 0o600
    assert client.get(ROUTE, headers=bearer).json()["version"] == state["version"]
    stale = refusal(
        client.put(ROUTE, headers=bearer, json=body), 409, "presentation_conflict"
    )
    assert stale["retryable"] is False
    # A browser session reads normally; a write needs the origin and CSRF token.
    _, csrf = bootstrap(client, bearer)
    origin = {"Origin": str(client.base_url).rstrip("/")}
    changed = compare(stock_only(bundled("studio")), state["version"])
    assert client.get(ROUTE).json()["version"] == state["version"]
    refusal(client.put(ROUTE, headers=origin, json=changed), 403, "invalid_csrf")
    written = client.put(ROUTE, headers={**origin, "X-CSRF-Token": csrf}, json=changed)
    assert written.status_code == 200, written.text
    for value in (absent, state, written.json()):
        assert PresentationState.model_validate(value).model_dump() == value


def test_api_put_parses_the_raw_body_strictly_within_one_mebibyte(tmp_path) -> None:
    service, client, bearer = setup(tmp_path)
    assert body_limit(ROUTE, "PUT") == 1_048_576
    assert body_limit(ROUTE, "POST") == DEFAULT_BODY_LIMIT
    headers = {**bearer, "Content-Type": "application/json"}
    mine = config(SHARED, "Mine", stock_payload("command"))
    valid = compare(store(namespace(STOCK, bundled("command"), mine)), None)
    text = json.dumps(valid)
    assert "0.5" in text  # a split ratio
    for raw, status, code in (
        (text[:-1] + ',"mode":"compare"}', 422, "invalid_request"),  # duplicate
        (text.replace("0.5", "NaN", 1), 422, "invalid_request"),
        (json.dumps({**valid, "extra": 1}), 422, "invalid_request"),
        (json.dumps({**valid, "mode": "replace"}), 422, "invalid_request"),
        # The shape admits it; the store rules refuse a stock "default".
        (
            json.dumps(compare(stock_only(bundled("default")), None)),
            422,
            "invalid_request",
        ),
        (" " * 1_048_577, 413, "request_too_large"),
    ):
        refusal(client.put(ROUTE, headers=headers, content=raw.encode()), status, code)
    plain = {**bearer, "Content-Type": "text/plain"}
    response = client.put(ROUTE, headers=plain, content=text.encode())
    refusal(response, 415, "json_required")
    assert not (service.config.config_dir / "presentation.json").exists()
    # Larger than the 4 KiB default body limit, within the 1 MiB policy.
    blob = config(SHARED, "Blob", {"blob": "x" * 100_000})
    large = compare(store(namespace(BOARD, None, blob)), None)
    assert client.put(ROUTE, headers=bearer, json=large).status_code == 200


def test_api_preserves_unreadable_files_and_replaces_only_by_digest(tmp_path) -> None:
    service, client, bearer = setup(tmp_path)
    home = service.config.config_dir
    value = stock_only(bundled("studio"))
    for raw, status, code in (
        (b"{not a store", "invalid", "presentation_integrity_error"),
        (
            b'{"format":"millrace-os-presentation-store/v3"}',
            "incompatible",
            "presentation_incompatible",
        ),
    ):
        plant(home, raw)
        digest = hashlib.sha256(raw).hexdigest()
        state = client.get(ROUTE, headers=bearer).json()
        assert (state["status"], state["version"], state["store"]) == (
            status,
            None,
            None,
        )
        assert state["file_sha256"] == digest
        refusal(client.put(ROUTE, headers=bearer, json=compare(value, None)), 409, code)
        wrong = p.HELPERS["replace_unreadable"](None, "0" * 64, value)
        response = client.put(ROUTE, headers=bearer, json=wrong)
        refusal(response, 409, "presentation_conflict")
        assert stored(home) == raw  # preserved byte for byte
    body = p.HELPERS["replace_unreadable"](None, digest, value)
    replaced = client.put(ROUTE, headers=bearer, json=body)
    assert replaced.status_code == 200 and replaced.json()["status"] == "ready"
    os.chmod(home / "presentation.json", 0o644)
    unsafe = refusal(client.get(ROUTE, headers=bearer), 422, "unsafe_path")
    assert "file_sha256" not in unsafe


def test_api_busy_is_retryable_and_an_uncertain_write_reconciles_by_get(
    tmp_path, monkeypatch
) -> None:
    service, client, bearer = setup(tmp_path)
    home = service.config.config_dir
    first_body = compare(stock_only(bundled("command")), None)
    first = client.put(ROUTE, headers=bearer, json=first_body).json()
    changed = compare(stock_only(bundled("studio")), first["version"])
    descriptor = os.open(home / ".presentation.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        busy = client.put(ROUTE, headers=bearer, json=changed)
    finally:
        os.close(descriptor)
    assert refusal(busy, 429, "presentation_busy")["retryable"] is True
    assert client.get(ROUTE, headers=bearer).json()["version"] == first["version"]
    real = os.fsync

    def failing(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real(descriptor)

    monkeypatch.setattr(os, "fsync", failing)
    uncertain = client.put(ROUTE, headers=bearer, json=changed)
    monkeypatch.undo()
    assert refusal(uncertain, 503, "presentation_write_uncertain")["retryable"] is False
    # GET and compare: the new complete store is visible with a fresh token,
    # so it needs no rewrite, and a blind retry with the old token conflicts.
    state = client.get(ROUTE, headers=bearer).json()
    assert state["status"] == "ready" and state["version"] != first["version"]
    assert state["store"]["namespaces"] == changed["store"]["namespaces"]
    retried = client.put(ROUTE, headers=bearer, json=changed)
    refusal(retried, 409, "presentation_conflict")


def test_api_get_refuses_a_hand_placed_state_over_the_response_bound(tmp_path) -> None:
    service, client, bearer = setup(tmp_path)
    home = service.config.config_dir

    def board(blob):
        value = store(namespace(BOARD, None, config(SHARED, "Blob", {"blob": blob})))
        return {**value, "version": SECOND}

    size = len(p.canonical_json_bytes(board("")))
    raw = plant(home, p.canonical_json_bytes(board("x" * (p.STATE_LIMIT - size))))
    assert len(raw) == p.STATE_LIMIT
    state = p.read_state(home)
    assert state["status"] == "ready"
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) > RESPONSE_LIMIT
    refusal(client.get(ROUTE, headers=bearer), 409, "response_too_large")
    assert stored(home) == raw


def test_registered_shapes_and_openapi_document_the_routes(tmp_path) -> None:
    service, _, _ = setup(tmp_path)
    _, registry = schema_artifacts()
    assert {"PresentationState", "PresentationWrite", "PresentationConfig"} <= {
        entry.contract_id for entry in registry.schemas
    }
    document = json.loads(export_artifacts(service.app)["openapi.json"])
    route = document["paths"][ROUTE]
    assert set(route) == {"get", "put"}
    assert route["put"]["requestBody"]["x-max-body-bytes"] == 1_048_576
    assert "X-CSRF-Token" in {item["name"] for item in route["put"]["parameters"]}
    for method in ("get", "put"):
        content = route[method]["responses"]["200"]["content"]["application/json"]
        assert content["schema"] == {"$ref": "#/components/schemas/PresentationState"}
    home = tmp_path / "shapes"
    home.mkdir(mode=0o700)
    state = p.write_state(home, compare(stock_only(bundled("command")), None))
    assert PresentationState.model_validate(state).model_dump() == state
    exported = p.HELPERS["export"](state, STOCK, bundled("studio"))["result"]
    assert PresentationConfig.model_validate(exported).model_dump() == exported
    body = compare(stock_only(bundled("studio")), state["version"])
    assert PresentationWrite.model_validate(body).model_dump() == body
    unreadable = stock_only(bundled("precision"))
    replace = p.HELPERS["replace_unreadable"](None, "0" * 64, unreadable)
    assert PresentationWrite.model_validate(replace).model_dump() == replace
    for bad in (
        {**body, "extra": 1},
        {**body, "expected_version": "A" * 32},
        {key: value for key, value in body.items() if key != "expected_version"},
        {**replace, "expected_version": None},
    ):
        with pytest.raises(ValidationError):
            PresentationWrite.model_validate(bad)


def wire_cli(monkeypatch, client, before_put=None):
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
            url = urllib.parse.urlsplit(request.full_url)
            assert (url.netloc, url.path, url.query) == ("127.0.0.1:8731", ROUTE, "")
            sent.append((request.get_method(), request.data))
            if request.get_method() == "PUT" and before_put is not None:
                before_put()
            response = client.request(
                request.get_method(),
                url.path,
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


def cli(tmp_path, capsys, *argv):
    from millrace_os.cli import main

    paths = ["--config-dir", str(tmp_path / "config")]
    code = main(["presentation", *argv, *paths, "--state-dir", str(tmp_path / "state")])
    return code, json.loads(capsys.readouterr().out)


def methods(sent):
    return [method for method, _ in sent]


def test_cli_performs_one_get_then_one_compare_put_without_retry(
    tmp_path, monkeypatch, capsys
) -> None:
    _, client, bearer = setup(tmp_path)
    sent = wire_cli(monkeypatch, client)
    assert cli(tmp_path, capsys, "get")[1]["status"] == "absent"
    assert methods(sent) == ["GET"]
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(stock_payload("command")))
    name = "Café ☕ layout"
    create = ("create", *STOCK_NS, "--name", name, "--payload-file", str(payload))
    sent.clear()
    code, created = cli(tmp_path, capsys, *create, "--expected-version", "null")
    assert (code, created["status"]) == (0, "ready")
    assert methods(sent) == ["GET", "PUT"]
    raw = sent[1][1]
    compact = json.dumps(json.loads(raw), ensure_ascii=False, separators=(",", ":"))
    assert raw == compact.encode() and name.encode() in raw
    assert json.loads(raw)["expected_version"] is None
    # Explicit null now conflicts before any PUT; omission sets no precondition.
    sent.clear()
    code, error = cli(tmp_path, capsys, *create, "--expected-version", "null")
    assert (code, error, methods(sent)) == (
        1,
        {"error": "presentation_conflict"},
        ["GET"],
    )
    code, again = cli(tmp_path, capsys, *create)
    assert code == 0
    configs = spaces(again)[STOCK_KEY]["configs"]
    assert [item["name"] for item in configs] == [name, name]  # labels, not keys
    first = configs[0]["id"]
    code, listed = cli(tmp_path, capsys, "list", *STOCK_NS)
    assert (code, listed["version"]) == (0, again["version"])
    assert [item["id"] for item in listed["result"][0]["configs"]] == [
        item["id"] for item in configs
    ]
    code, studio = cli(tmp_path, capsys, "read", *STOCK_NS, "--bundled", "studio")
    assert (code, studio["result"]["kind"], studio["result"]["id"]) == (
        0,
        "bundled",
        "studio",
    )
    # IDs stay inside their explicit namespace; stock never has a null default.
    assert cli(tmp_path, capsys, "set-default", *BOARD_NS, "--none")[0] == 0
    sent.clear()
    rename = ("rename", *BOARD_NS, "--config", first, "--name", "x")
    renamed = cli(tmp_path, capsys, *rename)
    assert renamed == (1, {"error": "presentation_config_not_found"})
    assert methods(sent) == ["GET"]
    nulled = cli(tmp_path, capsys, "set-default", *STOCK_NS, "--none")
    assert nulled == (1, {"error": "invalid_request"})
    code, exported = cli(tmp_path, capsys, "export", *STOCK_NS, "--config", first)
    document = tmp_path / "document.json"
    document.write_text(json.dumps(exported["result"]))
    copied = ("import", "--file", str(document), "--name", "Copied")
    code, imported = cli(tmp_path, capsys, *copied)
    assert code == 0
    assert "Copied" in [item["name"] for item in spaces(imported)[STOCK_KEY]["configs"]]

    # A concurrent writer between GET and PUT: the conflict is returned once.
    def concurrent():
        current = client.get(ROUTE, headers=bearer).json()
        theirs = p.HELPERS["rename"](current, STOCK, first, "Theirs")
        assert client.put(ROUTE, headers=bearer, json=theirs).status_code == 200

    sent = wire_cli(monkeypatch, client, before_put=concurrent)
    mine = ("rename", *STOCK_NS, "--config", first, "--name", "Mine")
    code, error = cli(tmp_path, capsys, *mine)
    assert (code, error["code"]) == (1, "presentation_conflict")
    assert methods(sent) == ["GET", "PUT"]  # returned, never retried
    current = client.get(ROUTE, headers=bearer).json()
    names = {item["id"]: item["name"] for item in spaces(current)[STOCK_KEY]["configs"]}
    assert names[first] == "Theirs"


def test_cli_files_are_strict_and_unreadable_files_need_their_digest(
    tmp_path, monkeypatch, capsys
) -> None:
    service, client, _ = setup(tmp_path)
    sent = wire_cli(monkeypatch, client)
    duplicate = tmp_path / "write.json"
    duplicate.write_text('{"format":"x","format":"y"}')
    with pytest.raises(SystemExit) as exit_:
        cli(tmp_path, capsys, "put", "--file", str(duplicate))
    assert exit_.value.code == 2 and sent == []
    write = tmp_path / "write-ok.json"
    write.write_text(json.dumps(compare(stock_only(bundled("command")), None)))
    code, state = cli(tmp_path, capsys, "put", "--file", str(write))
    assert (code, state["status"], methods(sent)) == (0, "ready", ["PUT"])
    raw = plant(service.config.config_dir, b"{damaged")
    code, state = cli(tmp_path, capsys, "get")
    assert (code, state["status"]) == (0, "invalid")
    replacement = tmp_path / "store.json"
    replacement.write_text(json.dumps(stock_only(bundled("precision"))))
    unreadable = ("replace-unreadable", "--store-file", str(replacement))
    code, error = cli(tmp_path, capsys, *unreadable, "--observed-sha256", "0" * 64)
    assert (code, error["code"]) == (1, "presentation_conflict")
    assert stored(service.config.config_dir) == raw
    digest = hashlib.sha256(raw).hexdigest()
    code, state = cli(tmp_path, capsys, *unreadable, "--observed-sha256", digest)
    assert (code, state["status"]) == (0, "ready")


def test_cli_mirrors_the_fifteen_alternatives_and_refuses_ambiguous_options(
    tmp_path, monkeypatch, capsys
) -> None:
    from millrace_os.cli import PRESENTATION_COMMANDS, main

    def operation(name):
        return {"default": "read_default"}.get(name, name.replace("-", "_"))

    assert len(PRESENTATION_COMMANDS) == 15
    assert {operation(name) for name in PRESENTATION_COMMANDS} == set(p.HELPERS)
    for name in PRESENTATION_COMMANDS:
        with pytest.raises(SystemExit) as exit_:
            main(["presentation", name, "--help"])
        assert exit_.value.code == 0, name
    capsys.readouterr()
    _, client, _ = setup(tmp_path)
    sent = wire_cli(monkeypatch, client)
    payload = tmp_path / "payload.json"
    payload.write_text("{}")
    create = ("create", *STOCK_NS, "--name", "x", "--payload-file", str(payload))
    for argv in (
        (*create, "--config", "a" * 32),  # never an abbreviated --config-dir
        (*create, "--config-d", str(tmp_path / "other")),
        ("list", "--frontend", "stock"),  # the namespace is an explicit pair
        ("read", *STOCK_NS),  # REF is required
        ("read", *STOCK_NS, "--config", "a" * 32, "--bundled", "studio"),
        ("set-default", *STOCK_NS),
        ("delete", *STOCK_NS),
    ):
        with pytest.raises(SystemExit) as exit_:
            cli(tmp_path, capsys, *argv)
        assert exit_.value.code == 2, argv
    assert sent == []
