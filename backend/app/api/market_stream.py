"""Authenticated, bounded observation streaming; no broker or order authority."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.api.market import quote
from app.config import get_settings
from app.core.data_origin import DataOrigin
from app.security.auth import AuthError, OwnerAuth

router = APIRouter(prefix="/market", tags=["market"])
_connections: set[WebSocket] = set()


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=1, max_length=4096)
    instruments: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        min_length=1, max_length=10
    )
    origin: DataOrigin


async def _serve(socket: WebSocket):
    text = await asyncio.wait_for(socket.receive_text(), timeout=5)
    if len(text) > 8192:
        await socket.close(code=1008)
        return
    subscription = Subscription.model_validate_json(text)
    identifiers = tuple(dict.fromkeys(subscription.instruments))
    auth = OwnerAuth()
    while True:
        await asyncio.wait_for(auth.authenticate(subscription.token.get_secret_value()), timeout=5)
        observations = [
            await asyncio.wait_for(quote(identifier, subscription.origin), timeout=5)
            for identifier in identifiers
        ]
        await asyncio.wait_for(auth.authenticate(subscription.token.get_secret_value()), timeout=5)
        await asyncio.wait_for(
            socket.send_json(
                {
                    "type": "QUOTE_SNAPSHOT",
                    "quotes": [observation.model_dump(mode="json") for observation in observations],
                }
            ),
            timeout=5,
        )
        try:
            await asyncio.wait_for(socket.receive_text(), timeout=2)
        except asyncio.TimeoutError:
            continue
        await socket.close(code=1008)
        return


@router.websocket("/stream")
async def stream(socket: WebSocket):
    await guarded_stream(socket, _serve)


async def guarded_stream(socket: WebSocket, handler):
    if socket.headers.get("origin") not in get_settings().cors_origins or socket.query_params:
        await socket.close(code=1008)
        return
    if len(_connections) >= 20:
        await socket.close(code=1013)
        return
    _connections.add(socket)
    try:
        await socket.accept()
        await handler(socket)
    except WebSocketDisconnect:
        pass
    except AuthError:
        await socket.close(code=4401)
    except (ValidationError, ValueError, KeyError):
        await socket.close(code=1008)
    except Exception:
        await socket.close(code=1011)
    finally:
        _connections.discard(socket)
