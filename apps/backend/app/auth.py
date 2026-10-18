"""Single-user browser authentication for hosted deployments."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from contextvars import ContextVar
from collections import deque
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SESSION_COOKIE = "rm_session"
CSRF_COOKIE = "rm_csrf"
SESSION_TTL_SECONDS = 12 * 60 * 60
_RATE_WINDOW_SECONDS = 15 * 60
_MAX_LOGIN_ATTEMPTS = 5
_LOCK = threading.Lock()
_sessions: dict[str, tuple[int, str]] = {}
_login_attempts: dict[str, deque[float]] = {}
_password_cache: tuple[bytes, bytes, bytes] | None = None
_session_cookie_context: ContextVar[str | None] = ContextVar(
    "auth_session_cookie", default=None
)

_PUBLIC_PATHS = {
    "/api/v1/health",
    "/api/v1/auth/login",
    "/api/v1/auth/session",
    "/auth/callback",
}


def app_environment() -> str:
    return os.getenv("APP_ENV", "production").strip().lower()


def validate_auth_configuration() -> None:
    """Fail startup unless production credentials are explicitly configured."""
    if app_environment() == "development":
        if os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"):
            raise RuntimeError("APP_ENV=development cannot be used on a hosted Render service.")
        return
    password = os.getenv("APP_AUTH_PASSWORD", "")
    signing_secret = os.getenv("APP_AUTH_SECRET", "")
    if len(password) < 12:
        raise RuntimeError("APP_AUTH_PASSWORD must contain at least 12 characters.")
    if len(signing_secret) < 32:
        raise RuntimeError("APP_AUTH_SECRET must contain at least 32 characters.")
    origin = urlsplit(os.getenv("APP_AUTH_ORIGIN", ""))
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise RuntimeError("APP_AUTH_ORIGIN must be the HTTPS application origin.")


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signature(value: str) -> str:
    secret = os.getenv("APP_AUTH_SECRET", "").encode("utf-8")
    return _encoded(hmac.new(secret, value.encode("ascii"), hashlib.sha256).digest())


def _password_digest(password: str) -> bool:
    global _password_cache
    configured = os.getenv("APP_AUTH_PASSWORD", "")
    signing_secret = os.getenv("APP_AUTH_SECRET", "")
    cache_key = hashlib.sha256((configured + signing_secret).encode("utf-8")).digest()
    salt = hashlib.sha256(signing_secret.encode("utf-8")).digest()[:16]
    with _LOCK:
        if _password_cache is None or _password_cache[0] != cache_key:
            _password_cache = (
                cache_key,
                salt,
                hashlib.scrypt(
                    configured.encode("utf-8"), salt=salt, n=2**14, r=8, p=1
                ),
            )
        expected = _password_cache[2]
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1
        )
    return hmac.compare_digest(candidate, expected)


def _prune(now: float) -> None:
    for session_id, (expiry, _) in list(_sessions.items()):
        if expiry <= now:
            _sessions.pop(session_id, None)
    for address, attempts in list(_login_attempts.items()):
        while attempts and attempts[0] <= now - _RATE_WINDOW_SECONDS:
            attempts.popleft()
        if not attempts:
            _login_attempts.pop(address, None)


def _trusted_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    configured = os.getenv("APP_AUTH_ORIGIN", "").rstrip("/")
    if not configured and app_environment() == "development":
        configured = "http://localhost:3000"
    return bool(configured) and hmac.compare_digest(origin.rstrip("/"), configured)


def login(request: Request, password: str) -> JSONResponse:
    """Validate the one configured password and issue a signed browser session."""
    if not _trusted_origin(request):
        return JSONResponse({"detail": "Login failed."}, status_code=403)

    address = request.client.host if request.client else "unknown"
    now = time.time()
    with _LOCK:
        _prune(now)
        attempts = _login_attempts.setdefault(address, deque())
        if len(attempts) >= _MAX_LOGIN_ATTEMPTS:
            return JSONResponse(
                {"detail": "Too many login attempts. Try again later."},
                status_code=429,
                headers={"Retry-After": str(_RATE_WINDOW_SECONDS)},
            )
        attempts.append(now)

    configured = os.getenv("APP_AUTH_PASSWORD", "")
    if not configured:
        return JSONResponse({"detail": "Login is not configured."}, status_code=503)
    if not _password_digest(password):
        return JSONResponse({"detail": "Login failed."}, status_code=401)

    expiry = int(now) + SESSION_TTL_SECONDS
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    signed = f"{session_id}.{expiry}"
    session_cookie = f"{signed}.{_signature(signed)}"
    with _LOCK:
        _sessions[session_id] = (expiry, csrf_token)

    response = JSONResponse({"authenticated": True, "csrf_token": csrf_token})
    secure = app_environment() != "development"
    response.set_cookie(
        SESSION_COOKIE,
        session_cookie,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )
    return response


def authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        session_id, expiry_text, signature = token.split(".", 2)
        expiry = int(expiry_text)
    except (ValueError, TypeError):
        return False
    signed = f"{session_id}.{expiry}"
    if expiry <= time.time() or not hmac.compare_digest(signature, _signature(signed)):
        return False
    with _LOCK:
        _prune(time.time())
        return session_id in _sessions


def csrf_valid(request: Request) -> bool:
    if not _trusted_origin(request):
        return False
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        return False
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        session_id = token.split(".", 1)[0]
    except ValueError:
        return False
    with _LOCK:
        session = _sessions.get(session_id)
        return bool(session and hmac.compare_digest(session[1], header))


def current_session_cookie() -> str | None:
    """Return the authenticated request cookie for same-request PDF rendering."""
    return _session_cookie_context.get()


def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE, "")
    session_id = token.split(".", 1)[0] if token else ""
    with _LOCK:
        _sessions.pop(session_id, None)
    response = JSONResponse({"authenticated": False})
    secure = app_environment() != "development"
    for name, httponly in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.set_cookie(
            name, "", max_age=0, expires=0, httponly=httponly,
            secure=secure, samesite="strict", path="/"
        )
    return response


def issue_login(request: Request, password: str) -> JSONResponse:
    if app_environment() == "development" and not os.getenv("APP_AUTH_PASSWORD"):
        return JSONResponse({"detail": "Login is not configured."}, status_code=503)
    return login(request, password)


class AuthenticationMiddleware:
    """Protect every FastAPI route except explicit protocol entry points."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if app_environment() == "development":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if path in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if not authenticated(request):
            response = JSONResponse(
                {"detail": "Authentication required."}, status_code=401
            )
        elif method not in {"GET", "HEAD", "OPTIONS"} and not csrf_valid(request):
            response = JSONResponse(
                {"detail": "CSRF validation failed."}, status_code=403
            )
        else:
            cookie = request.cookies.get(SESSION_COOKIE, "")
            context_token = _session_cookie_context.set(cookie)
            try:
                await self.app(scope, receive, send)
            finally:
                _session_cookie_context.reset(context_token)
            return
        await response(scope, receive, send)

