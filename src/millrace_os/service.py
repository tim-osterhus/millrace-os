"""Local FastAPI service lifecycle and OS-owned dependency boundaries."""

from __future__ import annotations

import errno
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import uvicorn
from fastapi import FastAPI

from .config import ApplicationConfig, _endpoint_host
from .credentials import CredentialStore
from .recovery.runtime import RecoveryRuntime
from .storage.database import SQLiteState
from .storage.errors import StateError


class PortCollisionError(RuntimeError):
    """Raised when the configured loopback port is already bound."""

    code = "port_collision"


class BindError(RuntimeError):
    """Raised when the configured local endpoint cannot be bound."""

    code = "bind_error"


class ServiceStartupError(RuntimeError):
    """Raised when an ASGI server cannot become ready."""

    code = "service_startup_error"


class ServiceLifecycleError(RuntimeError):
    """Raised when the service lifecycle cannot advance safely."""

    code = "service_lifecycle_error"


class ProviderProcessBoundary(Protocol):
    """Marker boundary; service shutdown never owns this process lifecycle."""


@dataclass(slots=True)
class ServiceDependencies:
    provider_processes: ProviderProcessBoundary | None = None
    state_factory: Callable[[Path], SQLiteState] = SQLiteState.open
    router_client: object | None = None


def _public_liveness(service: Service) -> tuple[dict[str, str], int]:
    if service.lifecycle != "running" or service._state is None:
        return {"status": "unavailable"}, 503
    if service._recovery is not None:
        return {"status": "unavailable"}, 503
    return {"status": "ok"}, 200


def create_app(service: Service) -> FastAPI:
    """Create the P01 app with only public-safe liveness routes."""

    app = FastAPI(
        title="Millrace OS",
        version="0.23.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    from .api import configure_api

    configure_api(app, service)

    return app


class Service:
    """Own HTTP and OS-local state, but never provider-hosted processes."""

    def __init__(
        self,
        config: ApplicationConfig,
        *,
        dependencies: ServiceDependencies | None = None,
    ) -> None:
        self.config = config
        self.dependencies = dependencies or ServiceDependencies()
        from .auth import LocalTrust

        self.trust = LocalTrust(CredentialStore(config.credential_path))
        self.event_connections = 0
        from .router_client import RouterClient
        from .router_workers import RouterWorkers
        from .workflow_drafts import WorkflowDrafts

        self.router_client = self.dependencies.router_client or RouterClient(
            config.router_state_homes
        )
        self.router_workers = RouterWorkers(self.router_client)
        self.workflow_drafts = WorkflowDrafts(
            config.state_dir, self.router_client, self.router_workers
        )
        self.router_workers.admission_guard = self.workflow_drafts.acquire
        self._shutdown_thread: threading.Thread | None = None
        self._shutdown_lock = threading.Lock()
        self.app = create_app(self)
        self._server: uvicorn.Server | None = None
        self._server_socket: socket.socket | None = None
        self._server_thread: threading.Thread | None = None
        self._server_error: BaseException | None = None
        self._state: SQLiteState | None = None
        self._recovery: RecoveryRuntime | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._lifecycle = "stopped"

    @property
    def lifecycle(self) -> str:
        return self._lifecycle

    @property
    def state(self) -> SQLiteState | None:
        return self._state

    @property
    def bound_port(self) -> int:
        if self._server_socket is None:
            raise ServiceLifecycleError("service has not started")
        return int(self._server_socket.getsockname()[1])

    @property
    def browser_endpoint(self) -> str:
        return f"http://{_endpoint_host(self.config.host)}:{self.bound_port}/"

    @property
    def health_endpoint(self) -> str:
        return f"http://{_endpoint_host(self.config.host)}:{self.bound_port}/health"

    @property
    def readiness_endpoint(self) -> str:
        return f"http://{_endpoint_host(self.config.host)}:{self.bound_port}/ready"

    @property
    def recovery_endpoint(self) -> str:
        return f"http://{_endpoint_host(self.config.host)}:{self.bound_port}/recovery"

    def _bind_socket(self) -> socket.socket:
        try:
            addresses = socket.getaddrinfo(
                self.config.host,
                self.config.port,
                type=socket.SOCK_STREAM,
                flags=socket.AI_PASSIVE,
            )
        except OSError as exc:
            raise BindError(f"could not resolve local bind {self.config.host}") from exc
        if not addresses:
            raise BindError(f"could not resolve local bind {self.config.host}")

        # Use the first address for a literal/localhost loopback endpoint.  No
        # fallback to another address or port is allowed after a bind error.
        family, socktype, protocol, _canonname, sockaddr = addresses[0]
        server_socket = socket.socket(family, socktype, protocol)
        try:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind(sockaddr)
            server_socket.listen(socket.SOMAXCONN)
            server_socket.setblocking(False)
            return server_socket
        except OSError as exc:
            server_socket.close()
            if exc.errno == errno.EADDRINUSE:
                raise PortCollisionError(
                    f"port {self.config.host}:{self.config.port} is already in use"
                ) from exc
            raise BindError(
                f"could not bind {self.config.host}:{self.config.port}: "
                f"{exc.strerror or exc}"
            ) from exc

    def start(self) -> None:
        if self._lifecycle != "stopped":
            raise ServiceLifecycleError("service is already started")
        self._recovery = None
        self._shutdown_thread = None
        self.router_workers.reopen()
        self._server_error = None
        self._ready.clear()
        self._stopped.clear()
        self.config.ensure_directories()
        self.trust.reset()
        try:
            self._state = self.dependencies.state_factory(self.config.database_path)
        except StateError as error:
            self._state = None
            self._recovery = RecoveryRuntime.from_error(error)

        try:
            server_socket = self._bind_socket()
        except BaseException:
            self._close_state()
            self._recovery = None
            raise
        try:
            config = uvicorn.Config(
                self.app,
                host=self.config.host,
                port=self.config.port,
                fd=server_socket.fileno(),
                log_level="error",
                access_log=False,
                lifespan="on",
            )
            server = uvicorn.Server(config)
            self._server_socket = server_socket
            self._server = server
            self._lifecycle = "starting"
            self._server_thread = threading.Thread(
                target=self._serve,
                args=(server, server_socket),
                name="millrace-os-asgi",
                daemon=True,
            )
            self._server_thread.start()
            tick = threading.Event()
            # Poll in short intervals so a failed ASGI startup is surfaced
            # synchronously and cannot emit a false READY line.
            for _ in range(500):
                if server.started:
                    self._lifecycle = "running"
                    self._ready.set()
                    return
                if self._server_thread is None or not self._server_thread.is_alive():
                    break
                tick.wait(0.01)
            error = self._server_error
            self._abort_server()
            self._close_state()
            self._lifecycle = "stopped"
            if isinstance(error, BaseException):
                raise ServiceStartupError(
                    "ASGI server failed during startup"
                ) from error
            raise ServiceStartupError("ASGI server did not become ready")
        except BaseException:
            if self._server is None:
                server_socket.close()
            raise

    def _serve(self, server: uvicorn.Server, server_socket: socket.socket) -> None:
        try:
            server.run(sockets=[server_socket])
        except BaseException as error:
            self._server_error = error

    def _abort_server(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._server_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive() and server is not None:
                server.force_exit = True
                thread.join(timeout=1)
        server_socket = self._server_socket
        if server_socket is not None:
            try:
                server_socket.close()
            except OSError:
                pass

    def _close_state(self) -> None:
        if self._state is not None:
            self._state.close()
        self._state = None

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def wait_for_stop(self, timeout: float | None = None) -> bool:
        return self._stopped.wait(timeout)

    def request_shutdown(self, *, cancel: bool = False) -> None:
        """Set closing intent and start one owned drain coordinator without joining."""
        with self._shutdown_lock:
            if self._lifecycle == "stopped":
                return
            repeated = self._shutdown_thread is not None
            self._lifecycle = "closing"
            self.router_workers.request_shutdown(cancel=cancel or repeated)
            if repeated:
                return
            self._shutdown_thread = threading.Thread(
                target=self._drain_and_stop, name="millrace-os-drain", daemon=False
            )
            self._shutdown_thread.start()

    def _drain_and_stop(self) -> None:
        self.router_workers.drain()
        self._finish_stop()

    def stop(self) -> None:
        """Drain router calls before closing HTTP and OS state."""
        if self._lifecycle == "stopped":
            return
        # Calling stop to await a previously requested drain must not turn a
        # graceful pause into the explicit second-shutdown cancellation intent.
        if self._shutdown_thread is None:
            self.request_shutdown()
        thread = self._shutdown_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _finish_stop(self) -> None:
        self._lifecycle = "stopping"
        self._abort_server()
        self._server = None
        self._server_socket = None
        self._server_thread = None
        self._close_state()
        self._recovery = None
        self._ready.clear()
        self._stopped.set()
        self._lifecycle = "stopped"

    def run_forever(self) -> None:
        self.start()
        try:
            while self._lifecycle in {"running", "closing"}:
                if self._server_thread is not None:
                    self._server_thread.join(timeout=0.5)
        finally:
            self.stop()

    def health_response(self) -> tuple[dict[str, str], int]:
        """Return only the public-safe P01 liveness projection."""

        return _public_liveness(self)

    def recovery_shell(self) -> str:
        """Return a static shell without state, schema, or diagnostics."""

        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Millrace OS</title></head><body>"
            "<main><h1>Millrace OS</h1>"
            "<p>Authentication is required for service details.</p>"
            "</main></body></html>"
        )
