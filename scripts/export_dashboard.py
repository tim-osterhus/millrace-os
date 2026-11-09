"""Export the built stock frontend into the package's exact asset closure.

Maintainer-only. After `npm --prefix frontend run build`, run:

    python -I scripts/export_dashboard.py           # validate, then write
    python -I scripts/export_dashboard.py --check   # validate and compare only

The input is the owned Vite staging directory `frontend/dist/`, its manifest
`.vite/manifest.json` and the `module-closure.json` recorded by the build. The
output is the fixed package directory `src/millrace_os/assets/dashboard/`:
`index.html`, the hashed JS/CSS closure, `THIRD_PARTY_NOTICES.txt`,
`dependency-inventory.json` and the stock `millrace-os-frontend.json`.

The exporter refuses anything it cannot attribute: an emitted module outside
`frontend/src` that is not a locked production package or a known Vite/Rolldown
helper, a module record that does not cover every emitted JS chunk exactly
once, nonlocal or inline HTML, unlisted staging output, symlinks, source maps
and lock entries without registry provenance. It never runs Node, never fetches
and never touches a path other than its fixed staging input and owned output.
Standard library only, so it needs no product import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
STAGING = FRONTEND / "dist"
OUTPUT = ROOT / "src" / "millrace_os" / "assets" / "dashboard"
REGISTRY = ROOT / "src" / "millrace_os" / "schemas" / "registry.json"
BASE = "/ui/stock/"
MANIFEST_NAME = "millrace-os-frontend.json"
NOTICES = "THIRD_PARTY_NOTICES.txt"
INVENTORY = "dependency-inventory.json"
REGISTRY_URL = "https://registry.npmjs.org/"
# Envelopes the stock client consumes; the manifest pins their exact digests.
STOCK_CONTRACTS = (
    "ClientCapabilities",
    "DemoContext",
    "Error",
    "ExchangeInput",
    "PresentationState",
    "PresentationWrite",
    "RouterHomes",
    "RouterView",
    "SessionContext",
    "SessionOutput",
    "WorkflowOutput",
    "WorkflowOutputs",
)
# Only operations that need no optional component, so stock always serves.
STOCK_CAPABILITIES = (
    "client.capabilities.read",
    "client.presentation.read",
    "client.presentation.write",
)
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
# Build-tool runtime code that the bundler itself writes into chunks.
TOOL_HELPERS = (
    ("\0vite/", "vite"),
    ("vite/", "vite"),
    ("\0rolldown", "rolldown"),
    ("rolldown:", "rolldown"),
)
TOOL_PACKAGES = frozenset({"vite", "rolldown"})
STAGING_EXTRAS = frozenset({"index.html", ".vite/manifest.json", "module-closure.json"})
_COMPONENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_PACKAGE_DIR = re.compile(r"^((?:node_modules/(?:@[^/]+/)?[^/]+/)+)")
_LICENSE_FILE = re.compile(r"(?i)^(licen[cs]e|copying|notice)(\.[a-z0-9]+)?$")


class ExportError(Exception):
    """The staged build cannot become the reviewed package closure."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=1
    ).encode("utf-8") + b"\n"


def _strict_load(path: Path) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ExportError(f"duplicate key {key!r} in {path.name}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, ValueError) as exc:
        raise ExportError(f"unreadable JSON {path}: {exc}") from exc


def _regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ExportError(f"not a regular file: {path}")
    return path.read_bytes()


def _relative_asset(value: str) -> str:
    parts = value.split("/")
    if not (1 <= len(value) <= 512 and all(_COMPONENT.fullmatch(p) for p in parts)):
        raise ExportError(f"unsafe asset path {value!r}")
    if parts[0] == "instances" or value.endswith(".map"):
        raise ExportError(f"reserved or source-map path {value!r}")
    return value


def _media(path: str) -> str:
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    media = MEDIA_TYPES.get(suffix)
    if media is None or (suffix == ".html" and path != "index.html"):
        raise ExportError(f"unsupported asset type {path!r}")
    return media


def _staged_files() -> set[str]:
    if STAGING.is_symlink() or not STAGING.is_dir():
        raise ExportError("frontend/dist is missing; run the frontend build first")
    found = set()
    for path in STAGING.rglob("*"):
        if path.is_symlink():
            raise ExportError(f"symlink in staging: {path}")
        if path.is_file():
            found.add(path.relative_to(STAGING).as_posix())
    return found


def _closure(manifest: dict) -> list[str]:
    """Entry, imported and lazily imported chunks plus their CSS and assets."""
    entries = [key for key, item in manifest.items() if item.get("isEntry")]
    if entries != ["index.html"]:
        raise ExportError(f"expected the single index.html entry, found {entries}")
    seen, files, stack = set(), set(), ["index.html"]
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        item = manifest.get(key)
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ExportError(f"manifest reference without a file: {key!r}")
        files.add(_relative_asset(item["file"]))
        for name in item.get("css", []) + item.get("assets", []):
            files.add(_relative_asset(name))
        stack.extend(item.get("imports", []) + item.get("dynamicImports", []))
    return sorted(files)


class _ShellReferences(HTMLParser):
    """Collect script/link targets; refuse inline code, styles and handlers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.open_script = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for name, _ in attrs:
            if name == "style" or name.startswith("on"):
                raise ExportError(f"inline {name} attribute in index.html")
        if tag in {"style", "base", "iframe", "object", "embed", "form"}:
            raise ExportError(f"<{tag}> is not allowed in index.html")
        if tag == "script":
            if not values.get("src"):
                raise ExportError("inline <script> in index.html")
            self.references.append(values["src"])
            self.open_script = True
        if tag == "link":
            if values.get("href"):
                self.references.append(values["href"])
        if tag in {"img", "source", "video", "audio"} and values.get("src"):
            self.references.append(values["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.open_script = False

    def handle_data(self, data):
        if self.open_script and data.strip():
            raise ExportError("inline script text in index.html")


def _shell_references(html: bytes, closure: list[str]) -> None:
    parser = _ShellReferences()
    parser.feed(html.decode("utf-8"))
    parser.close()
    for reference in parser.references:
        if not reference.startswith(BASE) or reference.startswith(BASE + "/"):
            raise ExportError(f"nonlocal or relative-root reference {reference!r}")
        if reference[len(BASE) :] not in closure:
            raise ExportError(f"index.html references unlisted {reference!r}")


def _lock() -> tuple[dict, bytes]:
    raw = _regular(FRONTEND / "package-lock.json")
    lock = _strict_load(FRONTEND / "package-lock.json")
    if not isinstance(lock, dict) or lock.get("lockfileVersion") != 3:
        raise ExportError("package-lock.json must be lockfileVersion 3")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or "" not in packages:
        raise ExportError("package-lock.json has no packages map")
    for key, entry in packages.items():
        if key == "":
            continue
        if entry.get("link") or not str(entry.get("resolved", "")).startswith(
            REGISTRY_URL
        ):
            raise ExportError(f"{key} is not a registry package")
        if not str(entry.get("integrity", "")).startswith("sha512-"):
            raise ExportError(f"{key} has no sha512 integrity")
    return packages, raw


def _resolve(packages: dict, parent: str, name: str) -> str | None:
    """npm's nested resolution: nearest `node_modules/name` above `parent`."""
    base = parent
    while True:
        candidate = (base + "/" if base else "") + "node_modules/" + name
        if candidate in packages:
            return candidate
        if not base:
            return None
        cut = base.rfind("/node_modules/")
        base = base[:cut] if cut >= 0 else ""


def _attribute(module_id: str) -> tuple[str, str]:
    """`("first-party"|"package"|"tool", key)` for one emitted module."""
    clean = module_id.split("?", 1)[0]
    for prefix, tool in TOOL_HELPERS:
        if clean.startswith(prefix):
            return "tool", tool
    path = Path(clean)
    if clean.startswith("\0") or not path.is_absolute():
        raise ExportError(f"unattributed virtual module {module_id!r}")
    try:
        relative = path.resolve().relative_to(FRONTEND.resolve()).as_posix()
    except ValueError as exc:
        raise ExportError(f"module outside frontend/: {module_id!r}") from exc
    if relative == "index.html" or relative.startswith("src/"):
        return "first-party", relative
    match = _PACKAGE_DIR.match(relative)
    if match is None:
        raise ExportError(f"unattributed module {module_id!r}")
    return "package", match.group(1).rstrip("/")


def _license_texts(package_dir: Path, emitted: set[Path]) -> list[tuple[str, str]]:
    texts = []
    for path in sorted(package_dir.iterdir()):
        if path.is_file() and _LICENSE_FILE.match(path.name):
            texts.append((path.name, path.read_text(encoding="utf-8")))
    # Prebuilt bundles may carry the notices of code they inline.
    for module in sorted(emitted):
        legal = module.with_name(module.name + ".LEGAL.txt")
        if legal.is_file():
            label = legal.relative_to(package_dir).as_posix()
            texts.append((label, legal.read_text(encoding="utf-8")))
    if not texts:
        raise ExportError(f"no license text in {package_dir}")
    return texts


def _chunks(closure_record: dict, closure: list[str]) -> list[dict]:
    """The recorded chunks, once each, for exactly the emitted JS closure.

    Attribution is only as complete as this coverage: an omitted, duplicate or
    unknown chunk, or one with no emitted module, would leave bundled code
    without notices. Each is refused before any package is attributed.
    """
    chunks = closure_record.get("chunks")
    if closure_record.get("format") != "millrace-os-module-closure/v1" or not (
        isinstance(chunks, list)
    ):
        raise ExportError("module-closure.json has an unknown format")
    names = []
    for chunk in chunks:
        if not (
            isinstance(chunk, dict)
            and set(chunk) == {"fileName", "modules"}
            and isinstance(chunk["fileName"], str)
            and isinstance(chunk["modules"], list)
        ):
            raise ExportError("module-closure.json has a malformed chunk")
        name = chunk["fileName"]
        for module in chunk["modules"]:
            if not (
                isinstance(module, dict)
                and set(module) == {"id", "renderedLength"}
                and isinstance(module["id"], str)
                and type(module["renderedLength"]) is int
                and module["renderedLength"] >= 0
            ):
                raise ExportError(f"malformed module record in chunk {name!r}")
        if not any(module["renderedLength"] > 0 for module in chunk["modules"]):
            raise ExportError(f"chunk {name!r} records no emitted module")
        names.append(name)
    expected = {path for path in closure if path.endswith((".js", ".mjs"))}
    missing = sorted(expected - set(names))
    extra = sorted(set(names) - expected)
    duplicate = sorted({name for name in names if names.count(name) > 1})
    if missing or extra or duplicate:
        raise ExportError(
            "module-closure.json does not cover the emitted JS closure: "
            f"missing {missing}, extra {extra}, duplicate {duplicate}"
        )
    return chunks


def _inventory(
    closure_record: dict, closure: list[str], packages: dict, lock_raw: bytes
):
    chunks = _chunks(closure_record, closure)
    emitted: dict[str, set[Path]] = {}
    modules: dict[str, int] = {}
    tools: set[str] = set()
    first_party = 0
    for chunk in chunks:
        for module in chunk["modules"]:
            module_id, length = module["id"], module["renderedLength"]
            is_css = module_id.split("?", 1)[0].endswith(".css")
            if length <= 0 and not is_css:
                continue  # tree-shaken: no emitted code
            kind, key = _attribute(module_id)
            if kind == "tool":
                tools.add(key)
            elif kind == "first-party":
                first_party += 1
            else:
                entry = packages.get(key)
                if entry is None:
                    raise ExportError(f"emitted module from unlocked {key!r}")
                name = key.rsplit("node_modules/", 1)[1]
                if entry.get("dev") and name in TOOL_PACKAGES:
                    tools.add(name)
                    continue
                if entry.get("dev"):
                    raise ExportError(f"build-only package code emitted: {key}")
                emitted.setdefault(key, set()).add(
                    Path(module_id.split("?", 1)[0]).resolve()
                )
                modules[key] = modules.get(key, 0) + 1
    included = dict.fromkeys(emitted)
    stack = list(emitted)
    while stack:
        parent = stack.pop()
        for name in sorted(packages[parent].get("dependencies", {})):
            key = _resolve(packages, parent, name)
            if key is None or name.startswith("@types/"):
                continue
            if packages[key].get("dev"):
                raise ExportError(f"production dependency {key} is marked dev")
            if key not in included:
                included[key] = parent
                stack.append(key)
    bundled, notices = [], []
    for key in sorted(included):
        entry = packages[key]
        name = key.rsplit("node_modules/", 1)[1]
        directory = FRONTEND / key
        declared = _strict_load(directory / "package.json")
        if declared.get("version") != entry.get("version"):
            raise ExportError(f"{key} installed version differs from the lock")
        texts = _license_texts(directory, emitted.get(key, set()))
        bundled.append({
            "name": name,
            "version": entry["version"],
            "license": entry.get("license"),
            "integrity": entry["integrity"],
            "resolved": entry["resolved"],
            "emitted_modules": modules.get(key, 0),
            "included_by": None if key in emitted else included[key],
            "notice_files": [label for label, _ in texts],
        })
        notices.append((f"{name}@{entry['version']}", entry.get("license"), texts))
    helpers = []
    for tool in sorted(tools):
        key = "node_modules/" + tool
        entry = packages.get(key)
        if entry is None:
            raise ExportError(f"helper tool {tool} is not locked")
        texts = _license_texts(FRONTEND / key, set())
        helpers.append({
            "name": tool,
            "version": entry["version"],
            "license": entry.get("license"),
            "role": "bundler-authored runtime helper code in emitted chunks",
            "notice_files": [label for label, _ in texts],
        })
        title = f"{tool}@{entry['version']} (helper code)"
        notices.append((title, entry.get("license"), texts))
    build_only = [
        {
            "name": key.rsplit("node_modules/", 1)[1],
            "version": entry.get("version"),
            "license": entry.get("license"),
        }
        for key, entry in sorted(packages.items())
        if key and entry.get("dev")
    ]
    inventory = {
        "format": "millrace-os-dependency-inventory/v1",
        "frontend_id": "stock",
        "package_lock_sha256": hashlib.sha256(lock_raw).hexdigest(),
        "package_json_sha256": hashlib.sha256(
            _regular(FRONTEND / "package.json")
        ).hexdigest(),
        "first_party_modules": first_party,
        "bundled": bundled,
        "tool_helpers": helpers,
        "build_only": build_only,
        "spdx_bundled": sorted(
            {item["license"] for item in bundled + helpers if item["license"]}
        ),
    }
    return inventory, _notice_text(notices)


def _notice_text(notices) -> bytes:
    lines = [
        "Third-party software bundled in the Millrace OS stock browser client.",
        "Millrace OS itself is licensed separately under Apache-2.0 (LICENSE).",
        "",
    ]
    for title, license_id, texts in notices:
        lines += ["=" * 72, f"{title} - {license_id}", "=" * 72]
        for label, text in texts:
            lines += [f"--- {label} ---", text.rstrip("\n"), ""]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_outputs() -> dict[str, bytes]:
    staged = _staged_files()
    manifest = _strict_load(STAGING / ".vite" / "manifest.json")
    if not isinstance(manifest, dict):
        raise ExportError("Vite manifest is not an object")
    closure = _closure(manifest)
    unexpected = staged - set(closure) - STAGING_EXTRAS
    missing = (set(closure) | {"index.html"}) - staged
    if unexpected or missing:
        raise ExportError(
            f"staging mismatch: extra {sorted(unexpected)}, missing {sorted(missing)}"
        )
    shell = _regular(STAGING / "index.html")
    _shell_references(shell, closure)
    outputs = {"index.html": shell}
    for path in closure:
        content = _regular(STAGING / path)
        if path.endswith((".js", ".mjs", ".css")) and b"sourceMappingURL" in content:
            raise ExportError(f"source-map reference in {path}")
        outputs[path] = content
    packages, lock_raw = _lock()
    record = _strict_load(STAGING / "module-closure.json")
    if not isinstance(record, dict):
        raise ExportError("module-closure.json is not an object")
    inventory, notices = _inventory(record, closure, packages, lock_raw)
    outputs[NOTICES] = notices
    outputs[INVENTORY] = _json_bytes(inventory)
    registry = _strict_load(REGISTRY)
    digests = {item["contract_id"]: item["sha256"] for item in registry["schemas"]}
    missing_contracts = [name for name in STOCK_CONTRACTS if name not in digests]
    if missing_contracts:
        raise ExportError(f"registry lacks {missing_contracts}; export schemas first")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    stock_manifest = {
        "format": "millrace-os-frontend-bundle/v1",
        "frontend_id": "stock",
        "frontend_version": project["version"],
        "entry": "index.html",
        "assets": [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "media_type": _media(path),
            }
            for path, content in sorted(outputs.items())
        ],
        "api_version": "v1",
        "required_contracts": [
            {"contract_id": name, "version": 1, "sha256": digests[name]}
            for name in STOCK_CONTRACTS
        ],
        "required_capabilities": list(STOCK_CAPABILITIES),
        "presentation_format": None,
        "default_presentation": None,
    }
    folded = [path.lower() for path in outputs]
    if len(set(folded)) != len(folded):
        raise ExportError("case-insensitive duplicate output paths")
    outputs[MANIFEST_NAME] = _json_bytes(stock_manifest)
    return outputs


def _existing() -> dict[str, bytes]:
    if not OUTPUT.exists():
        return {}
    if OUTPUT.is_symlink() or not OUTPUT.is_dir():
        raise ExportError(f"owned output is not a plain directory: {OUTPUT}")
    found = {}
    for path in OUTPUT.rglob("*"):
        if path.is_symlink():
            raise ExportError(f"symlink in owned output: {path}")
        if path.is_file():
            found[path.relative_to(OUTPUT).as_posix()] = path.read_bytes()
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the packaged closure; write nothing",
    )
    args = parser.parse_args(argv)
    try:
        outputs = build_outputs()
        current = _existing()
    except ExportError as error:
        print(f"export refused: {error}", file=sys.stderr)
        return 1
    if args.check:
        differs = sorted(
            name
            for name in set(outputs) | set(current)
            if outputs.get(name) != current.get(name)
        )
        for name in differs:
            print(f"differs: {name}", file=sys.stderr)
        return 1 if differs else 0
    for name, content in sorted(outputs.items()):
        target = OUTPUT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    # Remove only stale files inside the owned output, after a valid write.
    for name in sorted(set(current) - set(outputs)):
        (OUTPUT / name).unlink()
    for directory in sorted(OUTPUT.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    for name in sorted(outputs):
        print(f"{hashlib.sha256(outputs[name]).hexdigest()}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
