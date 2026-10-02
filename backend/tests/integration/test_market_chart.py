"""Real candle ingestion reaches the API without future/unknown-origin chart values."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.db import session as db_session
from app.db.models.market_data import Candle
from app.marketdata.ingest import CandleStore
from app.marketdata.models import Bar
from tests.integration.test_paper_execution import credentials, setup_execution

__all__ = ["credentials"]


async def chart_data(credentials, fake_clock):
    _engine, _proposal, _market, client, context = await setup_execution(credentials, fake_clock)
    start = fake_clock.now() - timedelta(minutes=20)
    bars = [
        Bar(
            ts=start + timedelta(minutes=index),
            open=Decimal(100 + index),
            high=Decimal(101 + index),
            low=Decimal(99 + index),
            close=Decimal(100 + index),
            volume=1000,
        )
        for index in range(22)
    ]
    await CandleStore().write(context.market.instrument_id, 1, bars, data_origin="SYNTHETIC")
    return client, context.market.instrument_id


async def test_closed_source_filtered_candles_and_backend_sma(db_engine, credentials, fake_clock):
    client, instrument = await chart_data(credentials, fake_clock)
    try:
        result = await client.get(
            f"/api/v1/market/candles/{instrument}", params={"origin": "SYNTHETIC", "interval": 1}
        )
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "RECORDED"
        assert len(body["bars"]) == 20
        assert Decimal(body["bars"][-1]["sma20"]) == Decimal("109.5")
        assert all(bar["sma20"] is None for bar in body["bars"][:-1])
        assert body["bars"][-1]["closed_at"] == fake_clock.now().isoformat()
        wrong_origin = await client.get(
            f"/api/v1/market/candles/{instrument}", params={"origin": "LIVE"}
        )
        assert wrong_origin.json()["bars"] == []
        assert wrong_origin.json()["status"] == "UNAVAILABLE"
        before_ingestion = await client.get(
            f"/api/v1/market/candles/{instrument}",
            params={
                "origin": "SYNTHETIC",
                "as_of": (fake_clock.now() - timedelta(seconds=1)).isoformat(),
            },
        )
        assert before_ingestion.json()["bars"] == []
        future = await client.get(
            f"/api/v1/market/candles/{instrument}",
            params={
                "origin": "SYNTHETIC",
                "as_of": (fake_clock.now() + timedelta(seconds=1)).isoformat(),
            },
        )
        assert future.status_code == 422
        assert (
            await client.get(
                f"/api/v1/market/candles/{instrument}",
                params={"origin": "SYNTHETIC", "interval": 1440},
            )
        ).status_code == 422
    finally:
        await client.aclose()


async def test_unknown_ingestion_invalid_stored_bar_and_auth(db_engine, credentials, fake_clock):
    client, instrument = await chart_data(credentials, fake_clock)
    try:
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(Candle).where(Candle.instrument_id == instrument).order_by(Candle.ts)
            )
            row.ingested_at = None
        result = await client.get(
            f"/api/v1/market/candles/{instrument}", params={"origin": "SYNTHETIC"}
        )
        assert len(result.json()["bars"]) == 19
        assert all(row["sma20"] is None for row in result.json()["bars"])
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(Candle)
                .where(Candle.instrument_id == instrument, Candle.ingested_at.is_not(None))
                .order_by(Candle.ts)
            )
            row.close = Decimal(999)
        invalid = await client.get(
            f"/api/v1/market/candles/{instrument}", params={"origin": "SYNTHETIC"}
        )
        assert invalid.json()["status"] == "INVALID_STORED_DATA"
        assert invalid.json()["bars"] == []
        listed = await client.get("/api/v1/market/instruments", params={"query": "TEST"})
        assert any(row["id"] == instrument for row in listed.json()["instruments"])
        client.headers.pop("Authorization")
        assert (await client.get("/api/v1/market/instruments")).status_code == 401
    finally:
        await client.aclose()
