"""The packaged stock frontend: manifest closure, exact assets and shell targets.

The stock client is one prebuilt `millrace-os-frontend-bundle/v1` closure under
the package resource directory `assets/dashboard/`. That directory is not a
public prefix: the shell is served only at the exact base `/ui/stock/` and each
asset only at `/ui/stock/{manifest path}`. Every shell navigation and every
asset read re-reads and validates the manifest and re-hashes the bytes it
serves, so a changed or partial closure refuses instead of mixing files.

Nothing here imports Core, Plus or MCP, and no request selects a filesystem
path. A trusted replacement frontend (OS-C1) reuses `validate_manifest`.
"""

from __future__ import annotations

import hashlib
import re
import stat
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.resources import files

from .contracts import strict_json

MANIFEST_FORMAT = "millrace-os-frontend-bundle/v1"
MANIFEST_NAME = "millrace-os-frontend.json"
STOCK_ID = "stock"
STOCK_BASE = "/ui/stock/"
TARGET_KEYS = ("home", "instance", "run")
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


def query_keys(path: str) -> frozenset[str] | None:
    """Query keys admitted on shell and redirect paths; None for other paths."""
    if path in {"/", "/ui/stock", STOCK_BASE}:
        return frozenset(TARGET_KEYS)
    if legacy_target(path) is not None:
        return frozenset({"home"})
    return None


def legacy_target(path: str) -> tuple[str, str | None] | None:
    """`(instance, run)` for `/instances/...` and `{base}instances/...` forms."""
    for prefix in ("/instances/", STOCK_BASE + "instances/"):
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


def shell_location(target: Mapping[str, str]) -> str:
    """The exact stock base plus validated query; never a fragment."""
    query = urllib.parse.urlencode(
        [(key, target[key]) for key in TARGET_KEYS if key in target]
    )
    return STOCK_BASE + ("?" + query if query else "")
