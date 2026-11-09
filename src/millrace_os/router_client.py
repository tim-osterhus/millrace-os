"""Lazy public Plus/Core access. Core remains the only execution authority."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import re
from pathlib import Path

__all__ = ["RouterClient", "RouterUnavailable", "request_digest"]

INSPECTION_CONTRACT = "millrace-router-inspection/v1"
WORKFLOW_VIEWS_CONTRACT = "millrace-workflow-views/v1"
HOMES_PAGE = 50
# Statuses of the Plus output helper's typed refusals; any other code is 409.
OUTPUT_STATUS = {
    "invalid_output_record": 422,
    "invalid_request": 422,
    "output_not_found": 404,
    "output_representation_unavailable": 404,
    "output_too_large": 413,
    "output_publication_uncertain": 503,
}


class RouterUnavailable(RuntimeError):
    """A sanitized OS availability/shape refusal, distinct from a Core receipt."""

    def __init__(self, code: str, status: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def request_digest(request: dict) -> str:
    """Correlate the untouched envelope using Core request/v2's JSON encoding."""
    try:
        raw = json.dumps(
            request, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, TypeError) as exc:
        raise RouterUnavailable("invalid_request", 422) from exc
    return hashlib.sha256(raw.encode()).hexdigest()


class RouterClient:
    """Select configured homes and invoke only public Plus facade operations."""

    def __init__(self, homes: dict[str, Path]) -> None:
        self.homes = dict(homes)

    def state_home(self, home: str) -> Path:
        try:
            return self.homes[home]
        except KeyError as exc:
            raise RouterUnavailable("unknown_router_home", 422) from exc

    def _control(self):
        try:
            router = importlib.import_module("millrace_plus.router")
            router.require_core()
            return importlib.import_module("millrace_plus.router.control")
        except ImportError as exc:
            raise RouterUnavailable("router_unavailable") from exc

    def _invoke(self, home, operation, *args, **kwargs):
        module = self._control()
        try:
            return getattr(module, operation)(
                *args, state_home=self.state_home(home), **kwargs
            )
        except Exception as exc:
            # This is the public, documented execution exception boundary; no
            # exception text or filesystem path is copied into HTTP output.
            execution = importlib.import_module("millrace.router.execution")
            if isinstance(exc, execution.RouterError):
                raise RouterUnavailable("core_" + exc.code, 409) from exc
            # Plus's public ownership/catalog guard refuses before Core runs;
            # keep its machine code in the established catalog_<code> form.
            catalog = importlib.import_module("millrace_plus.catalog")
            if isinstance(exc, catalog.CatalogError):
                raise RouterUnavailable("catalog_" + exc.code, 409) from exc
            raise

    def list_instances(self, home):
        return self._invoke(home, "list_instances")

    def inspect_instance(self, home, instance_id):
        return self._invoke(home, "inspect_instance", instance_id)

    def inspect_run(self, home, instance_id, run_id):
        return self._invoke(home, "inspect_run", instance_id, run_id)

    def validate_revision(self, home, instance_id, *, run_id=None, workflow_file=None):
        return self._invoke(
            home,
            "validate_revision",
            instance_id,
            run_id=run_id,
            workflow_file=workflow_file,
        )

    def execute_request(self, home, request, on_started=None):
        return self._invoke(
            home, "execute_request", copy.deepcopy(request), on_started=on_started
        )

    def receipt(
        self, home, request_id, *, instance_id=None, run_id=None, bounded=False
    ):
        """Inspect known targets only; no replay or guessed inventory scan.

        `bounded=True` is the strict view lookup: one indexed Core receipt read
        under the inspection budget, with typed refusals and no legacy fallback.
        The default lifecycle path never touches the bounded reader.
        """
        if bounded:
            if instance_id is None:
                raise RouterUnavailable("invalid_request", 422)
            page = self._view(
                home,
                "inspect_run_page",
                instance_id,
                run_id,
                section="controls",
                request_id=request_id,
            )
            items = page["items"]
            return copy.deepcopy(items[0]) if items else None
        if instance_id is None:
            return None
        projections = []
        if run_id is not None:
            projections.append(self.inspect_run(home, instance_id, run_id))
        # Start is an instance-targeted control whose applied receipt contains
        # the newly generated run ID. Its row remains in instance controls.
        projections.append(self.inspect_instance(home, instance_id))
        for data in projections:
            for receipt in data.get("controls", []):
                if receipt.get("request_id") == request_id:
                    return copy.deepcopy(receipt)
        return None

    def _inspection(self):
        """The Plus facade, only when it forwards Core's bounded inspection."""
        try:
            module = self._control()
        except RouterUnavailable as exc:
            raise RouterUnavailable("router_inspection_unavailable") from exc
        if getattr(module, "INSPECTION_CONTRACT", None) != INSPECTION_CONTRACT:
            raise RouterUnavailable("router_inspection_unavailable")
        return module

    def _view(self, home, operation, *args, **kwargs):
        self.state_home(home)
        self._inspection()
        try:
            return self._invoke(home, operation, *args, **kwargs)
        except ImportError as exc:
            # Plus refuses a missing Core operation with CoreContractError.
            raise RouterUnavailable("router_inspection_unavailable") from exc

    def list_homes(self, *, limit=HOMES_PAGE, cursor=None):
        """Configured home names only; no state home is read."""
        names = sorted(self.homes)
        raw = json.dumps(names, ensure_ascii=False, separators=(",", ":"))
        prefix = "h" + hashlib.sha256(raw.encode()).hexdigest()[:32] + "_"
        offset = 0
        if cursor is not None:
            # The digest binds exact membership, so an offset cannot drift.
            match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]{0,8})", cursor)
            if match is None or int(match.group(1)) >= len(names):
                raise RouterUnavailable("invalid_request", 422)
            offset = int(match.group(1))
        end = offset + limit
        return {
            "items": names[offset:end],
            "next_cursor": prefix + str(end) if end < len(names) else None,
        }

    def view_instances(self, home, *, limit=50, cursor=None):
        return self._view(home, "list_instances_page", limit=limit, cursor=cursor)

    def view_instance(self, home, instance_id, *, limit=50, cursor=None):
        return self._view(
            home, "inspect_instance_page", instance_id, limit=limit, cursor=cursor
        )

    def view_run(self, home, instance_id, run_id, **selectors):
        return self._view(home, "inspect_run_page", instance_id, run_id, **selectors)

    def view_revision(self, home, instance_id, run_id, revision_id):
        return self._view(
            home, "inspect_revision_graph", instance_id, run_id, revision_id
        )

    def view_validate(self, home, instance_id, *, run_id=None):
        return self._view(
            home, "validate_revision_summary", instance_id, run_id=run_id
        )

    def _views(self):
        """The public Plus output helper, only with its exact version marker."""
        try:
            module = importlib.import_module("millrace_plus.workflow_views")
        except ImportError as exc:
            raise RouterUnavailable("workflow_views_unavailable") from exc
        if getattr(module, "WORKFLOW_VIEWS_CONTRACT", None) != WORKFLOW_VIEWS_CONTRACT:
            raise RouterUnavailable("workflow_views_unavailable")
        return module

    def _output(self, home, operation, *args, **kwargs):
        state_home = self.state_home(home)
        module = self._views()
        try:
            return getattr(module, operation)(*args, state_home=state_home, **kwargs)
        except module.WorkflowViewsError as exc:
            raise RouterUnavailable(exc.code, OUTPUT_STATUS.get(exc.code, 409)) from exc

    def publish_output(self, home, record):
        """Publish through the helper; its exact identity lookup needs Core."""
        self.state_home(home)
        self._views()
        self._inspection()
        try:
            return self._output(home, "publish_output", record)
        except RouterUnavailable:
            raise
        except ImportError as exc:
            raise RouterUnavailable("router_inspection_unavailable") from exc
        except OSError as exc:
            # The helper cannot say whether a record became visible.
            raise RouterUnavailable("output_publication_uncertain") from exc
        except Exception as exc:
            execution = importlib.import_module("millrace.router.execution")
            if isinstance(exc, execution.RouterError):
                raise RouterUnavailable("core_" + exc.code, 409) from exc
            raise

    def list_outputs(self, home, instance_id, run_id, *, limit=50, cursor=None):
        """Stored records only: no Core read."""
        return self._output(
            home, "list_outputs", instance_id, run_id, limit=limit, cursor=cursor
        )

    def read_output(
        self, home, instance_id, run_id, output_id, record_sha256, representation
    ):
        """Stored records only: no Core read."""
        return self._output(
            home,
            "read_output",
            instance_id,
            run_id,
            output_id,
            record_sha256,
            representation,
        )

    def _catalog(self):
        try:
            return importlib.import_module("millrace_plus.catalog")
        except ImportError as exc:
            raise RouterUnavailable("catalog_unavailable") from exc

    def catalog(self, home, operation, **kwargs):
        self.state_home(home)
        module = self._catalog()
        try:
            return getattr(module, operation)(**kwargs)
        except module.CatalogError as exc:
            raise RouterUnavailable("catalog_" + exc.code, 409) from exc
