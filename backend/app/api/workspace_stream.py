"""Authenticated refresh hints from committed events; HTTP remains authoritative."""

import asyncio
import json
from collections import deque

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from redis.asyncio import Redis

from app.api.market_stream import guarded_stream
from app.config import get_settings
from app.execution.event_types import PAPER_EVENT_TYPES
from app.security.auth import OwnerAuth

router = APIRouter(prefix="/workspace", tags=["workspace"])
STREAMS = tuple(f"ats:events:{kind.value}" for kind in dict.fromkeys(PAPER_EVENT_TYPES.values()))


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=1, max_length=4096)


async def _serve(socket: WebSocket):
    text = await asyncio.wait_for(socket.receive_text(), timeout=5)
    if len(text) > 8192:
        raise ValueError("subscription too large")
    subscription = Subscription.model_validate_json(text)
    auth = OwnerAuth()

    async def authenticate():
        await asyncio.wait_for(auth.authenticate(subscription.token.get_secret_value()), timeout=5)

    async def send(kind):
        await authenticate()
        await asyncio.wait_for(socket.send_json({"type": kind}), timeout=5)

    await authenticate()
    client = Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=5, socket_timeout=3
    )
    try:
        cursors = {}
        for stream in STREAMS:
            tail = await asyncio.wait_for(client.xrevrange(stream, count=1), timeout=6)
            cursors[stream] = tail[0][0] if tail else "0-0"
        await send("WORKSPACE_REFRESH")
        seen: set[str] = set()
        history: deque[str] = deque()
        while True:
            await authenticate()
            rows = await asyncio.wait_for(client.xread(cursors, count=100, block=1000), timeout=6)
            changed = False
            for stream, entries in rows:
                key = stream.decode() if isinstance(stream, bytes) else stream
                for identifier, fields in entries:
                    cursors[key] = identifier
                    raw = fields.get(b"data", fields.get("data"))
                    if raw is None or len(raw) > 65536:
                        raise ValueError("invalid event envelope")
                    event = json.loads(raw)
                    identity = event.get("id")
                    if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
                        raise ValueError("invalid event identity")
                    if identity in seen:
                        continue
                    seen.add(identity)
                    history.append(identity)
                    if len(history) > 1024:
                        seen.remove(history.popleft())
                    changed = True
            await send("WORKSPACE_REFRESH" if changed else "WORKSPACE_HEARTBEAT")
    finally:
        await client.aclose()


@router.websocket("/stream")
async def stream(socket: WebSocket):
    await guarded_stream(socket, _serve)
