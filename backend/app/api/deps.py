"""Deny-by-default HTTP authentication including schema/docs and unknown routes."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.security.auth import AuthError, OwnerAuth

PUBLIC = frozenset({("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/refresh")})


async def authenticate_request(request):
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise AuthError()
    return await OwnerAuth().authenticate(authorization[7:])


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if (request.method, request.url.path) not in PUBLIC:
            try:
                request.state.principal = await authenticate_request(request)
            except AuthError as error:
                return JSONResponse(
                    {"detail": error.message},
                    status_code=error.status,
                    headers={"Cache-Control": "no-store"},
                )
            except Exception:
                return JSONResponse({"detail": "Authentication unavailable"}, status_code=503)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
