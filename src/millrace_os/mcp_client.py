"""Bounded fixed-path OS API client; local credentials never cross outward."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import httpx
from pydantic import ValidationError

from .contracts import Error
from .mcp_contracts import MAX_OUTPUT, MCPConfig, access_error
from .paths import validate_file_target

__all__ = ["BackendClient"]


def _credential(path: str) -> str:
    target, exists = validate_file_target(Path(path))
    if not exists:
        raise ValueError("credential unavailable")
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise ValueError("credential unavailable")
        raw = os.read(fd, 4097)
        value = json.loads(raw)
        token = value["token"]
        if (
            set(value) != {"token"}
            or not isinstance(token, str)
            or not 32 <= len(token) <= 256
            or any(
                c
                not in (
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                )
                for c in token
            )
        ):
            raise ValueError("credential unavailable")
        return token
    finally:
        os.close(fd)


class BackendClient:
    """Own one HTTP pool, never the OS daemon or its workers."""

    def __init__(self, config: MCPConfig, *, transport=None) -> None:
        self.config = config
        self.http = httpx.AsyncClient(
            base_url=config.os_origin,
            timeout=5,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self.version: dict = {}
        self.contracts: dict = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(
        self, method: str, path: str, *, params=None, body=None, correlation=None
    ) -> dict:
        try:
            token = await asyncio.to_thread(_credential, self.config.os_credential_file)
        except (OSError, ValueError, KeyError, TypeError):
            return access_error("local_credential_unavailable", correlation=correlation)
        try:
            async with (
                asyncio.timeout(5),
                self.http.stream(
                    method,
                    path,
                    params=params,
                    json=body,
                    headers={"Authorization": "Bearer " + token},
                ) as response,
            ):
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_OUTPUT:
                        return access_error(
                            "backend_response_too_large", correlation=correlation
                        )
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    code, retryable = "backend_http_" + str(status), status == 429
                    try:
                        error = Error.model_validate_json(chunks)
                        code = error.code
                        retryable = retryable or error.retryable
                    except ValidationError:
                        pass
                    return access_error(
                        code,
                        status=status,
                        retryable=retryable,
                        correlation=correlation,
                    )
                data = json.loads(chunks)
                if not isinstance(data, dict):
                    raise ValueError("invalid envelope")
                return data
        except (httpx.TimeoutException, TimeoutError):
            return access_error("backend_timeout", correlation=correlation)
        except (httpx.HTTPError, ValueError, UnicodeError):
            return access_error("backend_unavailable", correlation=correlation)

    async def startup(self) -> None:
        self.version = await self._request("GET", "/api/v1/version")
        self.contracts = await self._request("GET", "/api/v1/contracts")
        version = self.version.get("data", {})
        registry = self.contracts.get("data", {})
        if (
            not isinstance(version, dict)
            or not isinstance(registry, dict)
            or not isinstance(registry.get("schemas"), list)
        ):
            raise ValueError("unsupported backend contracts")
        ids = {
            x.get("contract_id")
            for x in registry.get("schemas", [])
            if isinstance(x, dict)
            and type(x.get("version")) is int
            and x.get("version") == 1
        }
        if (
            version.get("api") != "v1"
            or version.get("package") != "millrace-os"
            or registry.get("registry") != "os.registry.v1"
            or not {"RouterRead-JsonValue", "RouterCallObservation"} <= ids
        ):
            raise ValueError("unsupported backend contracts")

    async def call(self, name: str, args: dict) -> dict:
        home = args["home"]
        params = {"home": home}
        correlation = {
            k: args[k]
            for k in ("home", "instance_id", "run_id", "request_id")
            if k in args
        }
        if name == "millrace_v1_request":
            request = args["request"]
            correlation.update(
                {
                    k: request[k]
                    for k in ("request_id", "instance_id", "run_id")
                    if k in request
                }
            )
            return await self._request(
                "POST", "/api/v1/router/requests", body=args, correlation=correlation
            )
        if name == "millrace_v1_validate":
            return await self._request(
                "POST", "/api/v1/router/validate", body=args, correlation=correlation
            )
        if name == "millrace_v1_receipt":
            params.update(
                {
                    k: args[k]
                    for k in ("instance_id", "run_id")
                    if k in args and args[k] is not None
                }
            )
            path = "/api/v1/router/requests/" + args["request_id"]
        else:
            path = "/api/v1/router/instances"
            if "instance_id" in args:
                path += "/" + args["instance_id"]
            if "run_id" in args:
                path += "/runs/" + args["run_id"]
        return await self._request("GET", path, params=params, correlation=correlation)
