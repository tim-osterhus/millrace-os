"""External resource JWT verification and all-route HTTP admission."""

from __future__ import annotations

import asyncio
import json
import math
import time
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

from .mcp_contracts import MAX_INPUT, MAX_OUTPUT, PROTOCOL, MCPConfig

__all__ = ["ResourceVerifier", "HTTPGuard"]


class ResourceVerifier:
    """Accept only configured RS256 operator principals; own a bounded JWKS cache."""

    def __init__(self, config: MCPConfig, *, transport=None) -> None:
        self.config = config
        self.http = httpx.AsyncClient(
            timeout=5, follow_redirects=False, trust_env=False, transport=transport
        )
        self.keys: dict = {}
        self.loaded = 0.0
        self.next_refresh = 0.0
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        await self.http.aclose()

    async def _refresh(self) -> None:
        async with asyncio.timeout(5):
            async with self.http.stream("GET", self.config.jwks_url) as response:
                if response.status_code != 200:
                    raise ValueError("key unavailable")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 262144:
                        raise ValueError("key response too large")
                keys = json.loads(raw)["keys"]
                if not isinstance(keys, list) or len(keys) > 64:
                    raise ValueError("invalid keys")
                selected = {}
                for key in keys:
                    if not (
                        key.get("kty") == "RSA"
                        and key.get("alg", "RS256") == "RS256"
                        and key.get("use", "sig") == "sig"
                    ):
                        continue
                    kid = key.get("kid")
                    if not isinstance(kid, str) or not kid or kid in selected:
                        raise ValueError("invalid key identity")
                    selected[kid] = jwt.PyJWK.from_dict(key, algorithm="RS256").key
                self.keys = selected
                self.loaded = time.monotonic()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            if not isinstance(token, str) or len(token.encode()) > 16384:
                return None
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256"
                or not isinstance(kid, str)
                or not kid
                or any(k in header for k in ("jku", "x5u", "crit", "jwk"))
            ):
                return None
            now = time.monotonic()
            key = self.keys.get(kid) if now - self.loaded < 300 else None
            if key is None:
                async with self.lock:
                    now = time.monotonic()
                    key = self.keys.get(kid) if now - self.loaded < 300 else None
                    if key is None:
                        if now < self.next_refresh:
                            return None
                        # Gate attempts before I/O, including failed/cancelled fetches.
                        self.next_refresh = now + 30
                        await self._refresh()
                        key = self.keys.get(kid)
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.config.issuer_url,
                audience=self.config.resource_url,
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud"],
                    "strict_aud": True,
                },
                leeway=0,
            )
            now = time.time()
            for name in ("exp", "iat", "nbf"):
                if name in claims:
                    value = claims[name]
                    if type(value) not in {int, float} or not math.isfinite(value):
                        return None
                    if (name == "exp" and value <= now) or (
                        name != "exp" and value > now
                    ):
                        return None
            subject, scope = claims["sub"], claims.get("scope")
            if (
                subject not in self.config.trusted_subjects
                or not isinstance(scope, str)
                or "millrace:operator" not in scope.split()
            ):
                return None
            return AccessToken(
                token=token,
                client_id=subject,
                subject=subject,
                scopes=scope.split(),
                expires_at=int(claims["exp"]),
                resource=self.config.resource_url,
            )
        except (
            jwt.PyJWTError,
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            return None


class HTTPGuard:
    """Bound all routes before SDK parsing, with exact Host/Origin and CORS."""

    def __init__(self, app, config: MCPConfig) -> None:
        self.app, self.config = app, config
        self.path = urlsplit(config.resource_url).path or "/mcp"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: dict[bytes, list[bytes]] = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.lower(), []).append(value)
        origin = headers.get(b"origin")
        host = headers.get(b"host", [])
        guarded = (
            b"host",
            b"origin",
            b"authorization",
            b"mcp-protocol-version",
            b"content-length",
        )
        bad = (
            len(host) != 1
            or host[0].decode("latin1") not in self.config.allowed_hosts
            or any(len(headers.get(k, [])) > 1 for k in guarded)
            or (
                origin is not None
                and origin[0].decode("latin1") not in self.config.allowed_origins
            )
        )
        forwarded = any(
            k == b"forwarded" or k.startswith(b"x-forwarded-") for k in headers
        )
        peer = scope.get("client") or (None, 0)
        if bad or (forwarded and peer[0] != self.config.proxy_peer):
            await self._error(send, 403, "access_denied", origin=None)
            return
        allowed_origin = origin[0] if origin else None
        if scope["method"] == "OPTIONS":
            await self._error(send, 204, None, allowed_origin)
            return
        metadata_path = "/.well-known/oauth-protected-resource" + urlsplit(
            self.config.resource_url
        ).path.rstrip("/")
        if scope["path"] == metadata_path and scope["method"] == "GET":
            body = json.dumps(
                {
                    "resource": self.config.resource_url,
                    "authorization_servers": [self.config.issuer_url],
                    "scopes_supported": ["millrace:operator"],
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")]
                    + self._cors(allowed_origin),
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_INPUT:
                await self._error(send, 413, "request_too_large", allowed_origin)
                return
            if not message.get("more_body", False):
                break
        if scope["path"] == self.path:
            if scope["method"] == "GET":
                await self._error(send, 405, "json_post_required", allowed_origin)
                return
            selected = headers.get(b"mcp-protocol-version", [None])[0]
            if scope["method"] == "POST":
                try:
                    message = json.loads(body)
                    initialize = message.get("method") == "initialize"
                    version = (
                        message.get("params", {}).get("protocolVersion")
                        if initialize
                        else selected
                    )
                except (ValueError, AttributeError):
                    await self._error(send, 400, "invalid_request", allowed_origin)
                    return
                if version not in (PROTOCOL, PROTOCOL.encode()):
                    await self._error(send, 400, "unsupported_protocol", allowed_origin)
                    return
            elif selected != PROTOCOL.encode():
                await self._error(send, 400, "unsupported_protocol", allowed_origin)
                return
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        messages, size = [], 0

        async def bounded_send(message):
            nonlocal size
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not k.lower().startswith(b"access-control-")
                ]
                message["headers"] += self._cors(allowed_origin)
            if message["type"] == "http.response.body":
                size += len(message.get("body", b""))
            if size <= MAX_OUTPUT:
                messages.append(message)

        await self.app(scope, bounded_receive, bounded_send)
        if size > MAX_OUTPUT:
            await self._error(send, 502, "response_too_large", allowed_origin)
        else:
            raw = b"".join(m.get("body", b"") for m in messages)
            try:
                value = json.loads(raw)
                if isinstance(value.get("error"), dict):
                    value["error"].pop("data", None)
                    value["error"]["message"] = "invalid_request"
                    raw = json.dumps(value).encode()
                    start = messages[0]
                    start["headers"] = [
                        (k, v)
                        for k, v in start["headers"]
                        if k.lower() != b"content-length"
                    ]
                    messages = [start, {"type": "http.response.body", "body": raw}]
            except (ValueError, AttributeError):
                pass
            for message in messages:
                await send(message)

    @staticmethod
    def _cors(origin):
        if origin is None:
            return []
        return [
            (b"access-control-allow-origin", origin),
            (b"vary", b"Origin"),
            (b"access-control-allow-methods", b"POST, GET, DELETE, OPTIONS"),
            (
                b"access-control-allow-headers",
                b"Authorization, Content-Type, MCP-Protocol-Version, MCP-Session-Id",
            ),
            (b"access-control-expose-headers", b"MCP-Session-Id, WWW-Authenticate"),
        ]

    async def _error(self, send, status, code, origin):
        body = json.dumps({"error": code}).encode() if code else b""
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")]
                + self._cors(origin),
            }
        )
        await send({"type": "http.response.body", "body": body})
