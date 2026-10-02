"""Single-owner reference production from server-held evidence and broker account state."""

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import aliased

from app.agents.pipeline import DecisionContext
from app.agents.validation import ProposalValidator, ValidationMarket
from app.analysis.events import event_blackout
from app.analysis.regime.history import RegimeStore
from app.audit.service import AuditIdentity, AuditService
from app.core.enums import InstrumentType
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.audit import AuditEvent
from app.db.models.config import StrategyRegistration
from app.db.models.execution import PaperExecutionSlot
from app.db.models.instrument import Instrument
from app.execution.paper import stable_id
from app.modes import TradingMode
from app.risk.active import active_limits
from app.risk.models import BookLevel, MarketState
from app.strategies.base import StrategySpec
from app.strategies.reference import REFERENCE_IDS, ClosedCandleBreakout
from app.strategies.registry import specification_hash
from app.trading.contracts import derive_contract_costs
from app.trading.inputs import ReferenceInputStore
from app.trading.observations import ReferenceIngestion, ReferenceWarmupUnavailable
from app.trading.options import OptionEvidenceSource
from app.trading.reference import ReferenceDecisionService
from app.trading.regime import refresh_regime


def input_blocker(inputs, limits, history, as_of):
    if limits is None:
        return "RISK_CONFIGURATION_UNAVAILABLE"
    if inputs is None:
        return "CONTRACT_COST_AND_RESTRICTION_EVIDENCE_UNAVAILABLE"
    if history is None and inputs.regime_source is None:
        return "REGIME_UNAVAILABLE"
    if inputs.contract_source is not None and not inputs.contract_source.valid_at(as_of):
        return "CONTRACT_POLICY_OR_RESTRICTION_EXPIRED"
    if inputs.costs is not None and as_of - inputs.costs.observed_at > timedelta(
        seconds=limits.max_market_age_seconds
    ):
        return "CONTRACT_COST_AND_RESTRICTION_EVIDENCE_STALE"
    return None


class ReferenceRuntime:
    def __init__(self, executor, provider, *, calendar):
        self.executor = executor
        self.provider = provider
        self.clock = executor.clock
        self.calendar = calendar
        self.ingestion = ReferenceIngestion(
            provider,
            clock=self.clock,
            max_quote_age_seconds=executor.settings.tick_staleness_seconds,
        )
        self.inputs = ReferenceInputStore(self.clock)
        self.detail = "Reference producer has not run"
        self.latest_quotes = {}
        self.quote_refresh_error = False

    async def connect(self):
        await self.provider.connect()
        self.executor.source = self.provider.get_quote

    async def close(self):
        self.latest_quotes = {}
        await self.provider.close()

    async def refresh_quotes(self):
        if self.executor.settings.trading_mode != TradingMode.PAPER:
            raise ValueError("reference runtime requires PAPER")
        quotes = {}
        try:
            async with db_session.session_scope() as session:
                registrations = list(
                    (
                        await session.scalars(
                            sa.select(StrategyRegistration).where(
                                StrategyRegistration.strategy_id.in_(REFERENCE_IDS),
                                StrategyRegistration.version == "1",
                                StrategyRegistration.enabled_paper.is_(True),
                                StrategyRegistration.auto_disabled.is_(False),
                            )
                        )
                    ).all()
                )
            for registered in registrations:
                spec = StrategySpec.model_validate(registered.parameters)
                if specification_hash(spec) != registered.parameter_hash:
                    raise ValueError("reference specification changed")
                instrument = await self._instrument(spec)
                quotes[instrument.id] = await self.ingestion.observe_quote(instrument.id)
            self.latest_quotes = quotes
            self.quote_refresh_error = False
        except Exception:
            self.latest_quotes = {}
            self.quote_refresh_error = True
            raise

    async def _instrument(self, spec):
        async with db_session.session_scope() as session:
            instruments = list((await session.scalars(sa.select(Instrument))).all())
        selected = [
            item
            for item in instruments
            if item.key in spec.universe
            or f"{item.exchange.value}:{item.trading_symbol}" in spec.universe
        ]
        if len(selected) != 1:
            raise ValueError("reference version requires exactly one resolved instrument")
        expected = (
            InstrumentType.OPTION if spec.id == "long-option-breakout" else InstrumentType.EQUITY
        )
        if selected[0].instrument_type != expected:
            raise ValueError("reference strategy instrument class mismatch")
        return selected[0]

    async def cycle(self):
        if self.executor.settings.trading_mode != TradingMode.PAPER:
            raise ValueError("reference runtime requires PAPER")
        async with db_session.session_scope() as session:
            if await session.get(PaperExecutionSlot, "PAPER"):
                self.detail = "Monitoring reserved PAPER lifecycle"
                return
            terminal = aliased(AuditEvent)
            unresolved = await session.scalar(
                sa.select(AuditEvent.chain_id)
                .where(
                    AuditEvent.event_type == "REFERENCE_CYCLE_CLAIM",
                    ~sa.exists(
                        sa.select(terminal.id).where(
                            terminal.chain_id == AuditEvent.chain_id,
                            terminal.event_type.in_(
                                ["REFERENCE_CYCLE_RESULT", "REFERENCE_STAND_DOWN"]
                            ),
                        )
                    ),
                )
                .limit(1)
            )
            if unresolved:
                raise SafetyError("REFERENCE_INTERRUPTED_CYCLE_REVIEW_REQUIRED")
            registrations = list(
                (
                    await session.scalars(
                        sa.select(StrategyRegistration).where(
                            StrategyRegistration.strategy_id.in_(REFERENCE_IDS),
                            StrategyRegistration.version == "1",
                            StrategyRegistration.enabled_paper.is_(True),
                            StrategyRegistration.auto_disabled.is_(False),
                        )
                    )
                ).all()
            )
        self.detail = "No enabled reference strategy"
        for registered in registrations:
            await self._evaluate(registered)

    async def _record(self, identifier, event, instrument_id, result):
        await AuditService(self.clock).append(
            AuditIdentity(
                chain_id=identifier,
                event_type=event,
                actor="reference_runtime",
                mode=TradingMode.PAPER,
            ),
            {"instrument_id": instrument_id, "result": result},
        )

    async def _refresh_regime(self, source, instrument, identifier):
        try:
            return await refresh_regime(
                self.provider,
                source,
                underlying=instrument.underlying or instrument.trading_symbol,
                clock=self.clock,
            )
        except Exception as error:
            await self._record(
                identifier,
                "REFERENCE_STAND_DOWN",
                instrument.id,
                {"reason": "REGIME_REFRESH_FAILED", "error_type": type(error).__name__},
            )
            raise SafetyError("REGIME_REFRESH_FAILED") from error

    async def _contract_costs(
        self, identifier, instrument, observation, inputs, limits, *, option_evidence=None
    ):
        if inputs.costs is not None:
            return inputs.costs
        try:
            return await derive_contract_costs(
                self.executor,
                instrument,
                observation,
                inputs,
                option_evidence=option_evidence,
                max_age_seconds=min(
                    limits.max_market_age_seconds, self.executor.settings.tick_staleness_seconds
                ),
            )
        except ValueError:
            self.detail = "CONTRACT_DERIVATION_UNAVAILABLE"
            await self._record(
                identifier, "REFERENCE_STAND_DOWN", instrument.id, {"reason": self.detail}
            )
            return None

    async def _evaluate(self, registered):
        spec = StrategySpec.model_validate(registered.parameters)
        instrument = await self._instrument(spec)
        async with db_session.session_scope() as session:
            limits = await active_limits(session)
        as_of = self.clock.now()
        bar_end = as_of.replace(second=0, microsecond=0)
        identifier = stable_id(
            "cyc",
            ":".join(
                (
                    spec.id,
                    spec.version,
                    instrument.id,
                    self.provider.data_origin.value,
                    bar_end.isoformat(),
                )
            ),
        )
        async with db_session.session_scope() as session:
            claimed = await session.scalar(
                sa.select(AuditEvent.id).where(AuditEvent.chain_id == identifier).limit(1)
            )
            if claimed:
                if not await AuditService(self.clock).verify(identifier):
                    raise ValueError("reference cycle audit integrity failure")
                self.detail = "Closed candle already evaluated or reserved; no replay"
                return
            await AuditService(self.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=identifier,
                    event_type="REFERENCE_CYCLE_CLAIM",
                    actor="reference_runtime",
                    mode=TradingMode.PAPER,
                ),
                {
                    "instrument_id": instrument.id,
                    "strategy_id": spec.id,
                    "data_used": {
                        "closed_at": bar_end,
                        "parameter_hash": registered.parameter_hash,
                    },
                },
                expected_count=0,
            )
        inputs = await self.inputs.at(instrument.id, self.provider.data_origin, as_of)
        history = await RegimeStore(self.clock).at(
            instrument.underlying or instrument.trading_symbol,
            self.provider.data_origin,
            as_of,
        )
        reason = input_blocker(inputs, limits, history, as_of)
        if reason:
            self.detail = reason
            await self._record(
                identifier, "REFERENCE_STAND_DOWN", instrument.id, {"reason": reason}
            )
            return
        if inputs.regime_source is not None:
            history = await self._refresh_regime(inputs.regime_source, instrument, identifier)
        regime_id, regime = history
        try:
            observation = await self.ingestion.collect(instrument.id)
        except ReferenceWarmupUnavailable:
            self.detail = "REFERENCE_WARMUP_UNAVAILABLE"
            await self._record(
                identifier, "REFERENCE_STAND_DOWN", instrument.id, {"reason": self.detail}
            )
            return
        option_evidence = None
        if instrument.is_option:
            try:
                option_evidence = await OptionEvidenceSource(
                    self.provider, clock=self.clock
                ).observe(
                    instrument,
                    origin=observation.quote.data_origin,
                    max_age_seconds=limits.max_greeks_age_seconds,
                )
            except ValueError:
                self.detail = "OPTION_EVIDENCE_UNAVAILABLE"
                await self._record(
                    identifier, "REFERENCE_STAND_DOWN", instrument.id, {"reason": self.detail}
                )
                return
        costs = await self._contract_costs(
            identifier, instrument, observation, inputs, limits, option_evidence=option_evidence
        )
        if costs is None:
            return
        strategy = ClosedCandleBreakout(
            instrument.id,
            spec.universe[0],
            instrument.tick_size,
            spec.risk.requested_risk_fraction,
            clock=self.clock,
            long_option=spec.id == "long-option-breakout",
        )
        if specification_hash(strategy.spec) != registered.parameter_hash:
            raise ValueError("reference specification changed")
        await self.executor._reconcile()
        portfolio = await self.executor.portfolio_state(spec.id, self.provider.data_origin)
        quote = observation.quote
        now = self.clock.now()
        blackout = event_blackout(
            instrument.trading_symbol,
            as_of=now,
            calendar=regime.inputs.calendar,
            restricted_strategies=(spec.id,),
        )
        market = MarketState(
            instrument_id=instrument.id,
            as_of=now,
            observed_at=quote.observed_at,
            available_at=now,
            source_id=observation.chain_id,
            data_origin=quote.data_origin,
            bids=tuple(
                BookLevel(price=level.price, quantity=level.quantity) for level in quote.bids
            ),
            asks=tuple(
                BookLevel(price=level.price, quantity=level.quantity) for level in quote.asks
            ),
            mode=TradingMode.PAPER,
            ban_listed=inputs.ban_listed,
            greeks=option_evidence,
            news_halt=inputs.news_halt,
            event_blackout=blackout.status != "CLEAR",
            manually_blocked=inputs.manually_blocked,
        )
        context = DecisionContext(
            cycle_id=new_id("ref"),
            regime_id=regime_id,
            strategy=spec,
            market=market,
            portfolio=portfolio,
            costs=costs,
            limits=limits,
            blackout=blackout,
            prices=ValidationMarket(
                instrument_id=instrument.id,
                as_of=now,
                observed_at=quote.observed_at,
                available_at=now,
                data_origin=quote.data_origin,
                price=quote.ltp,
                atr=observation.available["indicators"].value["atr14"],
            ),
        )
        result = await ReferenceDecisionService(
            self.ingestion, ProposalValidator(inputs.validation, self.clock), calendar=self.calendar
        ).evaluate(strategy, context, observation=observation)
        self.detail = result.code
        await self._record(
            identifier, "REFERENCE_CYCLE_RESULT", instrument.id, result.model_dump(mode="json")
        )
