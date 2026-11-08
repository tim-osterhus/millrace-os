"""Versioned, bounded foundations with trust checked before route handlers."""

from __future__ import annotations

import asyncio
import copy
import secrets
import uuid
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path as FilePath
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import JsonValue
from starlette.exceptions import HTTPException

from .auth import COOKIE, SESSION_TTL, TrustError
from .config import _endpoint_host
from .contracts import (
    ActionInfo,
    ActionInput,
    BootstrapInput,
    BootstrapNonce,
    CatalogCopyInput,
    CatalogInitInput,
    Diagnostics,
    DraftApplyInput,
    DraftCreateInput,
    DraftFile,
    DraftFileInput,
    DraftFilesInput,
    DraftRead,
    DraftValidateInput,
    Error,
    Event,
    ExchangeInput,
    Health,
    HomesQuery,
    Identifier,
    Identity,
    OutputReadQuery,
    Page,
    PageQuery,
    ProviderInfo,
    PublicationRepairInput,
    Read,
    Registry,
    Rotation,
    RouterCallObservation,
    RouterCallObservationV2,
    RouterHomeItem,
    RouterHomes,
    RouterLocalRead,
    RouterObservationCapabilities,
    RouterRead,
    RouterRequestInput,
    RouterValidateInput,
    RouterValidation,
    RouterView,
    RouterViewValidateInput,
    RunViewQuery,
    SessionOutput,
    Version,
    ViewPageQuery,
    WorkflowOutput,
    WorkflowOutputPublication,
    WorkflowOutputPublishInput,
    WorkflowOutputs,
    canonical,
    schema_artifacts,
    strict_json,
)
from .paths import UnsafePathError
from .router_client import RouterUnavailable

DEFAULT_BODY_LIMIT = 4096
DRAFT_UPLOAD_LIMIT = 1048576
# Compact UTF-8 bytes of every new bounded JSON response: the 1 MiB payload
# policy plus the existing 1,024-byte wrapper reservation.
RESPONSE_LIMIT = DRAFT_UPLOAD_LIMIT + 1024
CALL_V2_MEDIA_TYPE = "application/vnd.millrace.router-call.v2+json"
PAGE_QUERY_PATHS = frozenset({"/api/v1/providers", "/api/v1/actions"})
# Large-body routes whose raw body is strictly parsed before model validation:
# ordinary JSON parsing would silently keep the last of duplicate keys.
STRICT_BODY_ROUTES = frozenset({("POST", "/api/v1/router/outputs")})


def body_limit(path, method):
    parts = path.split("/")
    upload = (
        method.upper() == "PUT"
        and len(parts) == 7
        and parts[:5] == ["", "api", "v1", "router", "drafts"]
        and parts[6] in {"file", "files"}
    )
    large = upload or (method.upper(), path) in STRICT_BODY_ROUTES
    return DRAFT_UPLOAD_LIMIT if large else DEFAULT_BODY_LIMIT


def _view_query(parts):
    """Bounded view and output keys by path shape; None for any other path."""
    if parts[4:] == ["homes"]:
        return frozenset({"limit", "cursor"})
    if parts[4] == "outputs":
        run = len(parts) in {9, 10} and parts[5] == "instances" and parts[7] == "runs"
        if run and len(parts) == 9:
            return frozenset({"home", "limit", "cursor"})
        if run:
            return frozenset({"home", "record_sha256", "representation"})
        return frozenset()
    if parts[4] != "views":
        return None
    if parts[5:6] == ["requests"] and len(parts) == 7:
        return frozenset({"home", "instance_id", "run_id"})
    if parts[5:6] != ["instances"]:
        return frozenset()
    if len(parts) in {6, 7}:
        return frozenset({"home", "limit", "cursor"})
    if len(parts) == 9 and parts[7] == "runs":
        return frozenset(
            {"home", "section", "limit", "cursor", "request_id", "invocation_id"}
        )
    if len(parts) == 11 and parts[7] == "runs" and parts[9] == "revisions":
        return frozenset({"home"})
    return frozenset()


def query_allowlist(path, method):
    """Query keys admitted before routing; None defers to a `PageQuery` route.

    Repeated or unlisted keys refuse `invalid_request` (422). Entries that
    predate the bounded views reproduce the earlier ad hoc pattern exactly.
    """
    if path in PAGE_QUERY_PATHS:
        return None
    if path == "/api/v1/router/requests" and method == "OPTIONS":
        return frozenset({"home"})  # and exactly once; see `trust_boundary`
    if not path.startswith("/api/v1/router/") or method != "GET":
        return frozenset()
    parts = path.split("/")
    view = _view_query(parts)
    if view is not None:
        return view
    documented = (
        path in {"/api/v1/router/instances", "/api/v1/router/catalog/templates"}
        or (len(parts) == 6 and parts[4] in {"instances", "requests", "drafts"})
        or (len(parts) == 8 and parts[4] == "instances" and parts[6] == "runs")
    )
    if not documented:
        return frozenset()
    if parts[4] == "requests":
        return frozenset({"home", "instance_id", "run_id"})
    return frozenset({"home"})


def error_response(code: str, status: int) -> JSONResponse:
    category = (
        "trust"
        if status in {401, 403}
        else "validation"
        if status == 422
        else "availability"
        if status in {429, 503}
        else "request"
    )
    payload = Error(
        code=code,
        category=category,
        summary=code.replace("_", " "),
        retryable=code in {"router_busy", "router_read_busy", "publication_busy"},
    )
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse(payload.model_dump(), status_code=status, headers=headers)


def envelope(contract, data):
    return Read(contract=contract, data=data, observed_at=datetime.now(UTC).isoformat())


def configure_api(app: FastAPI, service):
    schemas, registry = schema_artifacts()

    def origin():
        port = service.bound_port if service._server_socket else service.config.port
        return f"http://{_endpoint_host(service.config.host)}:{port}"

    @app.middleware("http")
    async def trust_boundary(request: Request, call_next):
        try:
            expected = origin()
            if request.headers.get("host") != expected.removeprefix("http://"):
                raise TrustError("untrusted_host", 403)
            supplied_origin = request.headers.get("origin")
            if supplied_origin is not None and supplied_origin != expected:
                raise TrustError("wrong_origin", 403)
            path = request.url.path
            public = path in {
                "/",
                "/recovery",
                "/assets/bootstrap.js",
                "/recovery/bootstrap.js",
                "/health",
                "/ready",
                "/api/v1/health",
            }
            exchange = path == "/api/v1/auth/exchange"
            if exchange:
                if supplied_origin != expected:
                    raise TrustError("wrong_origin", 403)
            elif not public:
                authorization = request.headers.get("authorization")
                if authorization is not None:
                    service.trust.bearer(authorization)
                    request.state.identity = "bearer"
                    request.state.recovery = False
                else:
                    session = service.trust.session(request.cookies.get(COOKIE))
                    request.state.identity = "session"
                    request.state.recovery = session.recovery
                    if request.method not in {"GET", "HEAD"}:
                        if supplied_origin != expected:
                            raise TrustError("wrong_origin", 403)
                        if not secrets.compare_digest(
                            request.headers.get("x-csrf-token", "").encode(),
                            session.csrf.encode(),
                        ):
                            raise TrustError("invalid_csrf", 403)
                if path in {"/api/v1/auth/bootstrap", "/api/v1/auth/rotate"}:
                    if request.state.identity != "bearer":
                        raise TrustError("bearer_required", 403)
                recovery_allowed = path in {
                    "/api/v1/recovery/diagnostics",
                    "/api/v1/identity",
                    "/api/v1/auth/bootstrap",
                    "/api/v1/auth/rotate",
                }
                if request.state.recovery and not recovery_allowed:
                    raise TrustError("recovery_session_only", 403)
                if (
                    service._recovery or service._state is None
                ) and not recovery_allowed:
                    raise TrustError("storage_unavailable", 503)
            if request.method in {"POST", "PUT", "PATCH"}:
                if (
                    request.headers.get("content-type", "").split(";")[0]
                    != "application/json"
                ):
                    raise TrustError("json_required", 415)
                # Bound even chunked bodies before Pydantic parsing.
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > body_limit(path, request.method):
                        raise TrustError("request_too_large", 413)
                    body.extend(chunk)
                request._body = bytes(body)
                if (request.method, path) in STRICT_BODY_ROUTES:
                    try:
                        strict_json(request._body)
                    except ValueError as exc:
                        raise TrustError("invalid_request", 422) from exc
            allowed = query_allowlist(path, request.method)
            if allowed is not None:
                keys = [key for key, value in request.query_params.multi_items()]
                if len(keys) != len(set(keys)) or set(keys) - allowed:
                    raise TrustError("invalid_request", 422)
                if (
                    path == "/api/v1/router/requests"
                    and request.method == "OPTIONS"
                    and keys != ["home"]
                ):
                    raise TrustError("invalid_request", 422)
            response = await call_next(request)
        except TrustError as exc:
            response = error_response(exc.code, exc.status)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; connect-src 'self'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                ),
            }
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors may contain input secrets and filesystem details.
        return error_response("invalid_request", 422)

    @app.exception_handler(RouterUnavailable)
    async def router_error(request, exc):
        return error_response(exc.code, exc.status)

    @app.exception_handler(UnsafePathError)
    async def path_error(request, exc):
        return error_response("unsafe_path", 422)

    @app.exception_handler(TrustError)
    async def trust_error(request, exc):
        return error_response(exc.code, exc.status)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(
            "route_not_found" if exc.status_code == 404 else "method_not_allowed",
            exc.status_code,
        )

    def asset(name):
        return files("millrace_os").joinpath("assets", name).read_text()

    def recovery_asset(name):
        return files("millrace_os.recovery").joinpath(name).read_text()

    @app.get("/", include_in_schema=False)
    async def bootstrap():
        try:
            return HTMLResponse(asset("bootstrap.html"))
        except FileNotFoundError:
            return error_response("normal_assets_unavailable", 503)

    @app.get("/recovery", include_in_schema=False)
    async def recovery():
        return HTMLResponse(asset("recovery.html"))

    @app.get("/assets/bootstrap.js", include_in_schema=False)
    async def script():
        try:
            return Response(asset("bootstrap.js"), media_type="text/javascript")
        except FileNotFoundError:
            return error_response("normal_assets_unavailable", 503)

    @app.get("/recovery/bootstrap.js", include_in_schema=False)
    async def recovery_script():
        try:
            return Response(
                recovery_asset("bootstrap.js"), media_type="text/javascript"
            )
        except FileNotFoundError:
            return error_response("recovery_assets_unavailable", 503)

    @app.get("/api/v1/health", response_model=Health)
    @app.get("/health", include_in_schema=False)
    @app.get("/ready", include_in_schema=False)
    async def health():
        data, status = service.health_response()
        return JSONResponse(data, status_code=status)

    @app.post("/api/v1/auth/bootstrap", response_model=BootstrapNonce)
    async def mint(body: BootstrapInput):
        if service._recovery and not body.recovery:
            raise TrustError("storage_unavailable", 503)
        return BootstrapNonce(nonce=service.trust.mint(body.recovery))

    @app.post("/api/v1/auth/exchange", response_model=SessionOutput)
    async def exchange(body: ExchangeInput, response: Response):
        sid, session = service.trust.exchange(body.nonce, body.recovery)
        response.set_cookie(
            COOKIE,
            sid,
            httponly=True,
            samesite="strict",
            max_age=SESSION_TTL,
            path="/",
            secure=False,
        )
        return SessionOutput(csrf=session.csrf)

    @app.post("/api/v1/auth/rotate", response_model=Rotation)
    async def rotate(body: BootstrapInput):
        service.trust.rotate()
        return Rotation()

    @app.get("/api/v1/identity", response_model=Read[Identity])
    async def identity(request: Request):
        return envelope(
            "os.identity.v1",
            Identity(
                authentication=request.state.identity,
                recovery_only=request.state.recovery,
            ),
        )

    def diagnostics():
        return Diagnostics(
            status="recovery" if service._recovery else "ready",
            storage_error=service._recovery.code if service._recovery else None,
        )

    @app.get("/api/v1/diagnostics", response_model=Read[Diagnostics])
    @app.get("/api/v1/recovery/diagnostics", response_model=Read[Diagnostics])
    async def diagnostic():
        return envelope("os.diagnostics.v1", diagnostics())

    @app.get("/api/v1/version", response_model=Read[Version])
    async def version():
        return envelope("os.version.v1", Version())

    @app.get("/api/v1/contracts", response_model=Read[Registry])
    async def contracts():
        return envelope("os.registry.v1", registry)

    @app.get("/api/v1/contracts/{contract_id}/{version}/{digest}")
    async def schema(
        contract_id: Identifier,
        version: Literal["1"],
        digest: Annotated[
            str, Path(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
        ],
    ):
        entry = next(
            (entry for entry in registry.schemas if entry.contract_id == contract_id),
            None,
        )
        if not entry or version != "1" or digest != entry.sha256:
            raise TrustError("schema_not_found", 404)
        return JSONResponse(schemas[contract_id], media_type="application/schema+json")

    def page_check(query):
        if query.cursor is not None:
            raise TrustError("cursor_unsupported", 409)

    @app.get("/api/v1/providers", response_model=Read[Page[ProviderInfo]])
    async def providers(query: Annotated[PageQuery, Query()]):
        page_check(query)
        # No adapter is admitted yet: do not invent positive availability.
        return envelope("os.providers.v1", Page[ProviderInfo](items=[]))

    @app.get("/api/v1/actions", response_model=Read[Page[ActionInfo]])
    async def actions(query: Annotated[PageQuery, Query()]):
        page_check(query)
        return envelope("os.actions.v1", Page[ActionInfo](items=[]))

    @app.post(
        "/api/v1/actions/{action_id}:invoke", response_model=Error, status_code=409
    )
    async def invoke(
        action_id: Identifier,
        body: ActionInput,
        idempotency_key: Annotated[Identifier, Header()],
        x_correlation_id: Annotated[Identifier, Header()],
    ):
        raise TrustError("action_unavailable", 409)

    @app.get(
        "/api/v1/events",
        response_class=StreamingResponse,
        responses={
            200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}
        },
    )
    async def events(request: Request):
        if request.query_params:
            raise TrustError("invalid_request", 422)
        cursor = request.headers.get("last-event-id")
        if cursor is not None:
            if (
                not cursor
                or len(cursor) > 128
                or not all(c.isascii() and (c.isalnum() or c in "_-") for c in cursor)
            ):
                raise TrustError("invalid_event_cursor", 422)
            raise TrustError("event_replay_unavailable", 409)
        # A single small availability notification then close: no durable history,
        # event IDs, heartbeat loop, queued provider messages, or retained buffers.
        if service.event_connections >= 8:
            raise TrustError("event_capacity", 429)
        service.event_connections += 1

        async def notification():
            try:
                yield "event: availability\ndata: " + Event().model_dump_json() + "\n\n"
            finally:
                service.event_connections -= 1

        return StreamingResponse(notification(), media_type="text/event-stream")

    configure_router_api(app, service)

    # Document uniform typed refusals without FastAPI's input-echoing 422 schema.
    for route in app.routes:
        if getattr(route, "include_in_schema", False):
            for status in (401, 403, 409, 413, 415, 422, 429, 503):
                route.responses.setdefault(
                    status, {"model": Error, "description": "Typed refusal"}
                )


def configure_router_api(app, service):
    """Bind the small public router surface to the same owned service calls."""

    read_slots = asyncio.Semaphore(8)

    async def offload(function, *args, **kwargs):
        if read_slots.locked():
            raise RouterUnavailable("router_read_busy", 429)
        async with read_slots:
            return await asyncio.to_thread(function, *args, **kwargs)

    async def wait_owned(call, *, version=1):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + service.router_workers.observation_timeout
        while not call.done.is_set() and loop.time() < deadline:
            await asyncio.sleep(min(0.01, max(0, deadline - loop.time())))
        return service.router_workers.call_observation(call, version=version)

    def core_read(home, data):
        return RouterRead(
            contract="millrace-os-router-read/v1",
            home=home,
            data=data,
            source_provider="core",
            authority="core-state-home",
            observed_at=datetime.now(UTC).isoformat(),
        )

    def local_read(home, data):
        return RouterLocalRead(
            home=home, data=data, observed_at=datetime.now(UTC).isoformat()
        )

    def bounded(model, *, status_code=200, media_type="application/json"):
        # Starlette renders the API encoding (compact UTF-8) measured here.
        response = JSONResponse(
            model.model_dump(by_alias=True),
            status_code=status_code,
            media_type=media_type,
        )
        if len(response.body) > RESPONSE_LIMIT:
            raise RouterUnavailable("response_too_large", 409)
        return response

    def view(home, data):
        return bounded(
            RouterView(home=home, data=data, observed_at=datetime.now(UTC).isoformat())
        )

    def status(observation):
        receipt = observation.core_receipt
        return (
            200
            if receipt and receipt.get("status") in {"applied", "refused"}
            else 202
            if observation.phase in {"admission_pending", "call_active"}
            or (receipt and receipt.get("status") == "queued")
            else 200
        )

    async def local_call(
        home, operation, body, callback, *, instance_id=None, run_id=None
    ):
        request = {
            "format": "millrace-os-local-request/v1",
            "operation": operation,
            "instance_id": instance_id,
            "run_id": run_id,
            "plane_id": None,
            "request_id": body.get("request_id") or uuid.uuid4().hex,
            "expected_state_version": body.get("base_state_version"),
            "payload": body,
        }
        call = service.router_workers.submit(
            home, request, operation=callback, bypass_guard=True
        )
        observation = await wait_owned(call)
        if call.error:
            return JSONResponse(call.error.model_dump(), status_code=call.error_status)
        if call.done.is_set():
            result = (
                call.result.model_dump()
                if hasattr(call.result, "model_dump")
                else call.result
            )
            return local_read(home, result)
        return JSONResponse(
            local_read(home, {"observation": observation.model_dump()}).model_dump(),
            status_code=202,
        )

    @app.get("/api/v1/router/instances", response_model=RouterRead[JsonValue])
    async def router_instances(home: Identifier):
        return core_read(
            home, await offload(service.router_client.list_instances, home)
        )

    @app.get(
        "/api/v1/router/instances/{instance_id}", response_model=RouterRead[JsonValue]
    )
    async def router_instance(instance_id: Identifier, home: Identifier):
        return core_read(
            home,
            await offload(service.router_client.inspect_instance, home, instance_id),
        )

    @app.get(
        "/api/v1/router/instances/{instance_id}/runs/{run_id}",
        response_model=RouterRead[JsonValue],
    )
    async def router_run(instance_id: Identifier, run_id: Identifier, home: Identifier):
        return core_read(
            home,
            await offload(service.router_client.inspect_run, home, instance_id, run_id),
        )

    @app.get("/api/v1/router/homes", response_model=RouterHomes)
    async def router_homes(query: Annotated[HomesQuery, Query()]):
        page = service.router_client.list_homes(limit=query.limit, cursor=query.cursor)
        return bounded(
            RouterHomes(
                items=[RouterHomeItem(name=name) for name in page["items"]],
                next_cursor=page["next_cursor"],
                observed_at=datetime.now(UTC).isoformat(),
            )
        )

    @app.get("/api/v1/router/views/instances", response_model=RouterView)
    async def view_instances(query: Annotated[ViewPageQuery, Query()]):
        data = await offload(
            service.router_client.view_instances,
            query.home,
            limit=query.limit,
            cursor=query.cursor,
        )
        return view(query.home, data)

    @app.get(
        "/api/v1/router/views/instances/{instance_id}", response_model=RouterView
    )
    async def view_instance(
        instance_id: Identifier, query: Annotated[ViewPageQuery, Query()]
    ):
        data = await offload(
            service.router_client.view_instance,
            query.home,
            instance_id,
            limit=query.limit,
            cursor=query.cursor,
        )
        return view(query.home, data)

    @app.get(
        "/api/v1/router/views/instances/{instance_id}/runs/{run_id}",
        response_model=RouterView,
    )
    async def view_run(
        instance_id: Identifier,
        run_id: Identifier,
        query: Annotated[RunViewQuery, Query()],
    ):
        data = await offload(
            service.router_client.view_run,
            query.home,
            instance_id,
            run_id,
            section=query.section,
            limit=query.limit,
            cursor=query.cursor,
            request_id=query.request_id,
            invocation_id=query.invocation_id,
        )
        return view(query.home, data)

    @app.get(
        "/api/v1/router/views/instances/{instance_id}/runs/{run_id}"
        "/revisions/{revision_id}",
        response_model=RouterView,
    )
    async def view_revision(
        instance_id: Identifier,
        run_id: Identifier,
        revision_id: Annotated[
            str, Path(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
        ],
        home: Identifier,
    ):
        data = await offload(
            service.router_client.view_revision, home, instance_id, run_id, revision_id
        )
        return view(home, data)

    @app.post("/api/v1/router/outputs", response_model=WorkflowOutputPublication)
    async def output_publish(body: WorkflowOutputPublishInput):
        # The trust boundary already refused duplicate keys and NaN/Infinity.
        if service.router_workers._closing:
            raise RouterUnavailable("router_closing", 503)
        result = await offload(
            service.router_client.publish_output, body.home, body.record
        )
        return bounded(
            WorkflowOutputPublication(
                home=body.home, observed_at=datetime.now(UTC).isoformat(), **result
            )
        )

    @app.get(
        "/api/v1/router/outputs/instances/{instance_id}/runs/{run_id}",
        response_model=WorkflowOutputs,
    )
    async def output_list(
        instance_id: Identifier,
        run_id: Identifier,
        query: Annotated[ViewPageQuery, Query()],
    ):
        page = await offload(
            service.router_client.list_outputs,
            query.home,
            instance_id,
            run_id,
            limit=query.limit,
            cursor=query.cursor,
        )
        return bounded(
            WorkflowOutputs(
                home=query.home, observed_at=datetime.now(UTC).isoformat(), **page
            )
        )

    @app.get(
        "/api/v1/router/outputs/instances/{instance_id}/runs/{run_id}/{output_id}",
        response_model=WorkflowOutput,
    )
    async def output_read(
        instance_id: Identifier,
        run_id: Identifier,
        output_id: Identifier,
        query: Annotated[OutputReadQuery, Query()],
    ):
        result = await offload(
            service.router_client.read_output,
            query.home,
            instance_id,
            run_id,
            output_id,
            query.record_sha256,
            query.representation,
        )
        return bounded(
            WorkflowOutput(
                home=query.home, observed_at=datetime.now(UTC).isoformat(), **result
            )
        )

    def observation_version(accept):
        selected = ", ".join(accept or []).strip()
        if selected == CALL_V2_MEDIA_TYPE:
            return 2
        if "application/vnd.millrace.router-call" in selected.lower():
            if selected != "application/vnd.millrace.router-call.v1+json":
                raise RouterUnavailable("unsupported_observation_version", 406)
        return 1

    def observation_response(observation, version):
        return JSONResponse(
            observation.model_dump(),
            status_code=status(observation),
            media_type=CALL_V2_MEDIA_TYPE if version == 2 else "application/json",
        )

    @app.options(
        "/api/v1/router/requests", response_model=RouterObservationCapabilities
    )
    async def router_observation_capabilities(home: Identifier):
        service.router_client.state_home(home)
        return RouterObservationCapabilities(
            home=home,
            observation_contracts=[
                "millrace-os-router-call/v1", "millrace-os-router-call/v2"
            ],
            holder_joined_supported=True,
        )

    @app.post(
        "/api/v1/router/requests",
        response_model=RouterCallObservation | RouterCallObservationV2,
        responses={202: {"model": RouterCallObservation | RouterCallObservationV2}},
    )
    async def router_request(
        body: RouterRequestInput, accept: Annotated[list[str] | None, Header()] = None
    ):
        version = observation_version(accept)
        call = service.router_workers.submit(body.home, body.request)
        observation = await wait_owned(call, version=version)
        if call.error:
            return JSONResponse(call.error.model_dump(), status_code=call.error_status)
        return observation_response(observation, version)

    @app.get(
        "/api/v1/router/requests/{request_id}",
        response_model=RouterCallObservation | RouterCallObservationV2,
        responses={202: {"model": RouterCallObservation | RouterCallObservationV2}},
    )
    async def router_observe(
        request_id: Identifier,
        home: Identifier,
        instance_id: Identifier | None = None,
        run_id: Identifier | None = None,
        accept: Annotated[list[str] | None, Header()] = None,
    ):
        version = observation_version(accept)
        if run_id is not None and instance_id is None:
            raise RouterUnavailable("invalid_request", 422)
        observation = await offload(
            service.router_workers.observe,
            home,
            request_id,
            instance_id=instance_id,
            run_id=run_id,
            version=version,
        )
        return observation_response(observation, version)

    @app.get(
        "/api/v1/router/views/requests/{request_id}",
        response_model=RouterCallObservation | RouterCallObservationV2,
        responses={202: {"model": RouterCallObservation | RouterCallObservationV2}},
    )
    async def view_request(
        request_id: Identifier,
        home: Identifier,
        instance_id: Identifier,
        run_id: Identifier | None = None,
        accept: Annotated[list[str] | None, Header()] = None,
    ):
        # Strict bounded observation only: no legacy full-inspection fallback.
        version = observation_version(accept)
        observation = await offload(
            service.router_workers.observe,
            home,
            request_id,
            instance_id=instance_id,
            run_id=run_id,
            version=version,
            bounded=True,
        )
        return bounded(
            observation,
            status_code=status(observation),
            media_type=CALL_V2_MEDIA_TYPE if version == 2 else "application/json",
        )

    @app.post("/api/v1/router/views/validate", response_model=RouterView)
    async def view_validate(body: RouterViewValidateInput):
        data = await offload(
            service.router_client.view_validate,
            body.home,
            body.instance_id,
            run_id=body.run_id,
        )
        return view(body.home, data)

    @app.post(
        "/api/v1/router/validate",
        response_model=RouterRead[RouterValidation],
        response_model_exclude_unset=True,
    )
    async def router_validate(body: RouterValidateInput):
        workflow = (
            service.workflow_drafts.workflow_file(body.home, body.draft_id)
            if body.draft_id
            else None
        )
        data = await offload(
            service.router_client.validate_revision,
            body.home,
            body.instance_id,
            run_id=body.run_id,
            workflow_file=workflow,
        )
        RouterValidation.model_validate(data)
        return core_read(body.home, data)

    @app.get("/api/v1/router/catalog/templates", response_model=RouterLocalRead)
    async def catalog_list(home: Identifier):
        return local_read(
            home,
            await offload(service.router_client.catalog, home, "list_templates"),
        )

    @app.post(
        "/api/v1/router/catalog/init",
        response_model=RouterLocalRead,
        responses={202: {"model": RouterLocalRead}},
    )
    async def catalog_init(body: CatalogInitInput):
        return await local_call(
            body.home,
            "catalog_init",
            body.model_dump(),
            lambda: service.router_client.catalog(
                body.home,
                "init_template",
                selector=body.selector,
                destination=FilePath(body.destination),
            ),
        )

    @app.post(
        "/api/v1/router/catalog/copy",
        response_model=RouterLocalRead,
        responses={202: {"model": RouterLocalRead}},
    )
    async def catalog_copy(body: CatalogCopyInput):
        if (body.source is None) == (body.draft_id is None):
            raise RouterUnavailable("invalid_request", 422)

        def copy_draft():
            source = (
                service.workflow_drafts.workflow_file(body.home, body.draft_id).parent
                if body.draft_id
                else FilePath(body.source)
            )
            return service.router_client.catalog(
                body.home,
                "copy_workflow",
                source=source,
                destination=FilePath(body.destination),
            )

        return await local_call(
            body.home, "catalog_copy", body.model_dump(), copy_draft
        )

    @app.post("/api/v1/router/drafts", response_model=DraftRead)
    async def draft_create(body: DraftCreateInput):
        if service.router_workers._closing:
            raise RouterUnavailable("router_closing", 503)
        return await offload(service.workflow_drafts.create, body.home, body.files)

    @app.get("/api/v1/router/drafts/{draft_id}", response_model=DraftRead)
    async def draft_read(draft_id: Identifier, home: Identifier):
        return await offload(service.workflow_drafts.read, home, draft_id)

    @app.put("/api/v1/router/drafts/{draft_id}/files", response_model=DraftRead)
    async def draft_files(draft_id: Identifier, body: DraftFilesInput):
        if service.router_workers._closing:
            raise RouterUnavailable("router_closing", 503)
        return await offload(
            service.workflow_drafts.put, body.home, draft_id, body.files, replace=True
        )

    @app.put("/api/v1/router/drafts/{draft_id}/file", response_model=DraftRead)
    async def draft_file(draft_id: Identifier, body: DraftFileInput):
        if service.router_workers._closing:
            raise RouterUnavailable("router_closing", 503)
        return await offload(
            service.workflow_drafts.put,
            body.home,
            draft_id,
            [DraftFile(path=body.path, content_base64=body.content_base64)],
        )

    @app.post(
        "/api/v1/router/drafts/{draft_id}/validate",
        response_model=RouterRead[RouterValidation],
        response_model_exclude_unset=True,
    )
    async def draft_validate(draft_id: Identifier, body: DraftValidateInput):
        data = await offload(
            service.workflow_drafts.validate,
            body.home,
            draft_id,
            body.instance_id,
            body.run_id,
        )
        RouterValidation.model_validate(data)
        return core_read(body.home, data)

    @app.post("/api/v1/router/drafts/{draft_id}/apply", response_model=RouterLocalRead)
    async def draft_apply(draft_id: Identifier, body: DraftApplyInput):
        return await local_call(
            body.home,
            "draft_apply",
            body.model_dump(),
            lambda: service.workflow_drafts.apply(
                body.home,
                draft_id,
                body.instance_id,
                body.run_id,
                body.base_state_version,
                body.request_id,
            ),
            instance_id=body.instance_id,
            run_id=body.run_id,
        )

    @app.post(
        "/api/v1/router/repair-publication",
        response_model=RouterLocalRead,
        responses={202: {"model": RouterLocalRead}},
    )
    async def publication_repair(body: PublicationRepairInput):
        return await local_call(
            body.home,
            "repair_publication",
            body.model_dump(),
            lambda: service.workflow_drafts.repair(body.home, body.instance_id),
            instance_id=body.instance_id,
        )


def export_artifacts(app):
    schemas, registry = schema_artifacts()
    document = copy.deepcopy(app.openapi())
    document["jsonSchemaDialect"] = "https://json-schema.org/draft/2020-12/schema"
    # Added responses after route construction need explicit schema references.
    document.setdefault("components", {}).setdefault("schemas", {})["Error"] = (
        Error.model_json_schema()
    )
    document["components"]["securitySchemes"] = {
        "Bearer": {"type": "http", "scheme": "bearer"},
        "Session": {"type": "apiKey", "in": "cookie", "name": COOKIE},
    }
    for name, path in document["paths"].items():
        for method, operation in path.items():
            if name not in {"/api/v1/health", "/api/v1/auth/exchange"}:
                operation["security"] = [{"Bearer": []}, {"Session": []}]
            if name in {"/api/v1/auth/bootstrap", "/api/v1/auth/rotate"}:
                operation["security"] = [{"Bearer": []}]
            if method in {"post", "put", "patch"}:
                operation.setdefault("requestBody", {})["x-max-body-bytes"] = (
                    body_limit(name, method)
                )
                operation.setdefault("parameters", []).append(
                    {
                        "name": "Origin",
                        "in": "header",
                        "required": name == "/api/v1/auth/exchange",
                        "description": (
                            "Exact configured origin; required for browser mutations."
                        ),
                        "schema": {"type": "string", "maxLength": 256},
                    }
                )
                if name.startswith(("/api/v1/actions/", "/api/v1/router/")):
                    operation["parameters"].append(
                        {
                            "name": "X-CSRF-Token",
                            "in": "header",
                            "required": False,
                            "description": "Required with session authentication.",
                            "schema": {
                                "type": "string",
                                "minLength": 43,
                                "maxLength": 64,
                            },
                        }
                    )
            if name == "/api/v1/router/requests" and method == "options":
                operation.setdefault("parameters", []).extend(
                    [
                        {
                            "name": "Origin", "in": "header", "required": False,
                            "description": (
                                "Exact configured origin required with session "
                                "authentication."
                            ),
                            "schema": {"type": "string", "maxLength": 256},
                        },
                        {
                            "name": "X-CSRF-Token", "in": "header", "required": False,
                            "description": "Required with session authentication.",
                            "schema": {
                                "type": "string", "minLength": 43, "maxLength": 64
                            },
                        },
                    ]
                )
            if name in {
                "/api/v1/router/requests",
                "/api/v1/router/requests/{request_id}",
                "/api/v1/router/views/requests/{request_id}",
            } and method in {"get", "post"}:
                for code in ("200", "202"):
                    content = operation["responses"][code].setdefault("content", {})
                    for media, model in (
                        ("application/json", "RouterCallObservation"),
                        (CALL_V2_MEDIA_TYPE, "RouterCallObservationV2"),
                    ):
                        content[media] = {
                            "schema": {"$ref": "#/components/schemas/" + model}
                        }
                operation["responses"]["406"] = {
                    "description": "Unsupported observation version",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"}
                        }
                    },
                }
            if name == "/api/v1/events":
                operation["parameters"] = [
                    {
                        "name": "Last-Event-ID",
                        "in": "header",
                        "required": False,
                        "description": (
                            "Valid cursors refuse 409: replay is not implemented."
                        ),
                        "schema": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": "^[A-Za-z0-9_-]+$",
                        },
                    }
                ]

            for status in (
                "401",
                "403",
                "404",
                "409",
                "413",
                "415",
                "422",
                "429",
                "503",
            ):
                operation["responses"].setdefault(
                    status, {"description": "Typed refusal"}
                )["content"] = {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/Error"}
                    }
                }
    document["paths"]["/api/v1/health"]["get"]["responses"]["503"] = {
        "description": "Not ready",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Health"}}
        },
    }
    return {
        "contracts.json": canonical(schemas),
        "registry.json": canonical(registry.model_dump()),
        "openapi.json": canonical(document),
    }
