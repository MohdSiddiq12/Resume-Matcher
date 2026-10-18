"""Hosted single-user authentication boundary tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from app import auth
from app.main import app

ORIGIN = "https://localhost"
PASSWORD = "correct horse battery staple"
SIGNING_SECRET = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def production_auth(
    monkeypatch: pytest.MonkeyPatch, isolated_backend_state: object
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("APP_AUTH_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("APP_AUTH_ORIGIN", ORIGIN)
    auth._sessions.clear()
    auth._login_attempts.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/v1/resumes/list",
        "/api/v1/jobs/queue",
        "/api/v1/applications",
        "/api/v1/config/features",
        "/api/v1/enrichment/analyze/example",
        "/api/v1/ats/parse-check",
        "/docs",
        "/redoc",
        "/openapi.json",
    ],
)
async def test_sensitive_route_groups_require_session(path: str) -> None:
    async with _client() as client:
        response = await client.get(path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}


async def test_login_issues_secure_session_and_session_endpoint_accepts_it() -> None:
    async with _client() as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert "httponly" in response.headers["set-cookie"].lower()
        assert "secure" in response.headers["set-cookie"].lower()
        assert "samesite=strict" in response.headers["set-cookie"].lower()
        assert (await client.get("/api/v1/config/features")).status_code == 200


async def test_invalid_login_is_generic_and_does_not_echo_password() -> None:
    async with _client() as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"password": "not-the-password"},
            headers={"Origin": ORIGIN},
        )
    assert response.status_code == 401
    assert "not-the-password" not in response.text
    assert response.json() == {"detail": "Login failed."}


async def test_state_change_requires_csrf_and_logout_revokes_session() -> None:
    async with _client() as client:
        await client.post(
            "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
        )
        csrf = client.cookies.get(auth.CSRF_COOKIE)
        rejected = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
        assert rejected.status_code == 403
        accepted = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf or ""},
        )
        assert accepted.status_code == 200
        assert (await client.get("/api/v1/config/features")).status_code == 401


async def test_expired_session_is_rejected() -> None:
    async with _client() as client:
        await client.post(
            "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
        )
        session_id = client.cookies.get(auth.SESSION_COOKIE, "").split(".", 1)[0]
        auth._sessions[session_id] = (0, auth._sessions[session_id][1])
        assert (await client.get("/api/v1/config/features")).status_code == 401


async def test_tampered_session_is_rejected() -> None:
    async with _client() as client:
        await client.post(
            "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
        )
        token = client.cookies.get(auth.SESSION_COOKIE, "")
        client.cookies.set(auth.SESSION_COOKIE, f"{token}tampered", secure=True)
        assert (await client.get("/api/v1/config/features")).status_code == 401


async def test_login_throttles_repeated_bad_passwords() -> None:
    async with _client() as client:
        for _ in range(auth._MAX_LOGIN_ATTEMPTS):
            response = await client.post(
                "/api/v1/auth/login",
                json={"password": "wrong"},
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 401
        blocked = await client.post(
            "/api/v1/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"]


async def test_health_is_public_and_contains_no_configuration() -> None:
    async with _client() as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


async def test_chatgpt_oauth_callback_remains_public() -> None:
    async with _client() as client:
        response = await client.get("/auth/callback?error=access_denied")
    assert response.status_code == 303


async def test_production_startup_fails_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import lifespan

    monkeypatch.delenv("APP_AUTH_PASSWORD")
    with pytest.raises(RuntimeError, match="APP_AUTH_PASSWORD"):
        async with lifespan(app):
            pytest.fail("Production startup unexpectedly succeeded")


def test_startup_requires_https_auth_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("APP_AUTH_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("APP_AUTH_ORIGIN", "http://localhost")
    with pytest.raises(RuntimeError, match="APP_AUTH_ORIGIN"):
        auth.validate_auth_configuration()


def test_development_auth_bypass_is_forbidden_on_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("RENDER_SERVICE_ID", "render-service")
    with pytest.raises(RuntimeError, match="cannot be used"):
        auth.validate_auth_configuration()
