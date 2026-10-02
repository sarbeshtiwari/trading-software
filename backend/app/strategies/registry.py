"""STRAT-002: persist immutable strategy versions with separate mode enablement."""

import hashlib
import json

import sqlalchemy as sa

from app.audit.service import AuditIdentity, AuditService
from app.db import session as db_session
from app.db.models.config import StrategyRegistration
from app.modes import TradingMode
from app.strategies.base import Strategy, StrategySpec


def specification_hash(spec: StrategySpec) -> str:
    payload = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class StrategyRegistry:
    async def register(
        self,
        strategy: Strategy,
        *,
        enabled_paper: bool = False,
        actor: str = "system",
        reason: str = "Application strategy registration",
    ) -> str:
        if not isinstance(strategy, Strategy):
            raise ValueError("strategy must implement the Strategy contract")
        if getattr(strategy.exit, "__func__", None) is not Strategy.exit:
            raise ValueError("strategy cannot replace the deterministic exit evaluator")
        spec = StrategySpec.model_validate(strategy.spec.model_dump())
        fingerprint = specification_hash(spec)
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(StrategyRegistration).where(
                    StrategyRegistration.strategy_id == spec.id,
                    StrategyRegistration.version == spec.version,
                )
            )
            if row:
                if row.parameter_hash != fingerprint:
                    raise ValueError("strategy parameters changed without a new version")
                return row.id
            row = StrategyRegistration(
                strategy_id=spec.id,
                version=spec.version,
                parameter_hash=fingerprint,
                parameters=spec.model_dump(mode="json"),
                enabled_paper=enabled_paper,
                enabled_supervised=False,
                enabled_live=False,
                live_approved=False,
            )
            session.add(row)
            await session.flush()
            await AuditService().append_in_session(
                session,
                AuditIdentity(
                    chain_id=row.id,
                    event_type="STRATEGY_REGISTERED",
                    actor=actor,
                    mode=TradingMode.PAPER,
                ),
                {
                    "strategy_id": spec.id,
                    "result": {
                        "parameter_hash": fingerprint,
                        "enabled_paper": enabled_paper,
                        "reason": reason,
                    },
                },
            )
            return row.id

    async def get(self, strategy_id: str, version: str) -> StrategyRegistration | None:
        async with db_session.session_scope() as session:
            return await session.scalar(
                sa.select(StrategyRegistration).where(
                    StrategyRegistration.strategy_id == strategy_id,
                    StrategyRegistration.version == version,
                )
            )

    async def set_enabled(
        self,
        strategy_id: str,
        version: str,
        mode: TradingMode,
        enabled: bool,
        *,
        actor: str = "system",
        reason: str = "Application strategy enablement",
    ) -> None:
        if mode == TradingMode.LIVE and enabled:
            raise ValueError("STRATEGY NOT APPROVED FOR LIVE TRADING")
        attribute = {
            TradingMode.PAPER: "enabled_paper",
            TradingMode.SUPERVISED: "enabled_supervised",
            TradingMode.LIVE: "enabled_live",
        }[mode]
        async with db_session.session_scope() as session:
            row = await session.scalar(
                sa.select(StrategyRegistration).where(
                    StrategyRegistration.strategy_id == strategy_id,
                    StrategyRegistration.version == version,
                )
            )
            if row is None:
                raise ValueError("unregistered strategy")
            if enabled and row.auto_disabled:
                raise ValueError("strategy auto-disabled; explicit audited review/reset required")
            setattr(row, attribute, enabled)
            await AuditService().append_in_session(
                session,
                AuditIdentity(
                    chain_id=row.id, event_type="STRATEGY_ENABLEMENT", actor=actor, mode=mode
                ),
                {
                    "strategy_id": strategy_id,
                    "result": {"enabled": enabled, "reason": reason, "version": version},
                },
            )

    async def list(self) -> list[dict]:
        async with db_session.session_scope() as session:
            rows = (
                await session.scalars(
                    sa.select(StrategyRegistration).order_by(
                        StrategyRegistration.strategy_id, StrategyRegistration.version
                    )
                )
            ).all()
        return [
            {
                "id": row.strategy_id,
                "version": row.version,
                "parameter_hash": row.parameter_hash,
                "enabled_paper": row.enabled_paper,
                "enabled_supervised": row.enabled_supervised,
                "enabled_live": row.enabled_live,
                "live_approved": row.live_approved,
                "auto_disabled": row.auto_disabled,
                "auto_disabled_reason": row.auto_disabled_reason,
            }
            for row in rows
        ]
