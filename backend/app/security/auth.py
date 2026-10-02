"""Argon2id login, bounded attempts and database-backed JWT sessions."""

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass

import jwt
import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from app.audit.service import AuditIdentity, AuditService
from app.config import get_settings
from app.core.clock import get_clock
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.security import DashboardSession, LoginGuard


class AuthError(Exception):
    def __init__(self, status=401, message="Authentication required"):
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Principal:
    username: str
    session_id: str


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerAuth:
    def __init__(self, settings=None, clock=None):
        self.settings = settings or get_settings()
        self.clock = clock or get_clock()

    def _credentials(self):
        settings = self.settings
        key = settings.jwt_secret.get_secret_value() if settings.jwt_secret else ""
        password_hash = settings.dashboard_password_hash or ""
        if len(key.encode()) < 32 or not password_hash.startswith("$argon2id$"):
            raise AuthError(503, "Owner authentication is not configured")
        return key, password_hash, digest(key + password_hash)

    async def _audit(self, session, event, actor, result):
        await AuditService(self.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=new_id("auth"),
                event_type=event,
                actor=actor,
                mode=self.settings.trading_mode,
            ),
            {"result": result},
        )

    def _access(self, row):
        key, _, _ = self._credentials()
        now = int(self.clock.now().timestamp())
        return jwt.encode(
            {
                "sub": row.username,
                "sid": row.id,
                "typ": "access",
                "iss": "ats",
                "aud": "ats-dashboard",
                "iat": now,
                "exp": min(now + self.settings.jwt_expiry_minutes * 60, int(row.expires_at)),
            },
            key,
            algorithm="HS256",
        )

    async def login(self, username, password):
        _, password_hash, fingerprint = self._credentials()
        now = int(self.clock.now().timestamp())
        error = None
        tokens = None
        async with db_session.session_scope() as session:
            guard = await session.get(LoginGuard, "owner", with_for_update=True)
            if guard is None:
                guard = LoginGuard(
                    id="owner", failures=0, window_start=now, locked_until=0, version=0
                )
                session.add(guard)
                await session.flush()
            if guard.locked_until > now:
                raise AuthError(429, "Login temporarily locked")
            if now - guard.window_start >= self.settings.login_lockout_minutes * 60:
                failures = 0
                window_start = now
            else:
                failures, window_start = guard.failures, guard.window_start
            try:
                verified = await asyncio.to_thread(PasswordHasher().verify, password_hash, password)
            except VerificationError:
                verified = False
            valid = verified and hmac.compare_digest(
                username.encode(), self.settings.dashboard_username.encode()
            )
            failures = 0 if valid else failures + 1
            locked_until = (
                now + self.settings.login_lockout_minutes * 60
                if failures >= self.settings.login_max_attempts
                else 0
            )
            changed = await session.execute(
                sa.update(LoginGuard)
                .where(LoginGuard.id == guard.id, LoginGuard.version == guard.version)
                .values(
                    failures=failures,
                    window_start=window_start,
                    locked_until=locked_until,
                    version=guard.version + 1,
                )
            )
            if changed.rowcount != 1:
                raise AuthError(503, "Concurrent authentication update; retry")
            if not valid:
                await self._audit(
                    session, "LOGIN_DENIED", "unauthenticated", {"locked": bool(locked_until)}
                )
                error = AuthError(
                    429 if locked_until else 401, "Invalid credentials or account locked"
                )
            else:
                identifier, secret = new_id("ses"), secrets.token_urlsafe(48)
                row = DashboardSession(
                    id=identifier,
                    username=self.settings.dashboard_username,
                    refresh_hash=digest(secret),
                    previous_refresh_hash=None,
                    credential_fingerprint=fingerprint,
                    expires_at=now + self.settings.jwt_refresh_hours * 3600,
                    revoked=False,
                    version=0,
                )
                session.add(row)
                await self._audit(session, "LOGIN", row.username, {"session_id": row.id})
                tokens = self._access(row), f"{identifier}.{secret}"
        if error:
            raise error
        return tokens

    async def authenticate(self, token):
        key, _, fingerprint = self._credentials()
        try:
            if not token or len(token) > 4096:
                raise ValueError("invalid token")
            claims = jwt.decode(
                token,
                key,
                algorithms=["HS256"],
                issuer="ats",
                audience="ats-dashboard",
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "require": ["sub", "sid", "typ", "exp", "iat", "iss", "aud"],
                },
            )
            now = self.clock.now().timestamp()
            if (
                claims["typ"] != "access"
                or claims["sub"] != self.settings.dashboard_username
                or type(claims["iat"]) is not int
                or type(claims["exp"]) is not int
                or not claims["iat"] <= now < claims["exp"]
            ):
                raise ValueError("invalid claims")
            async with db_session.session_scope() as session:
                row = await session.get(DashboardSession, claims["sid"])
                if (
                    row is None
                    or row.revoked
                    or row.expires_at <= now
                    or row.username != claims["sub"]
                    or row.credential_fingerprint != fingerprint
                ):
                    raise ValueError("invalid session")
            return Principal(row.username, row.id)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthError() from None

    async def refresh(self, token):
        _, _, fingerprint = self._credentials()
        if not token or len(token) > 256 or "." not in token:
            raise AuthError()
        identifier, secret = token.split(".", 1)
        tokens = None
        async with db_session.session_scope() as session:
            row = await session.get(DashboardSession, identifier, with_for_update=True)
            if (
                row is None
                or row.revoked
                or row.expires_at <= self.clock.now().timestamp()
                or row.credential_fingerprint != fingerprint
            ):
                raise AuthError()
            hashed = digest(secret)
            if not hmac.compare_digest(row.refresh_hash, hashed):
                if row.previous_refresh_hash and hmac.compare_digest(
                    row.previous_refresh_hash, hashed
                ):
                    row.revoked = True
                    await self._audit(
                        session, "REFRESH_REPLAY", row.username, {"session_id": row.id}
                    )
            else:
                new_secret = secrets.token_urlsafe(48)
                changed = await session.execute(
                    sa.update(DashboardSession)
                    .where(
                        DashboardSession.id == row.id,
                        DashboardSession.version == row.version,
                        DashboardSession.revoked.is_(False),
                        DashboardSession.refresh_hash == hashed,
                    )
                    .values(
                        previous_refresh_hash=hashed,
                        refresh_hash=digest(new_secret),
                        version=row.version + 1,
                    )
                )
                if changed.rowcount != 1:
                    raise AuthError()
                await self._audit(session, "REFRESH", row.username, {"session_id": row.id})
                tokens = self._access(row), f"{identifier}.{new_secret}"
        if tokens is None:
            raise AuthError()
        return tokens

    async def logout(self, principal):
        async with db_session.session_scope() as session:
            row = await session.get(DashboardSession, principal.session_id, with_for_update=True)
            if row is None or row.username != principal.username:
                raise AuthError()
            row.revoked = True
            row.version += 1
            await self._audit(session, "LOGOUT", row.username, {"session_id": row.id})
