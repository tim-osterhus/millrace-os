"""Versioned, bounded foundations with trust checked before route handlers."""

from __future__ import annotations

import copy
import secrets
from datetime import UTC, datetime
from importlib.resources import files
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException

from .auth import COOKIE, SESSION_TTL, TrustError
from .config import _endpoint_host
from .contracts import (
    ActionInfo,
    ActionInput,
    BootstrapInput,
    BootstrapNonce,
    Diagnostics,
    Error,
    Event,
    ExchangeInput,
    Health,
    Identifier,
    Identity,
    Page,
    PageQuery,
    ProviderInfo,
    Read,
    Registry,
    Rotation,
    SessionOutput,
    Version,
    canonical,
    schema_artifacts,
)


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
    payload = Error(code=code, category=category, summary=code.replace("_", " "))
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
                    if len(body) + len(chunk) > 4096:
                        raise TrustError("request_too_large", 413)
                    body.extend(chunk)
                request._body = bytes(body)
            if request.query_params and path not in {
                "/api/v1/providers",
                "/api/v1/actions",
            }:
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

    # Document uniform typed refusals without FastAPI's input-echoing 422 schema.
    for route in app.routes:
        if getattr(route, "include_in_schema", False):
            for status in (401, 403, 409, 413, 415, 422, 429, 503):
                route.responses.setdefault(
                    status, {"model": Error, "description": "Typed refusal"}
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
            if method == "post":
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
                if name.startswith("/api/v1/actions/"):
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
