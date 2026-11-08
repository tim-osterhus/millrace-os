"""Shared presentation preferences: one owner-only canonical JSON store.

The store is `config_dir/presentation.json` (`millrace-os-presentation-store/v2`).
Reads take no lock and never write; a broken file is reported, never repaired.
Writes compare an expected version under a nonblocking `flock`, then replace the
file atomically with a fresh version token. Payloads are presentation data only:
OS validates stock payloads and treats custom payloads as finite JSON objects.

The helpers in `HELPERS` are pure: each takes a GET state and returns either a
write body for `write_state` or a derived read result. Namespaces are always
explicit; names are labels and IDs are the only identity.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import (
    UnsafePathError,
    ensure_directory,
    open_new_file,
    validate_file_target,
)

__all__ = [
    "CONFIG_FORMAT",
    "HELPERS",
    "STATE_CONTRACT",
    "STOCK_FORMAT",
    "STOCK_FRONTEND",
    "STORE_FORMAT",
    "WRITE_FORMAT",
    "PresentationError",
    "bundled_configs",
    "canonical_json_bytes",
    "read_state",
    "strict_loads",
    "validate_store",
    "write_state",
]

STORE_FORMAT = "millrace-os-presentation-store/v2"
WRITE_FORMAT = "millrace-os-presentation-write/v1"
STATE_CONTRACT = "millrace-os-presentation-state/v1"
CONFIG_FORMAT = "millrace-os-presentation-config/v2"
STOCK_FRONTEND = "stock"
STOCK_FORMAT = "millrace-stock-presentation/v1"
FILE_NAME = "presentation.json"
LOCK_NAME = ".presentation.lock"
# The GET envelope, and so every readable store, fits the 1 MiB large-data policy.
STATE_LIMIT = 1_048_576
_CHUNK = 65_536

STATUS = {
    "invalid_request": 422,
    "presentation_config_not_found": 404,
    "presentation_conflict": 409,
    "presentation_integrity_error": 409,
    "presentation_incompatible": 409,
    "presentation_too_large": 413,
    "presentation_busy": 429,
    "presentation_write_uncertain": 503,
}

_CORE_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FRONTEND = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_NOT_PLAIN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_NEWER_STORE = re.compile(r"millrace-os-presentation-store/v([1-9][0-9]*)")
_PANELS = ("planes", "current-work", "graph", "inspector", "history", "revisions")
_SPLITS = ("detail-split", "status-stack", "graph-split")
_STOCK_CHOICES = {
    "preset": ("precision", "command", "studio"),
    "theme": ("system", "light", "dark"),
    "density": ("comfortable", "compact"),
    "arrangement": _SPLITS,
}
_OMITTED: Any = object()


class PresentationError(Exception):
    """A typed refusal; `status` is its HTTP status."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status = STATUS[code]


def _stock(preset, density, arrangement, hidden, ratios):
    return {
        "preset": preset,
        "theme": "system",
        "density": density,
        "arrangement": arrangement,
        "panels": {name: name not in hidden for name in _PANELS},
        "split_ratios": {name: ratios.get(name, 0.5) for name in _SPLITS},
    }


# Immutable bundled stock definitions; saved copies are never rewritten.
_BUNDLED = (
    (
        "precision",
        "Precision",
        _stock("precision", "comfortable", "detail-split", (), {}),
    ),
    (
        "command",
        "Command",
        _stock(
            "command",
            "compact",
            "status-stack",
            ("graph", "inspector", "revisions"),
            {},
        ),
    ),
    (
        "studio",
        "Studio",
        _stock(
            "studio",
            "comfortable",
            "graph-split",
            ("planes", "current-work", "revisions"),
            {"graph-split": 0.65},
        ),
    ),
)


def canonical_json_bytes(value: object) -> bytes:
    """`millrace-canonical-json/v1`: sorted keys, compact UTF-8, no newline.

    Only JSON values are accepted: objects with string keys, arrays, strings,
    finite numbers, booleans and null. Unicode is kept unnormalized; an unpaired
    surrogate refuses. Every refusal is a `ValueError`.
    """
    try:
        _json_value(value)
        text = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except RecursionError as exc:
        raise ValueError("JSON value nests too deeply") from exc
    return text.encode("utf-8")


def _json_value(value: object) -> None:
    if value is None or any(type(value) is kind for kind in (bool, int, str)):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite number")
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("non-string object key")
            _json_value(item)
        return
    raise ValueError("not a JSON value")


def strict_loads(raw: bytes) -> object:
    """Parse UTF-8 JSON, refusing duplicate keys and NaN/Infinity (ValueError)."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(name: str) -> object:
        raise ValueError("nonfinite number")

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except RecursionError as exc:
        raise ValueError("JSON nests too deeply") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _match(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _label(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 256
        and _NOT_PLAIN.search(value) is None
        and not any(char in value for char in "\t\n\r")
    )


def _object(value: object) -> bool:
    """A finite JSON object (custom presentation payloads)."""
    if type(value) is not dict:
        return False
    try:
        canonical_json_bytes(value)
    except ValueError:
        return False
    return True


def _stock_payload(value: object) -> bool:
    keys = {*_STOCK_CHOICES, "panels", "split_ratios"}
    if not (type(value) is dict and value.keys() == keys):
        return False
    panels, ratios = value["panels"], value["split_ratios"]
    return (
        all(
            type(value[key]) is str and value[key] in choices
            for key, choices in _STOCK_CHOICES.items()
        )
        and type(panels) is dict
        and panels.keys() == set(_PANELS)
        and all(type(panels[name]) is bool for name in _PANELS)
        and type(ratios) is dict
        and ratios.keys() == set(_SPLITS)
        and all(
            # The range alone also refuses nonfinite floats; no int is converted.
            type(ratios[name]) in (int, float) and 0 < ratios[name] < 1
            for name in _SPLITS
        )
    )


def _is_stock(frontend_id: str, presentation_format: str) -> bool:
    return (frontend_id, presentation_format) == (STOCK_FRONTEND, STOCK_FORMAT)


def _namespace_key(value: object) -> tuple[str, str]:
    """A strict explicit `{frontend_id, presentation_format}` pair."""
    if not (
        type(value) is dict
        and value.keys() == {"frontend_id", "presentation_format"}
        and _match(_FRONTEND, value["frontend_id"])
        and _match(_FORMAT, value["presentation_format"])
    ):
        raise PresentationError("invalid_request")
    key = (value["frontend_id"], value["presentation_format"])
    if key[0] == STOCK_FRONTEND and key[1] != STOCK_FORMAT:
        raise PresentationError("invalid_request")
    return key


def _payload_valid(key: tuple[str, str], payload: object) -> bool:
    return _stock_payload(payload) if _is_stock(*key) else _object(payload)


def _bundled_ids(key: tuple[str, str]) -> tuple[str, ...]:
    return _STOCK_CHOICES["preset"] if _is_stock(*key) else ("default",)


def _ref(
    key: tuple[str, str], value: object, *, nullable: bool
) -> dict[str, str] | None:
    """`ConfigRef` (or `DefaultRef` when nullable); null only for custom."""
    if value is None and nullable and not _is_stock(*key):
        return None
    if not (type(value) is dict and value.keys() == {"kind", "id"}):
        raise PresentationError("invalid_request")
    if value["kind"] == "user" and _match(_CORE_ID, value["id"]):
        return {"kind": "user", "id": value["id"]}
    if value["kind"] == "bundled" and value["id"] in _bundled_ids(key):
        return {"kind": "bundled", "id": value["id"]}
    raise PresentationError("invalid_request")


def validate_store(store: object, *, versioned: bool) -> dict[str, Any]:
    """A strict v2 store (a write store omits `version`); else `invalid_request`."""
    keys = {"format", "namespaces"} | ({"version"} if versioned else set())
    if not (
        type(store) is dict
        and store.keys() == keys
        and store["format"] == STORE_FORMAT
        and (not versioned or _match(_CORE_ID, store["version"]))
        and type(store["namespaces"]) is list
    ):
        raise PresentationError("invalid_request")
    seen: list[tuple[str, str]] = []
    for namespace in store["namespaces"]:
        if not (
            type(namespace) is dict
            and namespace.keys()
            == {"frontend_id", "presentation_format", "default_ref", "configs"}
            and type(namespace["configs"]) is list
        ):
            raise PresentationError("invalid_request")
        key = _namespace_key(
            {
                "frontend_id": namespace["frontend_id"],
                "presentation_format": namespace["presentation_format"],
            }
        )
        seen.append(key)
        ids: list[str] = []
        for config in namespace["configs"]:
            if not (
                type(config) is dict
                and config.keys() == {"id", "name", "payload"}
                and _match(_CORE_ID, config["id"])
                and _label(config["name"])
                and _payload_valid(key, config["payload"])
            ):
                raise PresentationError("invalid_request")
            ids.append(config["id"])
        if ids != sorted(set(ids)):
            raise PresentationError("invalid_request")  # unsorted or duplicate
        default = _ref(key, namespace["default_ref"], nullable=True)
        if default is not None and default["kind"] == "user" and (
            default["id"] not in ids
        ):
            raise PresentationError("invalid_request")  # unresolved user default
    if seen != sorted(set(seen)):
        raise PresentationError("invalid_request")  # unsorted or duplicate pair
    return store


def bundled_configs() -> list[dict[str, Any]]:
    """Fresh copies of the immutable bundled stock presets for a GET state."""
    return [
        {
            "frontend_id": STOCK_FRONTEND,
            "presentation_format": STOCK_FORMAT,
            "id": preset,
            "name": name,
            "payload": copy.deepcopy(payload),
            "payload_sha256": _sha256(canonical_json_bytes(payload)),
            "bundle_sha256": None,
        }
        for preset, name, payload in _BUNDLED
    ]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _state(status, version=None, file_sha256=None, store=None) -> dict[str, Any]:
    return {
        "contract": STATE_CONTRACT,
        "status": status,
        "version": version,
        "file_sha256": file_sha256,
        "store": store,
        "bundled_configs": bundled_configs(),
        "observed_at": _now(),
    }


def _classify(raw: bytes | None, digest: str) -> dict[str, Any]:
    if raw is None:  # larger than any readable store
        return _state("invalid", file_sha256=digest)
    try:
        value = strict_loads(raw)
    except ValueError:
        return _state("invalid", file_sha256=digest)
    if type(value) is dict and type(value.get("format")) is str:
        newer = _NEWER_STORE.fullmatch(value["format"])
        # The suffix has no leading zero, so every match except 1 and 2 is
        # newer; compared as text, a long suffix needs no integer conversion.
        if newer is not None and newer.group(1) not in {"1", "2"}:
            return _state("incompatible", file_sha256=digest)
    try:
        store = validate_store(value, versioned=True)
    except PresentationError:
        return _state("invalid", file_sha256=digest)
    return _state("ready", store["version"], digest, store)


def read_state(config_dir: Path) -> dict[str, Any]:
    """The GET state. No lock, no write; an unsafe target raises `UnsafePathError`.

    The file is read through one descriptor, so a concurrent replace yields the
    old or the new whole file. A regular file over the store limit is hashed in
    chunks and reported invalid without retaining its bytes.
    """
    target, exists = validate_file_target(Path(config_dir) / FILE_NAME)
    if not exists:
        return _state("absent")
    descriptor = os.open(
        target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise UnsafePathError(f"presentation file is not safe: {target}")
        digest = hashlib.sha256()
        kept: bytearray | None = bytearray()
        while chunk := os.read(descriptor, _CHUNK):
            digest.update(chunk)
            if kept is not None:
                kept.extend(chunk)
                if len(kept) > STATE_LIMIT:
                    kept = None
    finally:
        os.close(descriptor)
    return _classify(None if kept is None else bytes(kept), digest.hexdigest())


def _encoded_size(value: object) -> int:
    """Bytes in the API encoding (compact UTF-8)."""
    text = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return len(text.encode("utf-8"))


def _check_write(write: object) -> tuple[str, object, dict[str, Any]]:
    if type(write) is not dict or write.get("format") != WRITE_FORMAT:
        raise PresentationError("invalid_request")
    if write.get("mode") == "compare" and write.keys() == {
        "format",
        "mode",
        "expected_version",
        "store",
    }:
        expected = write["expected_version"]
        if expected is not None and not _match(_CORE_ID, expected):
            raise PresentationError("invalid_request")
    elif write.get("mode") == "replace_unreadable" and write.keys() == {
        "format",
        "mode",
        "observed_file_sha256",
        "store",
    }:
        expected = write["observed_file_sha256"]
        if not _match(_SHA256, expected):
            raise PresentationError("invalid_request")
    else:
        raise PresentationError("invalid_request")
    return write["mode"], expected, validate_store(write["store"], versioned=False)


def _admit(mode: str, expected: object, current: dict[str, Any]) -> None:
    status = current["status"]
    if mode == "replace_unreadable":
        if status not in {"invalid", "incompatible"} or current["file_sha256"] != (
            expected
        ):
            raise PresentationError("presentation_conflict")
    elif status == "invalid":
        raise PresentationError("presentation_integrity_error")
    elif status == "incompatible":
        raise PresentationError("presentation_incompatible")
    elif current["version"] != expected:  # absent requires null; ready its token
        raise PresentationError("presentation_conflict")


def write_state(config_dir: Path, write: object) -> dict[str, Any]:
    """PUT a `millrace-os-presentation-write/v1` body and return the new GET state.

    Before or at `os.replace`, any failure leaves the previous bytes unchanged
    and removes the temporary file. After the replace, a failed `config_dir`
    fsync raises `presentation_write_uncertain`: the new store may be visible
    and is not rolled back. Read and compare before any retry.
    """
    mode, expected, store = _check_write(write)
    full = {
        "format": STORE_FORMAT,
        "version": uuid.uuid4().hex,
        "namespaces": copy.deepcopy(store["namespaces"]),
    }
    encoded = canonical_json_bytes(full)
    result = _state("ready", full["version"], _sha256(encoded), full)
    if _encoded_size(result) > STATE_LIMIT:
        raise PresentationError("presentation_too_large")
    directory = ensure_directory(config_dir)
    lock, _ = validate_file_target(directory / LOCK_NAME)
    descriptor = os.open(
        lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PresentationError("presentation_busy") from exc
        _admit(mode, expected, read_state(directory))
        _replace(directory, encoded)
    finally:
        os.close(descriptor)  # also releases the lock
    return result


def _replace(directory: Path, encoded: bytes) -> None:
    target, _ = validate_file_target(directory / FILE_NAME)
    fd, temporary = open_new_file(directory / (".presentation-" + uuid.uuid4().hex))
    try:
        try:
            view = memoryview(encoded)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        validate_file_target(target)  # recheck immediately before replacing
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    except OSError as exc:
        raise PresentationError("presentation_write_uncertain") from exc


# Section 7.4.1 helpers. Each takes a GET state first.


def _readable(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The current namespaces; an unreadable file refuses with its typed code."""
    if state["status"] == "invalid":
        raise PresentationError("presentation_integrity_error")
    if state["status"] == "incompatible":
        raise PresentationError("presentation_incompatible")
    store = state["store"]
    return [] if store is None else store["namespaces"]


def _find(namespaces, key):
    for namespace in namespaces:
        if (namespace["frontend_id"], namespace["presentation_format"]) == key:
            return namespace
    return None


def _config(namespace, config_id):
    if namespace is not None:
        for config in namespace["configs"]:
            if config["id"] == config_id:
                return config
    raise PresentationError("presentation_config_not_found")


def _bundled(state, key, bundled_id):
    for config in state["bundled_configs"]:
        if (config["frontend_id"], config["presentation_format"], config["id"]) == (
            *key,
            bundled_id,
        ):
            return config
    return None


def _resolve(state, key, ref) -> dict[str, Any]:
    if ref["kind"] == "user":
        namespace = _find(_readable(state), key)
        config = _config(namespace, ref["id"])
    else:
        config = _bundled(state, key, ref["id"])
        if config is None:
            raise PresentationError("presentation_config_not_found")
    return {
        "frontend_id": key[0],
        "presentation_format": key[1],
        "kind": ref["kind"],
        "id": config["id"],
        "name": config["name"],
        "payload": copy.deepcopy(config["payload"]),
    }


def _derived(state, result) -> dict[str, Any]:
    return {
        "status": state["status"],
        "version": state["version"],
        "file_sha256": state["file_sha256"],
        "result": result,
    }


def _fallback(state, key) -> dict[str, str] | None:
    if _is_stock(*key):
        return {"kind": "bundled", "id": "precision"}
    if _bundled(state, key, "default") is not None:
        return {"kind": "bundled", "id": "default"}
    return None


def _mutate(state, expected_version, change) -> dict[str, Any]:
    """Apply `change` to a copy of the namespaces; return the compare write body."""
    namespaces = copy.deepcopy(_readable(state))
    if expected_version is not _OMITTED:
        if expected_version is not None and not _match(_CORE_ID, expected_version):
            raise PresentationError("invalid_request")
        if expected_version != state["version"]:
            raise PresentationError("presentation_conflict")
    change(namespaces)
    for namespace in namespaces:
        namespace["configs"].sort(key=lambda config: config["id"])
    namespaces.sort(key=lambda item: (item["frontend_id"], item["presentation_format"]))
    store = validate_store(
        {"format": STORE_FORMAT, "namespaces": namespaces}, versioned=False
    )
    return {
        "format": WRITE_FORMAT,
        "mode": "compare",
        "expected_version": state["version"],
        "store": store,
    }


def _namespace(state, namespaces, key, *, create):
    namespace = _find(namespaces, key)
    if namespace is None:
        if not create:
            raise PresentationError("presentation_config_not_found")
        # A new stock namespace defaults to Precision; a custom one to none.
        stock = {"kind": "bundled", "id": "precision"} if _is_stock(*key) else None
        namespace = {
            "frontend_id": key[0],
            "presentation_format": key[1],
            "default_ref": stock,
            "configs": [],
        }
        namespaces.append(namespace)
    return namespace


def _new_id(namespace) -> str:
    taken = {config["id"] for config in namespace["configs"]}
    while (identity := uuid.uuid4().hex) in taken:
        pass
    return identity


def _checked_name(name) -> str:
    if not _label(name):
        raise PresentationError("invalid_request")
    return name


def _checked_payload(key, payload) -> object:
    if not _payload_valid(key, payload):
        raise PresentationError("invalid_request")
    return copy.deepcopy(payload)


def _checked_id(config_id) -> str:
    if not _match(_CORE_ID, config_id):
        raise PresentationError("invalid_request")
    return config_id


def get(state):
    """GET state, unchanged."""
    return state


def put(state, write):
    """PUT body, unchanged (validated before any HTTP request)."""
    _check_write(write)
    return write


def replace_unreadable(state, observed_file_sha256, store):
    write = {
        "format": WRITE_FORMAT,
        "mode": "replace_unreadable",
        "observed_file_sha256": observed_file_sha256,
        "store": store,
    }
    _check_write(write)
    return write


def list_configs(state, namespace=None):
    namespaces = _readable(state)
    if namespace is not None:
        selected = _find(namespaces, _namespace_key(namespace))
        if selected is None:
            raise PresentationError("presentation_config_not_found")
        namespaces = [selected]
    return _derived(
        state,
        [
            {
                "frontend_id": item["frontend_id"],
                "presentation_format": item["presentation_format"],
                "default_ref": copy.deepcopy(item["default_ref"]),
                "configs": [
                    {"id": config["id"], "name": config["name"]}
                    for config in item["configs"]
                ],
            }
            for item in namespaces
        ],
    )


def read(state, namespace, ref):
    key = _namespace_key(namespace)
    _readable(state)  # an unreadable file refuses even a bundled read
    return _derived(state, _resolve(state, key, _ref(key, ref, nullable=False)))


def read_default(state, namespace):
    """Effective default; an absent stock namespace uses bundled Precision."""
    key = _namespace_key(namespace)
    selected = _find(_readable(state), key)
    if selected is not None:
        default = selected["default_ref"]
    elif _is_stock(*key):
        default = {"kind": "bundled", "id": "precision"}
    else:
        default = None
    return _derived(state, None if default is None else _resolve(state, key, default))


def export(state, namespace, ref):
    key = _namespace_key(namespace)
    _readable(state)
    config = _resolve(state, key, _ref(key, ref, nullable=False))
    return _derived(
        state,
        {
            "format": CONFIG_FORMAT,
            "frontend_id": key[0],
            "presentation_format": key[1],
            "name": config["name"],
            "payload": config["payload"],
        },
    )


def create(state, namespace, name, payload, expected_version=_OMITTED):
    key = _namespace_key(namespace)
    entry = {"name": _checked_name(name), "payload": _checked_payload(key, payload)}

    def change(namespaces):
        target = _namespace(state, namespaces, key, create=True)
        target["configs"].append({"id": _new_id(target), **entry})

    return _mutate(state, expected_version, change)


def update(state, namespace, config_id, payload, expected_version=_OMITTED):
    key = _namespace_key(namespace)
    config_id, value = _checked_id(config_id), _checked_payload(key, payload)

    def change(namespaces):
        target = _namespace(state, namespaces, key, create=False)
        _config(target, config_id)["payload"] = value

    return _mutate(state, expected_version, change)


def rename(state, namespace, config_id, name, expected_version=_OMITTED):
    key = _namespace_key(namespace)
    config_id, label = _checked_id(config_id), _checked_name(name)

    def change(namespaces):
        target = _namespace(state, namespaces, key, create=False)
        _config(target, config_id)["name"] = label

    return _mutate(state, expected_version, change)


def duplicate(state, namespace, source, name, expected_version=_OMITTED):
    key = _namespace_key(namespace)
    origin, label = _ref(key, source, nullable=False), _checked_name(name)
    payload = _resolve(state, key, origin)["payload"]

    def change(namespaces):
        # A bundled source may start the namespace; a user source already has it.
        target = _namespace(
            state, namespaces, key, create=origin["kind"] == "bundled"
        )
        entry = {"id": _new_id(target), "name": label, "payload": payload}
        target["configs"].append(entry)

    return _mutate(state, expected_version, change)


def delete(state, namespace, config_id, expected_version=_OMITTED):
    key = _namespace_key(namespace)
    config_id = _checked_id(config_id)

    def change(namespaces):
        target = _namespace(state, namespaces, key, create=False)
        target["configs"].remove(_config(target, config_id))
        if target["default_ref"] == {"kind": "user", "id": config_id}:
            target["default_ref"] = _fallback(state, key)

    return _mutate(state, expected_version, change)


def set_default(state, namespace, default, expected_version=_OMITTED):
    key = _namespace_key(namespace)
    ref = _ref(key, default, nullable=True)

    def change(namespaces):
        target = _namespace(state, namespaces, key, create=True)
        if ref is not None and ref["kind"] == "user":
            _config(target, ref["id"])
        target["default_ref"] = ref

    return _mutate(state, expected_version, change)


def reset(state, namespace, config_id, expected_version=_OMITTED):
    key = _namespace_key(namespace)
    config_id = _checked_id(config_id)

    def change(namespaces):
        config = _config(_namespace(state, namespaces, key, create=False), config_id)
        base_id = config["payload"]["preset"] if _is_stock(*key) else "default"
        base = _bundled(state, key, base_id)
        if base is None:
            raise PresentationError("presentation_incompatible")
        config["payload"] = copy.deepcopy(base["payload"])

    return _mutate(state, expected_version, change)


def import_config(state, config, name=_OMITTED, expected_version=_OMITTED):
    if not (
        type(config) is dict
        and config.keys()
        == {"format", "frontend_id", "presentation_format", "name", "payload"}
        and config["format"] == CONFIG_FORMAT
    ):
        raise PresentationError("invalid_request")
    key = _namespace_key(
        {
            "frontend_id": config["frontend_id"],
            "presentation_format": config["presentation_format"],
        }
    )
    label = _checked_name(config["name"] if name is _OMITTED else name)
    payload = _checked_payload(key, config["payload"])

    def change(namespaces):
        target = _namespace(state, namespaces, key, create=True)
        entry = {"id": _new_id(target), "name": label, "payload": payload}
        target["configs"].append(entry)

    return _mutate(state, expected_version, change)


HELPERS: dict[str, Callable[..., dict[str, Any]]] = {
    "get": get,
    "put": put,
    "replace_unreadable": replace_unreadable,
    "list": list_configs,
    "read": read,
    "read_default": read_default,
    "export": export,
    "create": create,
    "update": update,
    "rename": rename,
    "duplicate": duplicate,
    "delete": delete,
    "set_default": set_default,
    "reset": reset,
    "import": import_config,
}
