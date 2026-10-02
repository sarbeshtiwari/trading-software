"""Owner-operated local publication CLI; destination comes only from application settings."""

import argparse
import asyncio
import json
import logging

from sqlalchemy.ext.asyncio import create_async_engine

from app.backtest.child import read_object
from app.backtest.publication import publish_run
from app.config import HISTORICAL_EXECUTION_SETTINGS, get_settings
from app.core.clock import get_clock
from app.db import session as db_session
from app.modes import TradingMode


async def execute(source_settings, run_id, recording_sha256):
    configuration = read_object(source_settings, 65536)
    allowed = {"database_url", "starting_capital", *HISTORICAL_EXECUTION_SETTINGS}
    if not isinstance(configuration.get("database_url"), str) or set(configuration) - allowed:
        raise ValueError("explicit historical source settings required")
    settings = get_settings()
    if settings.trading_mode != TradingMode.PAPER:
        raise ValueError("historical publication requires PAPER mode")
    source = create_async_engine(configuration["database_url"])
    try:
        db_session.init_engine(settings)
        return await publish_run(
            source, run_id, recording_sha256, actor="owner-local-cli", clock=get_clock()
        )
    finally:
        await source.dispose()
        await db_session.dispose_engine()


def main():
    parser = argparse.ArgumentParser(
        description="Publish finalized historical reports without account state"
    )
    parser.add_argument("source_settings")
    parser.add_argument("run_id")
    parser.add_argument("recording_sha256")
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        status = asyncio.run(
            execute(arguments.source_settings, arguments.run_id, arguments.recording_sha256)
        )
        print(json.dumps({"status": status, "run_id": arguments.run_id, "simulated": True}))
        return 0
    except Exception as error:
        print(
            json.dumps({"status": "FAILED", "error_type": type(error).__name__, "simulated": True})
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
