"""Actual API login/session/lockout flows with isolated owner credentials."""

from datetime import timedelta

import httpx
import jwt
import pytest
import sqlalchemy as sa
from argon2 import PasswordHasher

from app.config import get_settings
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.main import create_app
from app.security.auth import AuthError, OwnerAuth


@pytest.fixture
def credentials(settings_env):
    password = "test-only-long-password"
    settings = settings_env(
        DASHBOARD_PASSWORD_HASH=PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(
            password
        ),
        JWT_SECRET="test-only-signing-key-with-at-least-32-bytes",
        JWT_EXPIRY_MINUTES=1,
        LOGIN_MAX_ATTEMPTS=2,
        LOGIN_LOCKOUT_MINUTES=1,
    )
    return settings, password


async def test_actual_login_refresh_expiry_logout_routes(db_engine, credentials, fake_clock):
    _, password = credentials
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"X-Requested-With": "ATS"},
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"username": "owner", "password": password}
        )
        assert login.status_code == 200
        assert (
            "HttpOnly" in login.headers["set-cookie"]
            and "SameSite=strict" in login.headers["set-cookie"]
        )
        access = login.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {access}"
        assert (await client.get("/api/v1/auth/me")).json() == {"username": "owner"}
        config = (await client.get("/api/v1/system/config")).json()
        assert (
            config["dashboard_password_hash"] == "***SET***" and config["jwt_secret"] == "***SET***"
        )
        fake_clock.advance(timedelta(seconds=60))
        assert (await client.get("/api/v1/system/status")).status_code == 401
        refreshed = await client.post("/api/v1/auth/refresh")
        assert refreshed.status_code == 200
        client.headers["Authorization"] = f"Bearer {refreshed.json()['access_token']}"
        assert (await client.post("/api/v1/auth/logout")).status_code == 204
        assert (await client.get("/api/v1/auth/me")).status_code == 401
        assert (await client.post("/api/v1/auth/refresh")).status_code == 401
    async with db_session.session_scope() as session:
        events = set(await session.scalars(sa.select(AuditEvent.event_type)))
    assert {"LOGIN", "REFRESH", "LOGOUT"} <= events


async def test_all_routes_require_authentication(db_engine):
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        routes = app.openapi()["paths"] | {
            "/api/v1/openapi.json": {"get": {}},
            "/api/v1/docs": {"get": {}},
        }
        for path, methods in routes.items():
            if path in ("/api/v1/auth/login", "/api/v1/auth/refresh"):
                continue
            for method in methods:
                response = await client.request(method.upper(), path)
                assert response.status_code == 401, (method, path, response.text)
        assert (await client.post("/api/v1/future-control")).status_code == 401


async def test_lockout_survives_engine_restart(db_engine, credentials, fake_clock):
    _, password = credentials
    auth = OwnerAuth()
    for expected in (401, 429):
        with pytest.raises(AuthError) as caught:
            await auth.login("unknown-owner", "incorrect-password")
        assert caught.value.status == expected
    await db_session.dispose_engine()
    db_session.init_engine()
    with pytest.raises(AuthError) as caught:
        await OwnerAuth().login("owner", password)
    assert caught.value.status == 429
    fake_clock.advance(timedelta(seconds=60))
    access, _ = await OwnerAuth().login("owner", password)
    assert (await OwnerAuth().authenticate(access)).username == "owner"


async def test_refresh_rotation_replay_and_credential_change(
    db_engine, credentials, fake_clock, monkeypatch
):
    _, password = credentials
    auth = OwnerAuth()
    _, old_refresh = await auth.login("owner", password)
    access, current = await auth.refresh(old_refresh)
    with pytest.raises(AuthError):
        await auth.refresh(old_refresh)
    with pytest.raises(AuthError):
        await auth.authenticate(access)
    with pytest.raises(AuthError):
        await auth.refresh(current)
    access, _ = await auth.login("owner", password)
    monkeypatch.setenv("JWT_SECRET", "another-test-only-signing-key-with-32-bytes")
    get_settings.cache_clear()
    with pytest.raises(AuthError):
        await OwnerAuth().authenticate(access)


async def test_tamper_csrf_validation_redaction_and_missing_config(
    db_engine, credentials, fake_clock
):
    settings, password = credentials
    auth = OwnerAuth()
    access, _ = await auth.login("owner", password)
    claims = jwt.decode(access, options={"verify_signature": False})
    forged = jwt.encode(
        claims | {"typ": "refresh"}, settings.jwt_secret.get_secret_value(), algorithm="HS256"
    )
    for token in (forged, access[:-8] + "tampered", "not-a-jwt"):
        with pytest.raises(AuthError):
            await auth.authenticate(token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        body = {"username": "owner", "password": password}
        assert (await client.post("/api/v1/auth/login", json=body)).status_code == 403
        client.headers.update({"X-Requested-With": "ATS", "Origin": "https://evil.invalid"})
        assert (await client.post("/api/v1/auth/login", json=body)).status_code == 403
        del client.headers["Origin"]
        response = await client.post("/api/v1/auth/login", json=body | {"unexpected": password})
        assert response.status_code == 422 and password not in response.text
