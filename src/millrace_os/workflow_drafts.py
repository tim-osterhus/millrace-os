"""OS draft files and a cooperating-client publication guard, never graph semantics."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from .contracts import DraftFile, DraftRead
from .paths import ensure_directory, open_new_file, validate_file_target
from .router_client import RouterUnavailable

__all__ = ["WorkflowDrafts"]

TERMINAL = {"succeeded", "cancelled", "failed"}


def _name(raw):
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise RouterUnavailable("invalid_draft_path", 422)
    path = PurePosixPath(raw)
    if (
        not path.parts
        or path.is_absolute()
        or str(path) != raw
        or any(p in {".", ".."} for p in path.parts)
        or any(p.casefold() == ".millrace-router-owner" for p in path.parts)
    ):
        raise RouterUnavailable("invalid_draft_path", 422)
    return raw


def _aliases(names):
    seen = {}
    files = set(names)
    for name in names:
        parts = PurePosixPath(name).parts
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            folded = prefix.casefold()
            if folded in seen and seen[folded] != prefix:
                raise RouterUnavailable("duplicate_draft_path", 422)
            if count < len(parts) and prefix in files:
                raise RouterUnavailable("invalid_draft_path", 422)
            seen[folded] = prefix


def _decode(files):
    result = {}
    aliases = set()
    for entry in files:
        name = _name(entry.path if isinstance(entry, DraftFile) else entry["path"])
        if name.casefold() in aliases:
            raise RouterUnavailable("duplicate_draft_path", 422)
        aliases.add(name.casefold())
        try:
            content = (
                entry.content_base64
                if isinstance(entry, DraftFile)
                else entry["content_base64"]
            )
            result[name] = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RouterUnavailable("invalid_draft_encoding", 422) from exc
    _aliases(result)
    return result


def _hash(files):
    encoded = {
        name: base64.b64encode(raw).decode() for name, raw in sorted(files.items())
    }
    return hashlib.sha256(
        json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write(path, raw):
    """Replace one safe regular file; tests may interrupt this publication seam."""
    ensure_directory(path.parent)
    target, _ = validate_file_target(path, owner_only=False)
    temporary = target.with_name(".os-publish-" + uuid.uuid4().hex)
    fd, temporary = open_new_file(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Check the destination again immediately before replacing it.
        validate_file_target(target, owner_only=False)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path, value):
    _write(
        path,
        (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(),
    )


class WorkflowDrafts:
    """Keep mutable authoring separate from immutable in-progress publication."""

    def __init__(self, state_dir, client, workers):
        self.root = Path(state_dir) / "drafts"
        self.client = client
        self.workers = workers
        self._lock = threading.RLock()

    def _folder(self, draft_id):
        if (
            not isinstance(draft_id, str)
            or len(draft_id) != 32
            or any(c not in "0123456789abcdef" for c in draft_id)
        ):
            raise RouterUnavailable("draft_not_found", 404)
        return self.root / draft_id

    def _metadata(self, home, draft_id):
        self.client.state_home(home)
        path = self._folder(draft_id) / "draft.json"
        target, exists = validate_file_target(path)
        if not exists:
            raise RouterUnavailable("draft_not_found", 404)
        try:
            value = json.loads(target.read_bytes())
            if value != {"draft_id": draft_id, "home": home}:
                raise ValueError
            return value
        except (ValueError, TypeError) as exc:
            raise RouterUnavailable("draft_evidence_invalid", 409) from exc

    def _files(self, root):
        files = {}
        if not root.exists():
            return files
        # Every component is checked by existing owner/path helpers; never follow
        # a directory symlink encountered during enumeration.
        ensure_directory(root)
        for parent, dirs, names in os.walk(root, followlinks=False):
            for name in dirs:
                ensure_directory(Path(parent) / name)
            for name in names:
                path = Path(parent) / name
                validate_file_target(path)
                rel = _name(path.relative_to(root).as_posix())
                if rel.casefold() in {key.casefold() for key in files}:
                    raise RouterUnavailable("duplicate_draft_path", 422)
                files[rel] = path.read_bytes()
        return files

    def create(self, home, files):
        self.client.state_home(home)
        contents = _decode(files)
        draft_id = uuid.uuid4().hex
        folder = self._folder(draft_id)
        with self._lock:
            ensure_directory(folder / "files")
            _json(folder / "draft.json", {"draft_id": draft_id, "home": home})
            for name, raw in contents.items():
                _write(folder / "files" / name, raw)
        return self.read(home, draft_id)

    def read(self, home, draft_id):
        with self._lock:
            self._metadata(home, draft_id)
            files = self._files(self._folder(draft_id) / "files")
        return DraftRead(
            home=home,
            draft_id=draft_id,
            files=[
                DraftFile(path=n, content_base64=base64.b64encode(v).decode())
                for n, v in sorted(files.items())
            ],
            sha256=_hash(files),
        )

    def put(self, home, draft_id, files, *, replace=False):
        contents = _decode(files)
        with self._lock:
            self._metadata(home, draft_id)
            root = self._folder(draft_id) / "files"
            current = self._files(root)
            existing = {name.casefold(): name for name in current}
            if any(
                name.casefold() in existing and existing[name.casefold()] != name
                for name in contents
            ):
                raise RouterUnavailable("duplicate_draft_path", 422)
            _aliases(current.keys() | contents.keys())
            for name, raw in contents.items():
                _write(root / name, raw)
            if replace:
                for name in current.keys() - contents.keys():
                    target, _ = validate_file_target(root / name)
                    target.unlink()
        return self.read(home, draft_id)

    def workflow_file(self, home, draft_id):
        self._metadata(home, draft_id)
        path = self._folder(draft_id) / "files" / "workflow.json"
        _, exists = validate_file_target(path)
        if not exists:
            raise RouterUnavailable("draft_workflow_missing", 409)
        return path

    def validate(self, home, draft_id, instance_id, run_id=None):
        return self.client.validate_revision(
            home,
            instance_id,
            run_id=run_id,
            workflow_file=self.workflow_file(home, draft_id),
        )

    def _publication(self, home, instance_id):
        if (
            not isinstance(instance_id, str)
            or len(instance_id) != 32
            or any(c not in "0123456789abcdef" for c in instance_id)
        ):
            raise RouterUnavailable("invalid_instance_id", 422)
        return self.client.state_home(home) / "os-client" / "publications" / instance_id

    def acquire(self, home, instance_id, *, repair=False):
        if instance_id is None:
            return lambda: None
        folder = self._publication(home, instance_id)
        ensure_directory(folder)
        target, _ = validate_file_target(folder / ".lock")
        fd = os.open(
            target, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker, exists = validate_file_target(folder / "in-progress.json")
            if exists and not repair:
                raise RouterUnavailable("publication_incomplete", 409)
        except BlockingIOError as exc:
            os.close(fd)
            raise RouterUnavailable("publication_busy", 429) from exc
        except BaseException:
            os.close(fd)
            raise
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        return release

    def _request(self, operation, instance_id, run_id, request_id, version=None):
        return {
            "format": "millrace-router-request/v2",
            "operation": operation,
            "instance_id": instance_id,
            "run_id": run_id,
            "plane_id": None,
            "request_id": request_id,
            "expected_state_version": version,
            "payload": {},
        }

    def _exact_receipt(self, home, request):
        from .router_client import request_digest

        result = self.client.receipt(
            home,
            request["request_id"],
            instance_id=request["instance_id"],
            run_id=request["run_id"],
        )
        if result is None:
            # Repair is an explicit intent to complete this recorded operation.
            # A known queued receipt is observed rather than replayed.
            result = self.client.execute_request(home, request)
        while True:
            if (
                result.get("request_digest") != request_digest(request)
                or result.get("request_id") != request["request_id"]
            ):
                raise RouterUnavailable("publication_receipt_mismatch", 409)
            if result.get("status") != "queued":
                return result
            time.sleep(0.02)
            observed = self.client.receipt(
                home,
                request["request_id"],
                instance_id=request["instance_id"],
                run_id=request["run_id"],
            )
            if observed is not None:
                result = observed

    def _control(self, record, field, operation, version=None):
        folder = self._publication(record["home"], record["instance_id"])
        request = record[field]
        if request is None:
            request = self._request(
                operation,
                record["instance_id"],
                record["run_id"],
                record["request_id"]
                if operation == "submit_revision"
                else uuid.uuid4().hex,
                version,
            )
            record[field] = request
            # The exact body survives a crash before, during, or after dispatch.
            _json(folder / "in-progress.json", record)
        receipt = self._exact_receipt(record["home"], request)
        previous = next(
            (r for r in record["receipts"] if r["request_id"] == request["request_id"]),
            None,
        )
        if previous is None:
            record["receipts"].append(receipt)
        elif previous != receipt:
            raise RouterUnavailable("publication_receipt_mismatch", 409)
        _json(folder / "in-progress.json", record)
        if receipt.get("status") != "applied":
            raise RouterUnavailable("publication_core_refused", 409)
        return receipt

    def _quiescent(self, home, instance_id, run_id):
        while True:
            run = self.client.inspect_run(home, instance_id, run_id)
            if (
                run.get("quiescent")
                and not run.get("inflight_invocations")
                and all(
                    i.get("writer_state") == "stopped"
                    for i in run.get("invocations", [])
                )
            ):
                break
            # Unknown/effective live writers require explicit reconciliation.
            if not run.get("inflight_invocations"):
                raise RouterUnavailable("publication_writer_unresolved", 409)
            time.sleep(0.02)
        self.workers.wait_group(
            home, instance_id, exclude_thread=threading.current_thread()
        )
        return self.client.inspect_run(home, instance_id, run_id)

    def apply(
        self, home, draft_id, instance_id, run_id, base_state_version, request_id
    ):
        release = self.acquire(home, instance_id)
        try:
            before = self.client.inspect_run(home, instance_id, run_id)
            if before["state_version"] != base_state_version:
                raise RouterUnavailable("stale_state_version", 409)
            if before["status"] in TERMINAL:
                raise RouterUnavailable("publication_terminal_run", 409)
            with self._lock:
                self._metadata(home, draft_id)
                files = self._files(self._folder(draft_id) / "files")
                token = uuid.uuid4().hex
                frozen = self._folder(draft_id) / "frozen" / token
                ensure_directory(frozen)
                for name, raw in files.items():
                    _write(frozen / name, raw)
            record = {
                "format": "millrace-os-publication/v1",
                "home": home,
                "instance_id": instance_id,
                "run_id": run_id,
                "base_state_version": base_state_version,
                "draft_id": draft_id,
                "frozen_token": token,
                "files_sha256": _hash(files),
                "request_id": request_id,
                "receipts": [],
                "candidate_id": None,
                "run_state_version": None,
                "pause_request": None,
                "submit_request": None,
            }
            folder = self._publication(home, instance_id)
            _json(folder / "in-progress.json", record)
            return self._publish(record)
        finally:
            release()

    def _publish(self, record):
        home, instance_id, run_id = (
            record["home"],
            record["instance_id"],
            record["run_id"],
        )
        folder = self._publication(home, instance_id)
        frozen = self._folder(record["draft_id"]) / "frozen" / record["frozen_token"]
        files = self._files(frozen)
        if _hash(files) != record["files_sha256"] or "workflow.json" not in files:
            raise RouterUnavailable("publication_evidence_invalid", 409)
        run = self.client.inspect_run(home, instance_id, run_id)
        terminal = run["status"] in TERMINAL
        if terminal and record["submit_request"] is not None:
            # Cancellation can follow a submit whose local completion write
            # failed. Preserve that actual receipt, without submitting anything
            # to a terminal run when no admission ever occurred.
            from .router_client import request_digest

            saved = record["submit_request"]
            receipt = self.client.receipt(
                home, saved["request_id"], instance_id=instance_id, run_id=run_id
            )
            if receipt is not None:
                receipt = self._exact_receipt(home, saved)
                if receipt.get("request_digest") != request_digest(saved):
                    raise RouterUnavailable("publication_receipt_mismatch", 409)
                if not any(
                    r["request_id"] == saved["request_id"] for r in record["receipts"]
                ):
                    record["receipts"].append(receipt)
                    _json(folder / "in-progress.json", record)
        if not terminal:
            self._control(record, "pause_request", "pause_run")
            run = self._quiescent(home, instance_id, run_id)
            terminal = run["status"] in TERMINAL
        if terminal and record["candidate_id"] is not None:
            retained_context = self.client.validate_revision(
                home, instance_id, run_id=run_id, workflow_file=frozen / "workflow.json"
            )
            retained_candidate = retained_context.get("draft_revision", {}).get(
                "candidate_id"
            )
            if retained_candidate != record["candidate_id"]:
                raise RouterUnavailable("publication_candidate_changed", 409)
        validation = self.client.validate_revision(
            home,
            instance_id,
            run_id=None if terminal else run_id,
            workflow_file=frozen / "workflow.json",
        )
        if not validation["valid"]:
            raise RouterUnavailable("publication_validation_failed", 409)
        candidate_id = validation["draft_revision"]["candidate_id"]
        version = validation["validation_context"].get("run_state_version")
        if (
            not terminal
            and record["candidate_id"] is not None
            and record["candidate_id"] != candidate_id
        ):
            raise RouterUnavailable("publication_candidate_changed", 409)
        if terminal:
            # Instance validation has advisory workspace context. Preserve the
            # original run candidate and record this distinct fresh-run target.
            record["terminal_candidate_id"] = candidate_id
        else:
            record.update(candidate_id=candidate_id, run_state_version=version)
        _json(folder / "in-progress.json", record)
        current = self.client.inspect_run(home, instance_id, run_id)
        if not terminal and current["state_version"] != version:
            raise RouterUnavailable("stale_state_version", 409)
        instance = self.client.inspect_instance(home, instance_id)
        destination = Path(instance["workflow_file"]).parent
        # Public catalog's ownership check also protects in-place publication.
        self.client.catalog(
            home,
            "check_ownership",
            workflow_file=Path(instance["workflow_file"]),
            project_root=Path(instance["project_root"]),
        )
        for name, raw in sorted(files.items()):
            _write(destination / name, raw)
        published = self.client.validate_revision(
            home, instance_id, run_id=None if terminal else run_id
        )
        if (
            not published["valid"]
            or published["draft_revision"]["candidate_id"] != candidate_id
        ):
            raise RouterUnavailable("publication_hash_mismatch", 409)
        if not terminal:
            if published["validation_context"]["run_state_version"] != version:
                raise RouterUnavailable("stale_state_version", 409)
            self._control(record, "submit_request", "submit_revision", version)
        else:
            record["terminal_evidence"] = {
                "status": current["status"],
                "state_version": current["state_version"],
            }
        record["status"] = "terminal_files_ready" if terminal else "paused_pending"
        _json(folder / "completed.json", record)
        marker, _ = validate_file_target(folder / "in-progress.json")
        marker.unlink()
        return record

    def repair(self, home, instance_id):
        release = self.acquire(home, instance_id, repair=True)
        try:
            path = self._publication(home, instance_id) / "in-progress.json"
            target, exists = validate_file_target(path)
            if not exists:
                raise RouterUnavailable("publication_not_found", 404)
            try:
                record = json.loads(target.read_bytes())
                wanted = {
                    "format",
                    "home",
                    "instance_id",
                    "run_id",
                    "base_state_version",
                    "draft_id",
                    "frozen_token",
                    "files_sha256",
                    "request_id",
                    "receipts",
                    "candidate_id",
                    "run_state_version",
                    "pause_request",
                    "submit_request",
                }
                if (
                    set(record) not in (wanted, wanted | {"terminal_candidate_id"})
                    or record["format"] != "millrace-os-publication/v1"
                    or record["home"] != home
                    or record["instance_id"] != instance_id
                ):
                    raise ValueError
                self._folder(record["draft_id"])
                self._folder(record["frozen_token"])
                if not isinstance(record["receipts"], list) or any(
                    not isinstance(r, dict) or not isinstance(r.get("request_id"), str)
                    for r in record["receipts"]
                ):
                    raise ValueError
                for field, operation in (
                    ("pause_request", "pause_run"),
                    ("submit_request", "submit_revision"),
                ):
                    request = record[field]
                    if request is None:
                        continue
                    expected_version = request["expected_state_version"]
                    if (operation == "pause_run" and expected_version is not None) or (
                        operation == "submit_revision"
                        and (type(expected_version) is not int or expected_version < 1)
                    ):
                        raise ValueError
                    expected = self._request(
                        operation,
                        instance_id,
                        record["run_id"],
                        request["request_id"],
                        expected_version,
                    )
                    if request != expected or (
                        operation == "submit_revision"
                        and request["request_id"] != record["request_id"]
                    ):
                        raise ValueError
            except (ValueError, TypeError, KeyError) as exc:
                raise RouterUnavailable("publication_evidence_invalid", 409) from exc
            return self._publish(record)
        finally:
            release()
