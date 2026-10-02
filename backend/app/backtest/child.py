"""Dedicated historical CLI child; never imported by API routes."""

import asyncio
import json
import logging
import sys
from pathlib import Path

from app.backtest.bootstrap import HistoricalManifest, prepare_run
from app.backtest.engine import run_prepared
from app.config import configure_historical_process
from app.core.clock import FakeClock, set_clock
from app.db import session as db_session
from app.marketdata.recordings import read_recording


def unique_keys(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate configuration key")
        values[key] = value
    return values


def read_object(path, limit):
    with Path(path).open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError("configuration size limit exceeded")
    value = json.loads(content, object_pairs_hook=unique_keys)
    if not isinstance(value, dict):
        raise ValueError("configuration object required")
    return value


async def execute(manifest_path, recording_path, settings_path):
    manifest = HistoricalManifest.model_validate(read_object(manifest_path, 4 * 1024 * 1024))
    payload = read_object(settings_path, 65536)
    settings = configure_historical_process(payload)
    clock = FakeClock(manifest.start_at)
    set_clock(clock)
    recording = read_recording(recording_path)
    try:
        db_session.init_engine(settings)
        worker = await prepare_run(
            manifest,
            recording,
            settings=settings,
            clock=clock,
            lock_path=Path.cwd() / "historical-worker.lock",
        )
        status = await run_prepared(worker, manifest)
        return {"status": status, "run_id": manifest.run_id, "simulated": True}
    finally:
        await db_session.dispose_engine()


def main():
    logging.disable(logging.CRITICAL)
    try:
        if len(sys.argv) != 4:
            raise ValueError("manifest, recording and settings paths required")
        result = asyncio.run(execute(*sys.argv[1:]))
        print(json.dumps(result))
        return {"COMPLETED": 0, "INCOMPLETE": 3, "FAILED": 1}[result["status"]]
    except Exception as error:
        print(
            json.dumps({"status": "FAILED", "error_type": type(error).__name__, "simulated": True})
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
