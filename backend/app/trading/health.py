"""Read-only availability evidence from the active recovered PAPER runtime."""

from datetime import timedelta

import sqlalchemy as sa

from app.audit.service import AuditService
from app.core.clock import UTC, get_clock
from app.core.data_origin import ExecutionRealism
from app.core.enums import HealthStatus, OrderStatus
from app.db import session as db_session
from app.db.models.system import Heartbeat
from app.db.models.trading import Order, Position
from app.execution.protection import protection_status
from app.modes import TradingMode
from app.monitoring.healthchecks import HealthCheck
from app.trading.worker import active_worker


class PaperMarketDataCheck(HealthCheck):
    name = "market_data"
    critical = True

    async def run(self):
        worker = active_worker()
        runtime = worker.reference_runtime if worker else None
        if runtime is None:
            return HealthStatus.SKIPPED, "Selected PAPER data provider unavailable", {}
        if runtime.quote_refresh_error:
            return HealthStatus.FAIL, "Selected PAPER quote refresh failed", {}
        observations = dict(runtime.latest_quotes)
        if not observations:
            return HealthStatus.SKIPPED, "No refreshed enabled-universe quotes", {}
        quotes = {}
        for identifier, observation in observations.items():
            runtime.ingestion.validate_quote(observation.quote, observation.quote.instrument)
            if not await AuditService(worker.clock).verify(observation.chain_id, expected_count=1):
                return HealthStatus.FAIL, "Quote observation audit unavailable", {}
            quotes[identifier] = {
                "observed_at": observation.quote.observed_at.isoformat(),
                "ltp": str(observation.quote.ltp),
                "data_origin": observation.quote.data_origin.value,
                "source_id": observation.chain_id,
            }
        return (
            HealthStatus.PASS,
            "Selected PAPER inputs fresh; entry gates remain authoritative",
            {
                "provider": runtime.provider.name,
                "quotes": quotes,
            },
        )


class PaperMarketTransportCheck(HealthCheck):
    name = "market_transport"
    critical = False

    async def run(self):
        worker = active_worker()
        runtime = worker.reference_runtime if worker else None
        if runtime is None:
            return HealthStatus.SKIPPED, "Selected PAPER data transport unavailable", {}
        status = getattr(runtime.provider, "status", HealthStatus.SKIPPED)
        if not isinstance(status, HealthStatus):
            status = HealthStatus.FAIL
        detail = (
            "REST fallback; refreshed quote evidence checked separately"
            if status == HealthStatus.DEGRADED
            else "Provider transport state; no live-execution verification"
        )
        return status, detail, {"provider": runtime.provider.name}


class PaperRuntimeCheck(HealthCheck):
    name = "order_service"
    timeout_seconds = 5.0

    def __init__(self, settings, *, clock=None):
        self.settings = settings
        self.clock = clock or get_clock()
        self.critical = settings.trading_mode != TradingMode.PAPER or settings.paper_worker_enabled

    async def run(self):
        if self.settings.trading_mode != TradingMode.PAPER:
            return HealthStatus.FAIL, "Real-broker order-service health is not implemented", {}
        if not self.settings.paper_worker_enabled:
            return HealthStatus.SKIPPED, "PAPER worker explicitly disabled", {}
        worker = active_worker()
        if worker is None or not worker.executor.ready:
            return HealthStatus.FAIL, "PAPER worker absent, stopped or unrecovered", {}
        if worker.executor.broker.execution_realism is not ExecutionRealism.SIMULATED:
            return HealthStatus.FAIL, "PAPER runtime broker identity mismatch", {}
        return await self._inspect(worker)

    async def _inspect(self, worker):
        now = self.clock.utcnow()
        async with db_session.session_scope() as session:
            heartbeat = await session.get(Heartbeat, "paper-worker")
            if heartbeat is None or worker.last_cycle is None:
                return HealthStatus.FAIL, "PAPER cycle heartbeat unavailable", {}
            beat = heartbeat.beat_at
            beat = beat.replace(tzinfo=UTC) if beat.tzinfo is None else beat
            freshness = timedelta(seconds=self.settings.paper_cycle_seconds * 3)
            if not timedelta(0) <= now - beat <= freshness or beat != worker.last_cycle:
                return HealthStatus.FAIL, "PAPER cycle heartbeat stale or inconsistent", {}
            unknown = await session.scalar(
                sa.select(sa.func.count())
                .select_from(Order)
                .where(
                    Order.mode == TradingMode.PAPER,
                    Order.status == OrderStatus.UNKNOWN,
                )
            )
            positions = list(
                (
                    await session.scalars(
                        sa.select(Position).where(
                            Position.mode == TradingMode.PAPER,
                            Position.net_quantity != 0,
                        )
                    )
                ).all()
            )
            protection = {
                position.id: await protection_status(session, position, now)
                for position in positions
            }
        context = {
            "mode": "PAPER",
            "execution_realism": "SIMULATED",
            "heartbeat_at": beat.isoformat(),
            "unknown_orders": unknown,
            "protection": protection,
            "worker_review_required": worker.failed,
            "cash": str(worker.executor.broker.account.cash),
            "available_margin": str(worker.executor.broker.account.available_margin),
            "open_positions": len(positions),
        }
        if unknown or any(status != "RECENT_SOFTWARE_CHECK" for status in protection.values()):
            return (
                HealthStatus.FAIL,
                "PAPER unknown order or protection evidence unavailable",
                context,
            )
        return (
            HealthStatus.PASS,
            "PAPER runtime available; risk and owner-review gates remain authoritative",
            context,
        )
