"""Complete browser frontends: the stock closure and one selected local closure.

The stock client is one prebuilt `millrace-os-frontend-bundle/v1` closure under
the package resource directory `assets/dashboard/`. That directory is not a
public prefix: the shell is served only at the exact base `/ui/stock/` and each
asset only at `/ui/stock/{manifest path}`. Every shell navigation and every
asset read re-reads and validates the manifest and re-hashes the bytes it
serves, so a changed or partial closure refuses instead of mixing files.

The operator may also select one prebuilt local closure (OS-C1): an explicit
absolute directory with the same manifest, keyed by the SHA-256 of its
canonical manifest. It is validated in place, never copied, installed or run
by the service, and served only at `/ui/local/{bundle_sha256}/` while it is
the effective selection. The desired selection is `config_dir/frontend.json`;
every navigation rereads it and revalidates the whole closure, and any failure
makes stock effective without changing what was selected.

Nothing here imports Core, Plus or MCP. Only the operator's explicit
validation and selection name a filesystem path; no shell or asset request
does.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import os
import re
import stat
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from .contracts import strict_json
from .paths import (
    UnsafePathError,
    ensure_directory,
    open_new_file,
    validate_ancestors,
    validate_file_target,
)
from .presentation import (
    PresentationError,
    canonical_json_bytes,
    state_fits,
    store_lock,
)

MANIFEST_FORMAT = "millrace-os-frontend-bundle/v1"
MANIFEST_NAME = "millrace-os-frontend.json"
STOCK_ID = "stock"
STOCK_BASE = "/ui/stock/"
LOCAL_PREFIX = "/ui/local/"
LAUNCHER = "/client"
TARGET_KEYS = ("home", "instance", "run")
STATE_CONTRACT = "millrace-os-frontend-state/v1"
VALIDATION_CONTRACT = "millrace-os-frontend-validation/v1"
SELECTION_FORMAT = "millrace-os-frontend-selection/v1"
SELECTION_NAME = "frontend.json"
SELECTION_LOCK = ".frontend.lock"
# A selected local directory path, in characters, so the PUT body fits the
# default 4,096-byte request policy (OS-G0 9).
PATH_LIMIT = 3968
_CHUNK = 65_536
# The reviewed normal-document policy for exact shell bases only. API and
# recovery responses keep the middleware's existing policy.
SHELL_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "style-src-elem 'self'; style-src-attr 'none'; connect-src 'self'; "
    "img-src 'self'; font-src 'self'; worker-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)
# OS-G0 9 extension allowlist; `.html` is admitted only for the entry.
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/vnd.microsoft.icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}
_MANIFEST_KEYS = {
    "format",
    "frontend_id",
    "frontend_version",
    "entry",
    "assets",
    "api_version",
    "required_contracts",
    "required_capabilities",
    "presentation_format",
    "default_presentation",
}
_COMPONENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_FRONTEND_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OPERATION = re.compile(r"[a-z][a-z_]*(\.[a-z][a-z_]*)+")
_CONTRACT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}")
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}")
CORE_ID = re.compile(r"[0-9a-f]{32}")
_LOCAL_BASE = re.compile(r"/ui/local/([0-9a-f]{64})/")
_LOCAL_LEGACY = re.compile(r"/ui/local/[0-9a-f]{64}/instances/")
_NOT_PATH = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
# A local base that is not the effective selection answers with this fixed
# page: never stock HTML, and only the permanent routes that always work.
UNAVAILABLE_PAGE = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>Frontend unavailable</title></head>\n<body><main>"
    "<h1>This frontend is not available</h1>"
    "<p>frontend_unavailable: this is not the selected frontend, or its files "
    "are missing, changed or incompatible. Nothing was replayed or changed.</p>"
    '<ul><li><a href="/ui/stock/">Open the stock frontend</a></li>'
    '<li><a href="/client">Frontend launcher</a></li>'
    '<li><a href="/recovery">Recovery</a></li></ul></main></body></html>\n'
)


class FrontendError(Exception):
    """A typed refusal; `status` is its HTTP status."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class ManifestError(ValueError):
    """The manifest or one listed file does not match the bundle contract."""


@dataclass(frozen=True)
class Asset:
    path: str
    sha256: str
    media_type: str


def _match(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _asset_path(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 512:
        raise ManifestError("asset path")
    parts = value.split("/")
    if not all(_COMPONENT.fullmatch(part) for part in parts):
        raise ManifestError("asset path")
    # `{base}instances/...` is a recognized redirect form, never an asset.
    if parts[0] == "instances":
        raise ManifestError("asset path")
    return value


def validate_manifest(
    raw: bytes,
    *,
    registry: Mapping[str, str],
    available_operations: Iterable[str],
    stock: bool,
) -> tuple[dict[str, object], dict[str, Asset]]:
    """Strictly validate one manifest; return it and its assets by path.

    `registry` maps current contract IDs to schema digests (version 1).
    Compatibility is positive: every required contract digest and capability
    must be currently available. `stock` admits the reserved ID `stock`, which
    only the packaged manifest may claim.
    """
    try:
        manifest = strict_json(raw)
    except ValueError as exc:
        raise ManifestError("manifest json") from exc
    if not (type(manifest) is dict and manifest.keys() == _MANIFEST_KEYS):
        raise ManifestError("manifest keys")
    if manifest["format"] != MANIFEST_FORMAT or manifest["api_version"] != "v1":
        raise ManifestError("manifest format")
    frontend_id = manifest["frontend_id"]
    if not _match(_FRONTEND_ID, frontend_id) or (frontend_id == STOCK_ID) != stock:
        raise ManifestError("frontend id")
    if not _match(_VERSION, manifest["frontend_version"]):
        raise ManifestError("frontend version")
    if manifest["entry"] != "index.html":
        raise ManifestError("entry")
    assets: dict[str, Asset] = {}
    listed = manifest["assets"]
    if type(listed) is not list:
        raise ManifestError("assets")
    for item in listed:
        if not (type(item) is dict and item.keys() == {"path", "sha256", "media_type"}):
            raise ManifestError("asset keys")
        path = _asset_path(item["path"])
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        expected = MEDIA_TYPES.get(suffix)
        if expected is None or item["media_type"] != expected:
            raise ManifestError("media type")
        if suffix == ".html" and path != "index.html":
            raise ManifestError("html outside entry")
        if not _match(_SHA256, item["sha256"]):
            raise ManifestError("sha256")
        assets[path] = Asset(path, item["sha256"], expected)
    paths = [item["path"] for item in listed]
    folded = [path.lower() for path in paths]
    if paths != sorted(paths) or len(set(folded)) != len(folded):
        raise ManifestError("asset order")
    if "index.html" not in assets:
        raise ManifestError("entry not listed")
    contracts = manifest["required_contracts"]
    if type(contracts) is not list:
        raise ManifestError("required contracts")
    for item in contracts:
        if not (
            type(item) is dict
            and item.keys() == {"contract_id", "version", "sha256"}
            and _match(_CONTRACT_ID, item["contract_id"])
            and type(item["version"]) is int
            and item["version"] == 1
            and _match(_SHA256, item["sha256"])
            and item["contract_id"] in registry
            and registry[item["contract_id"]] == item["sha256"]
        ):
            raise ManifestError("required contract")
    operations = set(available_operations)
    capabilities = manifest["required_capabilities"]
    if not (
        type(capabilities) is list
        and all(_match(_OPERATION, item) for item in capabilities)
        and set(capabilities) <= operations
    ):
        raise ManifestError("required capability")
    presentation_format = manifest["presentation_format"]
    default_presentation = manifest["default_presentation"]
    if (presentation_format is None) != (default_presentation is None):
        raise ManifestError("presentation pair")
    if presentation_format is not None and not (
        _match(_FORMAT, presentation_format) and type(default_presentation) is dict
    ):
        raise ManifestError("presentation")
    return manifest, assets


class StockFrontend:
    """Exact reads of the packaged stock closure; no caching, no discovery."""

    def __init__(
        self, *, registry: Mapping[str, str], available_operations: Iterable[str]
    ) -> None:
        self.registry = dict(registry)
        self.operations = frozenset(available_operations)

    def _resource(self, path: str) -> bytes:
        """One regular file inside the packaged closure; never a symlink."""
        resource = files("millrace_os")
        for part in ("assets", "dashboard", *path.split("/")):
            resource = resource.joinpath(part)
            # Filesystem installs expose real paths; refuse every symlinked
            # component so a listed name cannot alias a file elsewhere.
            if hasattr(resource, "is_symlink") and resource.is_symlink():
                raise OSError("symlinked frontend resource")
        if hasattr(resource, "lstat"):
            if not stat.S_ISREG(resource.lstat().st_mode):
                raise OSError("nonregular frontend resource")
        elif not resource.is_file():
            raise OSError("nonregular frontend resource")
        return resource.read_bytes()

    def _assets(self) -> dict[str, Asset]:
        try:
            raw = self._resource(MANIFEST_NAME)
            _, assets = validate_manifest(
                raw,
                registry=self.registry,
                available_operations=self.operations,
                stock=True,
            )
        except (OSError, ManifestError) as exc:
            raise FrontendError("frontend_unavailable", 503) from exc
        return assets

    def read(self, path: str) -> tuple[bytes, str]:
        """The exact listed bytes and media type; unlisted paths are 404."""
        assets = self._assets()
        asset = assets.get(path)
        if asset is None:
            raise FrontendError("route_not_found", 404)
        try:
            content = self._resource(path)
        except OSError as exc:
            raise FrontendError("frontend_unavailable", 503) from exc
        if hashlib.sha256(content).hexdigest() != asset.sha256:
            raise FrontendError("frontend_unavailable", 503)
        return content, asset.media_type

    def shell(self) -> bytes:
        return self.read("index.html")[0]


# --- the operator-selected local closure (OS-C1) ---


def bundle_root(path: object) -> Path:
    """An explicit absolute directory path with no lexical alias.

    `.`/`..` components, repeated or trailing slashes, a leading `//`, control
    characters and unpaired surrogates refuse `invalid_request`, so one
    directory has one spelling. Symlinks are refused when it is read.
    """
    if not (
        type(path) is str
        and 1 <= len(path) <= PATH_LIMIT
        and path.startswith("/")
        and not path.startswith("//")
        and _NOT_PATH.search(path) is None
        and os.path.normpath(path) == path
    ):
        raise FrontendError("invalid_request", 422)
    return Path(path)


def _read(root: Path, relative: str, *, keep: bool) -> tuple[str, bytes | None]:
    """The SHA-256 (and, if `keep`, the bytes) of one declared bundle file.

    Every lexical ancestor must be a real directory owned by this user or root
    and not writable by others (`paths.validate_ancestors`); the file itself
    must be such a regular file, opened without following a symlink.
    """
    target = root.joinpath(*relative.split("/"))
    validate_ancestors(target.parent)
    if stat.S_ISLNK(os.lstat(target).st_mode):
        raise UnsafePathError(f"frontend file must not be a symbolic link: {target}")
    descriptor = os.open(
        target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022
        ):
            raise UnsafePathError(f"frontend file is not safe: {target}")
        digest = hashlib.sha256()
        kept = bytearray() if keep else None
        while chunk := os.read(descriptor, _CHUNK):
            digest.update(chunk)
            if kept is not None:
                kept.extend(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), None if kept is None else bytes(kept)


@contextlib.contextmanager
def _bundle_files() -> Iterator[None]:
    """Unsafe paths keep their typed refusal; other file failures are
    `frontend_unavailable`."""
    try:
        yield
    except UnsafePathError:
        raise
    except OSError as exc:
        raise FrontendError("frontend_unavailable", 503) from exc


def read_manifest(
    root: Path, *, registry: Mapping[str, str], operations: Iterable[str]
) -> tuple[dict[str, object], dict[str, Asset], str]:
    """The validated local manifest, its assets and its bundle key.

    The key is the SHA-256 of the manifest's `millrace-canonical-json/v1`
    bytes (sorted keys, compact UTF-8), so a cosmetic re-encoding keeps it.
    """
    with _bundle_files():
        raw = _read(root, MANIFEST_NAME, keep=True)[1] or b""
    try:
        manifest, assets = validate_manifest(
            raw, registry=registry, available_operations=operations, stock=False
        )
        digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    except ValueError as exc:
        raise FrontendError("frontend_incompatible", 409) from exc
    return manifest, assets, digest


def verify_closure(
    root: Path, assets: Mapping[str, Asset], want: str | None = None
) -> bytes | None:
    """Re-hash every declared asset; return the bytes of `want` if listed.

    A missing or unreadable file is `frontend_unavailable`; bytes that differ
    from their declared digest are `frontend_conflict`. Undeclared files in
    the directory are ignored and never served.
    """
    content = None
    for asset in assets.values():
        with _bundle_files():
            digest, data = _read(root, asset.path, keep=asset.path == want)
        if digest != asset.sha256:
            raise FrontendError("frontend_conflict", 409)
        if asset.path == want:
            content = data
    return content


def bundled_default(
    manifest: Mapping[str, object], bundle_sha256: str
) -> dict[str, object] | None:
    """The bundle's immutable presentation default as a bundled config, if any.

    OS keeps the custom payload as finite JSON; only its own client reads it.
    """
    if manifest["presentation_format"] is None:
        return None
    payload = manifest["default_presentation"]
    return {
        "frontend_id": manifest["frontend_id"],
        "presentation_format": manifest["presentation_format"],
        "id": "default",
        "name": "Default",
        "payload": copy.deepcopy(payload),
        "payload_sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        "bundle_sha256": bundle_sha256,
    }


def _choice(value: object) -> dict[str, str] | None:
    """A strict desired choice: stock, or a canonical local path and digest."""
    if type(value) is not dict:
        return None
    if value.keys() == {"kind"} and value["kind"] == "stock":
        return {"kind": "stock"}
    if not (
        value.keys() == {"kind", "path", "bundle_sha256"}
        and value["kind"] == "local"
        and _match(_SHA256, value["bundle_sha256"])
    ):
        return None
    try:
        bundle_root(value["path"])
    except FrontendError:
        return None
    return {
        "kind": "local",
        "path": value["path"],
        "bundle_sha256": value["bundle_sha256"],
    }


@dataclass(frozen=True)
class Selection:
    """The saved selection: `absent` (stock), `ready` or `invalid`."""

    status: str
    version: str | None
    desired: dict[str, str] | None


def read_selection(config_dir: Path) -> Selection:
    """The saved selection. No lock, no write; unsafe targets raise.

    One descriptor read, so a concurrent replace yields the old or the new
    whole file. A malformed record is `invalid` and is never rewritten
    implicitly.
    """
    target, exists = validate_file_target(Path(config_dir) / SELECTION_NAME)
    if not exists:
        return Selection("absent", None, {"kind": "stock"})
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
            raise UnsafePathError(f"frontend selection is not safe: {target}")
        raw = bytearray()
        while chunk := os.read(descriptor, _CHUNK):
            raw.extend(chunk)
    finally:
        os.close(descriptor)
    try:
        record = strict_json(bytes(raw))
    except ValueError:
        return Selection("invalid", None, None)
    if not (
        type(record) is dict
        and record.keys() == {"format", "version", "desired"}
        and record["format"] == SELECTION_FORMAT
        and _match(CORE_ID, record["version"])
        and _choice(record["desired"]) is not None
    ):
        return Selection("invalid", None, None)
    return Selection("ready", record["version"], _choice(record["desired"]))


def _commit(directory: Path, encoded: bytes) -> None:
    """Write the selection with the presentation store's replace pattern.

    Before or at `os.replace` a failure leaves the previous bytes and removes
    the temporary file. A failed `config_dir` fsync after the replace raises
    `frontend_write_uncertain`: the new selection may be visible; read and
    compare before any retry.
    """
    target, _ = validate_file_target(directory / SELECTION_NAME)
    fd, temporary = open_new_file(directory / (".frontend-" + uuid.uuid4().hex))
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
        raise FrontendError("frontend_write_uncertain", 503) from exc


@dataclass(frozen=True)
class Resolution:
    """One navigation's effective choice; `sha256` is None for stock.

    `identity` is known whenever the manifest at the selected path still has
    the selected digest, even if a declared file then failed.
    """

    selection: Selection | None
    sha256: str | None = None
    manifest: dict[str, object] | None = None
    assets: Mapping[str, Asset] | None = None
    identity: dict[str, object] | None = None
    reason: str | None = None
    content: bytes | None = None

    @property
    def base(self) -> str:
        return STOCK_BASE if self.sha256 is None else local_base(self.sha256)


class LocalFrontend:
    """The selected local closure, reread and revalidated on every use.

    No cache: each shell navigation, asset read and state read rereads the
    selection, the manifest and every declared file. `available` returns the
    currently available operation IDs; it runs only when a local closure is
    selected. Any failure makes stock effective and keeps the desired record.
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        registry: Mapping[str, str],
        available: Callable[[], Iterable[str]],
    ) -> None:
        self.config_dir = Path(config_dir)
        self.registry = dict(registry)
        self.available = available

    def _manifest(self, root: Path) -> tuple[dict[str, object], dict[str, Asset], str]:
        operations = self.available()
        return read_manifest(root, registry=self.registry, operations=operations)

    def _effective(self, selection: Selection, want: str | None = None) -> Resolution:
        if selection.status == "invalid":
            return Resolution(selection, reason="frontend_selection_invalid")
        desired = selection.desired
        if desired is None or desired["kind"] == "stock":
            return Resolution(selection)
        root = Path(desired["path"])
        try:
            manifest, assets, digest = self._manifest(root)
        except UnsafePathError:
            return Resolution(selection, reason="unsafe_path")
        except FrontendError as exc:
            return Resolution(selection, reason=exc.code)
        if digest != desired["bundle_sha256"]:
            # Changed manifest bytes: it is no longer the selected bundle.
            return Resolution(selection, reason="frontend_conflict")
        identity = {
            "frontend_id": manifest["frontend_id"],
            "frontend_version": manifest["frontend_version"],
            "bundle_sha256": digest,
        }
        try:
            content = verify_closure(root, assets, want)
        except UnsafePathError:
            return Resolution(selection, identity=identity, reason="unsafe_path")
        except FrontendError as exc:
            return Resolution(selection, identity=identity, reason=exc.code)
        return Resolution(selection, digest, manifest, assets, identity, None, content)

    def resolve(self, want: str | None = None) -> Resolution:
        """The effective choice for one navigation; any failure means stock."""
        try:
            selection = read_selection(self.config_dir)
        except UnsafePathError:
            return Resolution(None, reason="unsafe_path")
        return self._effective(selection, want)

    def state(self) -> dict[str, object]:
        """`millrace-os-frontend-state/v1`. Reads only; an unsafe selection
        target raises `UnsafePathError` as the presentation GET does."""
        selection = read_selection(self.config_dir)
        resolution = self._effective(selection)
        return {
            "contract": STATE_CONTRACT,
            "status": selection.status,
            "version": selection.version,
            "desired": selection.desired,
            "effective": {
                "kind": "stock" if resolution.sha256 is None else "local",
                "base": resolution.base,
                "bundle_sha256": resolution.sha256,
            },
            "manifest": resolution.identity,
            "reason": resolution.reason,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def validate(self, path: object) -> dict[str, object]:
        """Read and verify one explicit directory; select or install nothing."""
        root = bundle_root(path)
        manifest, assets, digest = self._manifest(root)
        verify_closure(root, assets)
        return {
            "contract": VALIDATION_CONTRACT,
            "path": path,
            "bundle_sha256": digest,
            "manifest": manifest,
        }

    def write(
        self, expected_version: str | None, desired: Mapping[str, object]
    ) -> dict[str, object]:
        """Compare the version, revalidate the exact bytes, then commit.

        Under a nonblocking lock: a held lock, a changed or unreadable record,
        or a version other than `expected_version` (null: none saved) refuses
        `frontend_conflict` without writing. A local choice must validate to
        exactly its `bundle_sha256` with every listed file unchanged (else
        `frontend_conflict`). If it carries a presentation default, the size
        check and the commit also hold the presentation store's lock, which
        every store write holds while it resolves this selection: a busy store
        refuses `frontend_conflict`, and a GET over its 1 MiB policy refuses
        `frontend_incompatible`. Nothing else is changed.
        """
        choice = _choice(desired)
        if choice is None:
            raise FrontendError("invalid_request", 422)
        directory = ensure_directory(self.config_dir)
        lock, _ = validate_file_target(directory / SELECTION_LOCK)
        descriptor = os.open(
            lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FrontendError("frontend_conflict", 409) from exc
            current = read_selection(directory)
            if current.status == "invalid" or current.version != expected_version:
                raise FrontendError("frontend_conflict", 409)
            default: dict[str, object] | None = None
            if choice["kind"] == "local":
                root = bundle_root(choice["path"])
                manifest, assets, digest = self._manifest(root)
                if digest != choice["bundle_sha256"]:
                    raise FrontendError("frontend_conflict", 409)
                verify_closure(root, assets)
                default = bundled_default(manifest, digest)
            record = {
                "format": SELECTION_FORMAT,
                "version": uuid.uuid4().hex,
                "desired": choice,
            }
            encoded = canonical_json_bytes(record)
            if default is None:
                _commit(directory, encoded)  # a GET can only shrink
            else:
                try:
                    with store_lock(directory):
                        if not state_fits(directory, default):
                            raise FrontendError("frontend_incompatible", 409)
                        _commit(directory, encoded)
                except PresentationError as exc:  # a store write holds it
                    raise FrontendError("frontend_conflict", 409) from exc
        finally:
            os.close(descriptor)  # also releases the lock
        return self.state()

    def base(self) -> str:
        """The effective exact shell base."""
        return self.resolve().base

    def presentation_default(self) -> dict[str, object] | None:
        """The effective local closure's presentation default, if any."""
        resolution = self.resolve()
        if resolution.sha256 is None or resolution.manifest is None:
            return None
        return bundled_default(resolution.manifest, resolution.sha256)

    def shell(self, bundle_sha256: str) -> bytes | None:
        """The effective entry document, or None if this base is not effective."""
        resolution = self.resolve("index.html")
        if resolution.sha256 != bundle_sha256:
            return None
        return resolution.content

    def read(self, bundle_sha256: str, path: str) -> tuple[bytes, str]:
        """Exact listed bytes of the effective closure; never another digest."""
        resolution = self.resolve(path)
        if resolution.sha256 != bundle_sha256 or resolution.assets is None:
            raise FrontendError("frontend_unavailable", 503)
        asset = resolution.assets.get(path)
        if asset is None:
            raise FrontendError("route_not_found", 404)
        if resolution.content is None:  # a listed asset's bytes were kept
            raise FrontendError("frontend_unavailable", 503)
        return resolution.content, asset.media_type


def local_base(bundle_sha256: str) -> str:
    return LOCAL_PREFIX + bundle_sha256 + "/"


def is_shell(path: str) -> bool:
    """The two exact shell-base patterns that take the normal-document policy."""
    return path == STOCK_BASE or _LOCAL_BASE.fullmatch(path) is not None


def query_keys(path: str) -> frozenset[str] | None:
    """Query keys admitted on shell and redirect paths; None for other paths."""
    if path in {"/", "/ui/stock"} or is_shell(path):
        return frozenset(TARGET_KEYS)
    if legacy_target(path) is not None:
        return frozenset({"home"})
    return None


def legacy_target(path: str) -> tuple[str, str | None] | None:
    """`(instance, run)` for `/instances/...` and `{base}instances/...` forms."""
    prefixes = ["/instances/", STOCK_BASE + "instances/"]
    local = _LOCAL_LEGACY.match(path)
    if local is not None:
        prefixes.append(local[0])
    for prefix in prefixes:
        if path.startswith(prefix):
            parts = path[len(prefix) :].split("/")
            if len(parts) == 1:
                return parts[0], None
            if len(parts) == 3 and parts[1] == "runs":
                return parts[0], parts[2]
    return None


def shell_target(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Validate `home`, `instance`, `run` exactly as the browser client does."""
    pairs = list(items)
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or not set(keys) <= set(TARGET_KEYS):
        raise FrontendError("invalid_request", 422)
    target = dict(pairs)
    if "home" in target and not _match(IDENTIFIER, target["home"]):
        raise FrontendError("invalid_request", 422)
    for key, needs in (("instance", "home"), ("run", "instance")):
        if key in target and (
            needs not in target or not _match(CORE_ID, target[key])
        ):
            raise FrontendError("invalid_request", 422)
    return {key: target[key] for key in TARGET_KEYS if key in target}


def shell_location(target: Mapping[str, str], base: str = STOCK_BASE) -> str:
    """An exact shell base plus validated query; never a fragment."""
    query = urllib.parse.urlencode(
        [(key, target[key]) for key in TARGET_KEYS if key in target]
    )
    return base + ("?" + query if query else "")
