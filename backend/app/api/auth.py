"""Dashboard authentication endpoints; refresh credentials stay in HttpOnly cookies."""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.config import get_settings
from app.security.auth import AuthError, OwnerAuth

router = APIRouter(prefix="/auth", tags=["authentication"])
COOKIE = "ats_refresh"


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=1024)


class AccessResponse(BaseModel):
    access_token: str
    token_type: str = Field(default="bearer")
    expires_in: int


def _origin(request):
    origin = request.headers.get("origin")
    if origin and origin not in get_settings().cors_origins:
        raise AuthError(403, "Origin not allowed")
    if request.headers.get("X-Requested-With") != "ATS":
        raise AuthError(403, "CSRF header required")


def _tokens(response, tokens):
    settings = get_settings()
    access, refresh = tokens
    response.set_cookie(
        COOKIE,
        refresh,
        httponly=True,
        secure=settings.tls_enabled,
        samesite="strict",
        path="/api/v1/auth",
        max_age=settings.jwt_refresh_hours * 3600,
    )
    response.headers["Cache-Control"] = "no-store"
    return AccessResponse(access_token=access, expires_in=settings.jwt_expiry_minutes * 60)


@router.post("/login", response_model=AccessResponse)
async def login(body: LoginBody, request: Request, response: Response):
    _origin(request)
    return _tokens(
        response, await OwnerAuth().login(body.username, body.password.get_secret_value())
    )


@router.post("/refresh", response_model=AccessResponse)
async def refresh(request: Request, response: Response):
    _origin(request)
    return _tokens(response, await OwnerAuth().refresh(request.cookies.get(COOKIE)))


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    _origin(request)
    await OwnerAuth().logout(request.state.principal)
    response.delete_cookie(COOKIE, path="/api/v1/auth")
    response.headers["Cache-Control"] = "no-store"


@router.get("/me")
async def me(request: Request):
    return {"username": request.state.principal.username}
