"""Transient local browser trust, independent of operational storage."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from threading import RLock

from .credentials import CredentialStore

COOKIE = "millrace_os_session"
NONCE_TTL = 60
SESSION_TTL = 900
MAX_NONCES = 32
MAX_SESSIONS = 16


class TrustError(Exception):
    def __init__(self, code: str = "authentication_required", status: int = 401):
        self.code, self.status = code, status


@dataclass(frozen=True)
class Session:
    csrf: str
    expires: float
    recovery: bool


class LocalTrust:
    def __init__(self, store: CredentialStore, clock=time.monotonic):
        self.store, self.clock = store, clock
        self.token: str | None = None
        self.nonces: dict[str, tuple[float, bool]] = {}
        self.sessions: dict[str, Session] = {}
        self.lock = RLock()

    def reset(self):
        with self.lock:
            self.token = self.store.ensure()
            self.nonces.clear()
            self.sessions.clear()

    def prune(self):
        now = self.clock()
        self.nonces = {k: v for k, v in self.nonces.items() if v[0] > now}
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}

    def bearer(self, authorization: str | None):
        if (
            not self.token
            or not authorization
            or not secrets.compare_digest(
                authorization.encode(), ("Bearer " + self.token).encode()
            )
        ):
            raise TrustError()

    def mint(self, recovery: bool) -> str:
        with self.lock:
            self.prune()
            if len(self.nonces) >= MAX_NONCES:
                raise TrustError("trust_capacity", 429)
            nonce = secrets.token_urlsafe(32)
            self.nonces[nonce] = (self.clock() + NONCE_TTL, recovery)
            return nonce

    def exchange(self, nonce: str, recovery: bool) -> tuple[str, Session]:
        with self.lock:
            self.prune()
            item = self.nonces.get(nonce)
            if item is None or item[1] != recovery:
                raise TrustError("invalid_bootstrap")
            if len(self.sessions) >= MAX_SESSIONS:
                raise TrustError("trust_capacity", 429)
            del self.nonces[nonce]
            sid = secrets.token_urlsafe(32)
            session = Session(
                secrets.token_urlsafe(32), self.clock() + SESSION_TTL, recovery
            )
            self.sessions[sid] = session
            return sid, session

    def session(self, sid: str | None) -> Session:
        with self.lock:
            self.prune()
            session = self.sessions.get(sid or "")
            if session is None:
                raise TrustError()
            return session

    def rotate(self):
        with self.lock:
            # Invalidate before file IO; failed rotation also fails closed.
            self.token = None
            self.nonces.clear()
            self.sessions.clear()
            self.token = self.store.rotate()
