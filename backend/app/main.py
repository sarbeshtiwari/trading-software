"""FastAPI application entrypoint — BE-001, ARCH-002, ARCH-015, DEPLOY-005.

Startup order is deliberate and must not be rearranged casually:

1. Configure logging **first**, registering configured secrets with the redactor,
   so nothing that follows can log a credential.
2. Resolve the trading mode once, immutably.
3. Print the effective configuration (redacted) — the single most useful line in
   an incident is what the system thought it was configured to do.
4. Open the database.
5. Run startup health checks, which set the trading gate. The gate starts
   fail-closed, so a crash between steps 4 and 5 leaves trading disabled.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api import auth as auth_api
from app.api import audit as audit_api
from app.api import backtests as backtests_api
from app.api import emergency as emergency_api
from app.api import event_failures as event_failures_api
from app.api import fundamentals as fundamentals_api
from app.api import health as health_api
from app.api import historical_jobs as historical_jobs_api
from app.api import journal as journal_api
from app.api import market as market_api
from app.api import market_stream as market_stream_api
from app.api import workspace_stream as workspace_stream_api
from app.api import reconciliation as reconciliation_api
from app.api import news as news_api
from app.api import orders as orders_api
from app.api import reports as reports_api
from app.api import risk as risk_api
from app.api import strategies as strategies_api
from app.api import system as system_api
from app.api import readiness as readiness_api
from app.api import workspace as workspace_api
from app.api.deps import AuthenticationMiddleware
from app.backtest.jobs import HistoricalJobs
from app.backtest.walkforward import WalkForwardJobs
from app.brokers.registry import register_all
from app.config import Settings, get_settings
from app.core.data_origin import DataOrigin
from app.core.errors import ATSError
from app.core.ids import correlation_id as new_correlation_id
from app.core.lifecycle import get_lifecycle
from app.core.logging import configure_logging, get_logger, set_correlation_id
from app.db import session as db_session
from app.emergency.controls import EmergencyControls
from app.modes import TradingMode, resolve_mode
from app.monitoring.gate import get_trading_gate
from app.monitoring.event_delivery import EventFailureMonitor
from app.monitoring.runtime_events import RuntimeEventPublisher
from app.monitoring.healthchecks import get_health_registry
from app.monitoring.startup import run_startup_checks
from app.monitoring.watchdog import HealthWatchdog
from app.news.runtime import NewsAcquisitionCheck, NewsRuntime
from app.instruments.runtime import InstrumentRuntime, InstrumentRefreshCheck
from app.notifications.runtime import start_notifications, stop_notifications
from app.risk.safety import RiskSafety
from app.security.auth import AuthError
from app.trading.health import PaperMarketDataCheck, PaperMarketTransportCheck, PaperRuntimeCheck
from app.trading.worker import start_worker, stop_worker

logger = get_logger("main")

API_PREFIX = "/api/v1"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id to every request and echo it back — BE-010."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get("X-Request-Id")
        correlation = incoming or new_correlation_id()
        set_correlation_id(correlation)
        request.state.correlation_id = correlation
        try:
            response: Response = await call_next(request)
        finally:
            set_correlation_id(None)
        response.headers["X-Request-Id"] = correlation
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()

    configure_logging(
        level=settings.log_level,
        log_format=settings.log_format,
        log_dir=settings.log_dir,
        secrets=settings.secret_values,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        force=True,
    )

    mode = resolve_mode(settings.trading_mode)
    system_api.mark_started()

    logger.info(
        "Starting %s v%s",
        settings.app_name,
        settings.version,
        extra={"effective_config": settings.redacted_summary()},
    )
    logger.info(
        "TRADING MODE: %s",
        mode.value,
        extra={"trading_mode": mode.value, "broker_provider": settings.broker_provider.value},
    )

    # Providers register themselves with the factories here, explicitly, so the
    # selected broker is a consequence of configuration rather than of which
    # module happened to be imported first.
    register_all()

    lifecycle = get_lifecycle()
    db_session.init_engine(settings)
    lifecycle.on_shutdown(db_session.dispose_engine, name="database", timeout_seconds=10)
    app.state.notifications = await start_notifications(settings)
    lifecycle.on_shutdown(stop_notifications, name="notifications", timeout_seconds=10)

    try:
        await EmergencyControls().restore()
        for origin in DataOrigin:
            await RiskSafety().restore(mode, origin)
    except Exception:
        logger.error("Risk safety restoration failed; entry gate remains closed")

    try:
        result = await run_startup_checks(settings)
        if not result.trading_allowed:
            logger.error(
                "TRADING DISABLED at startup",
                extra={"reason": result.blocking_reason},
            )
    except Exception as exc:
        get_trading_gate().block("startup", f"startup checks failed: {exc}", blocks_exits=True)
        logger.exception("Startup health checks could not run")

    get_health_registry().register(PaperRuntimeCheck(settings))
    get_health_registry().register(InstrumentRefreshCheck(app.state.instrument_runtime))
    await app.state.instrument_runtime.start()
    lifecycle.on_shutdown(app.state.instrument_runtime.stop, name="instrument-refresh", timeout_seconds=15)
    get_health_registry().register(NewsAcquisitionCheck(app.state.news_runtime))
    await app.state.news_runtime.start()
    lifecycle.on_shutdown(app.state.news_runtime.stop, name="news-acquisition", timeout_seconds=15)
    if settings.trading_mode == TradingMode.PAPER:
        get_health_registry().register(PaperMarketDataCheck())
        get_health_registry().register(PaperMarketTransportCheck())
    watchdog = HealthWatchdog(settings.healthcheck_interval_seconds)
    app.state.health_watchdog = watchdog
    await watchdog.start()
    lifecycle.on_shutdown(watchdog.stop, name="health-watchdog", timeout_seconds=15)
    await start_worker(settings)
    lifecycle.on_shutdown(stop_worker, name="paper-worker", timeout_seconds=15)
    await app.state.event_failure_monitor.start()
    await app.state.runtime_event_publisher.start()
    lifecycle.on_shutdown(app.state.runtime_event_publisher.stop, name="runtime-event-publisher", timeout_seconds=15)
    lifecycle.on_shutdown(app.state.event_failure_monitor.stop, name="event-failure-monitor", timeout_seconds=15)
    lifecycle.on_shutdown(
        app.state.historical_jobs.stop, name="historical-jobs", timeout_seconds=30
    )
    lifecycle.on_shutdown(
        app.state.walkforward_jobs.stop, name="walkforward-jobs", timeout_seconds=30
    )
    try:
        yield
    finally:
        await lifecycle.shutdown(reason="lifespan-exit")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()

    app = FastAPI(
        title=f"{resolved.app_name} API",
        version=resolved.version,
        description=(
            "AI-assisted Indian equity & F&O trading system. "
            "Trading mode is fixed at process start and cannot be changed at runtime."
        ),
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )

    app.state.historical_jobs = HistoricalJobs(resolved)
    app.state.walkforward_jobs = WalkForwardJobs(app.state.historical_jobs)
    app.state.news_runtime = NewsRuntime(resolved)
    app.state.instrument_runtime = InstrumentRuntime(resolved)
    app.state.event_failure_monitor = EventFailureMonitor(resolved)
    app.state.runtime_event_publisher = RuntimeEventPublisher(resolved)
    app.include_router(readiness_api.router, prefix=API_PREFIX)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(AuthenticationMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-Id", "X-Requested-With"],
    )

    @app.exception_handler(ATSError)
    async def _ats_error_handler(request: Request, exc: ATSError) -> JSONResponse:
        """Map the error taxonomy onto HTTP status codes — ERR-007."""
        from app.core.errors import (
            ConfigurationError,
            DataQualityError,
            NotFoundError,
            SafetyError,
            TransientError,
            ValidationError,
        )

        if isinstance(exc, NotFoundError):
            status_code = 404
        elif isinstance(exc, ValidationError):
            status_code = 422
        elif isinstance(exc, SafetyError):
            status_code = 423  # Locked: refused for a safety reason
        elif isinstance(exc, (ConfigurationError, DataQualityError)):
            status_code = 409
        elif isinstance(exc, TransientError):
            status_code = 503
        else:
            status_code = 500

        logger.warning(
            "Request failed",
            extra={"path": request.url.path, "error": exc.to_dict(), "status_code": status_code},
        )
        return JSONResponse(status_code=status_code, content={"error": exc.to_dict()})

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, error: AuthError):
        return JSONResponse(
            {"detail": error.message},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, error: RequestValidationError):
        return JSONResponse(
            {
                "detail": [
                    {key: item[key] for key in ("loc", "type", "msg")} for item in error.errors()
                ]
            },
            status_code=422,
        )

    app.include_router(auth_api.router, prefix=API_PREFIX)
    app.include_router(audit_api.router, prefix=API_PREFIX)
    app.include_router(backtests_api.router, prefix=API_PREFIX)
    app.include_router(historical_jobs_api.router, prefix=API_PREFIX)
    app.include_router(journal_api.router, prefix=API_PREFIX)
    app.include_router(market_api.router, prefix=API_PREFIX)
    app.include_router(event_failures_api.router, prefix=API_PREFIX)
    app.include_router(market_stream_api.router, prefix=API_PREFIX)
    app.include_router(workspace_stream_api.router, prefix=API_PREFIX)
    app.include_router(reconciliation_api.router, prefix=API_PREFIX)
    app.include_router(news_api.router, prefix=API_PREFIX)
    app.include_router(orders_api.router, prefix=API_PREFIX)
    app.include_router(reports_api.router, prefix=API_PREFIX)
    app.include_router(emergency_api.router, prefix=API_PREFIX)
    app.include_router(risk_api.router, prefix=API_PREFIX)
    app.include_router(workspace_api.router, prefix=API_PREFIX)
    app.include_router(health_api.router, prefix=API_PREFIX)
    app.include_router(fundamentals_api.router, prefix=API_PREFIX)
    app.include_router(system_api.router, prefix=API_PREFIX)
    app.include_router(strategies_api.router, prefix=API_PREFIX)

    return app


app = create_app()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # logging is configured in lifespan
        reload=os.getenv("UVICORN_RELOAD", "").lower() == "true",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
