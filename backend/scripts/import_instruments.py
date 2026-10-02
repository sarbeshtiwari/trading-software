"""Owner-run public instrument import; never enables a trading worker."""

import argparse
import asyncio
import json

from app.config import get_settings
from app.core.logging import register_secrets
from app.db import session as db_session
from app.instruments.loader import InstrumentLoader


async def run(quarantine_duplicates=False):
    settings = get_settings()
    register_secrets(settings.secret_values)
    if settings.paper_worker_enabled:
        raise RuntimeError("Disable the configured worker before manual instrument maintenance")
    db_session.init_engine(settings)
    try:
        result = await InstrumentLoader(settings).load(quarantine_duplicates=quarantine_duplicates)
        return result.to_dict()
    finally:
        await db_session.dispose_engine()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quarantine-duplicates", action="store_true")
    arguments = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(arguments.quarantine_duplicates))))
    except Exception as error:
        print(json.dumps({"status": "IMPORT_FAILED", "error_type": type(error).__name__}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
