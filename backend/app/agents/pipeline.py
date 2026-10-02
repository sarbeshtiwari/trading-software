"""Shared advisory decision path; approvals here never authorize broker submission."""

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa
from pydantic import AwareDatetime, Field

from app.agents.proposal import EvidenceReference
from app.agents.tasks import PROPOSAL_AGENT
from app.agents.validation import ProposalValidator, ValidationMarket, _utc
from app.analysis.equity import EvidenceModel
from app.analysis.events import EventBlackout
from app.analysis.regime.classifier import RegimeDecision, evidence_fresh_at
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import freeze_snapshot
from app.config import get_settings
from app.core.enums import SignalDirection, TransactionType
from app.core.ids import new_id
from app.db import session as db_session
from app.db.models.decision import ConsideredCandidate, Proposal, RiskDecision, SizingRecord
from app.db.models.instrument import Instrument
from app.db.models.llm import LLMCall
from app.db.models.regime import RegimeHistory
from app.fno.restrictions import BanEntryError
from app.journal.rejections import record_rejection
from app.llm.fallback import DeterministicFallbackProvider, ProposalInputs
from app.llm.receipts import ReceiptError, ReceiptProposal, context_digest, resolve_receipt
from app.llm.service import review_signal
from app.llm.telemetry import record_fallback
from app.modes import TradingMode
from app.news.availability import research_at
from app.news.research_sentiment import sentiment_at
from app.portfolio.costs import FeeSchedule, risk_cost_reserve
from app.risk.active import active_limits
from app.risk.config import RiskLimits
from app.risk.engine import evaluate
from app.risk.entry_history import load_entry_history
from app.risk.entry_policy import apply_entry_policy
from app.risk.event_controls import EventControlError, require_event_entries
from app.risk.evidence import bind_decision
from app.risk.instrument_blocks import InstrumentEntryError, require_instrument_entries
from app.risk.models import EvidenceTime, MarketState, PortfolioState, RiskProposal
from app.risk.news_halts import NewsHaltError, require_no_news_halt
from app.risk.safety import RiskSafety
from app.sizing.lots import protective_ticks
from app.sizing.models import SizingInputs, SizingPolicy
from app.sizing.risk_based import size_position
from app.strategies.base import StrategySpec
from app.strategies.products import product_blocker
from app.strategies.registry import StrategyRegistry, specification_hash
from app.strategies.signal import Signal
from app.trading.options import require_option_observation


class ContractCosts(EvidenceTime):
    instrument_id: str
    margin_per_unit: Decimal = Field(gt=0)
    exposure_per_unit: Decimal = Field(gt=0)
    risk_cost_per_unit: Decimal = Field(ge=0)
    defined_max_loss_per_unit: Decimal | None = Field(default=None, gt=0)
    valid_until: AwareDatetime | None = None
    fee_schedule: FeeSchedule | None = None


class DecisionContext(EvidenceModel):
    cycle_id: str = Field(min_length=1, max_length=40)
    regime_id: str = Field(min_length=1)
    strategy: StrategySpec
    prices: ValidationMarket
    market: MarketState
    portfolio: PortfolioState
    costs: ContractCosts
    limits: RiskLimits
    llm_available: bool = False
    blackout: EventBlackout | None = None

    @classmethod
    def from_snapshot(cls, snapshot):
        metadata = {"validated_evidence", "regime_snapshot", "news_sentiment", "event_control"}
        return cls.model_validate(
            {key: value for key, value in snapshot.items() if key not in metadata}
        )


class PipelineResult(EvidenceModel):
    candidate_id: str
    proposal_id: str | None
    code: str
    approved_quantity: int = Field(ge=0)


class DecisionPipeline:
    def __init__(self, validator: ProposalValidator, *, calendar=None):
        self.calendar = calendar
        self.validator = validator
        self.safety = RiskSafety(clock=validator.clock)

    async def process(
        self,
        payload: str | dict | Signal | ReceiptProposal | None,
        context: DecisionContext,
        *,
        evidence: tuple[EvidenceReference, ...] = (),
        archive_fallback: bool = False,
    ) -> PipelineResult:
        try:
            context = DecisionContext.model_validate(context.model_dump())
            return await self._process(
                payload, context, evidence=evidence, archive_fallback=archive_fallback
            )
        except (NewsHaltError, InstrumentEntryError, EventControlError, BanEntryError) as error:
            return await self._negative(
                context, None, "RISK", error.code, decision_inputs=error.context
            )
        except ReceiptError as error:
            return await self._negative(context, None, "VALIDATION", error.code)
        except (ValueError, TypeError, ArithmeticError):
            await self.safety.trip_error(context.market.mode, context.market.data_origin)
            return await self._negative(context, None, "VALIDATION", "INVALID_DECISION_CONTEXT")
        except Exception:
            await self.safety.trip_error(context.market.mode, context.market.data_origin)
            raise

    async def _process(self, payload, context, *, evidence, archive_fallback):
        async with db_session.session_scope() as session:
            await require_no_news_halt(session, context.market)
        advisory_id = None
        receipt_context = context
        if isinstance(payload, ReceiptProposal):
            advisory_id = payload.call_id
            async with db_session.session_scope() as session:
                payload = (
                    await resolve_receipt(
                        session,
                        advisory_id,
                        context,
                        clock=self.validator.clock,
                    )
                ).model_dump()
            context = context.model_copy(update={"llm_available": True})
            age = self.validator.clock.now() - context.market.observed_at
            if age < timedelta(0) or age > timedelta(
                seconds=min(
                    context.limits.max_market_age_seconds,
                    self.validator.policy.max_market_age_seconds,
                )
            ):
                raise ReceiptError("STALE_AFTER_ADVISORY")
        async with db_session.session_scope() as session:
            await require_instrument_entries(session, context.market)
            event_control = await require_event_entries(
                session, context.market, strategy_id=context.strategy.id
            )
            instrument = await session.get(Instrument, context.market.instrument_id)
            regime = await session.get(RegimeHistory, context.regime_id)
        origin = "QUANT" if isinstance(payload, Signal) or payload is None else "LLM"
        code = await self._gate(context, instrument, regime, origin) or await self._safety_gate(
            context
        )
        if code:
            return await self._negative(context, instrument, "VALIDATION", code)
        if payload is None:
            return await self._negative(context, instrument, "SIGNAL", "NO_SIGNAL")
        if isinstance(payload, Signal):
            advisory_id, payload, code = await self._prepare_signal(
                payload, context, instrument, evidence, archive_fallback
            )
            if code:
                return await self._negative(
                    context,
                    instrument,
                    "VALIDATION" if code == "STALE_AFTER_ADVISORY" else "SIGNAL",
                    code,
                )
            if isinstance(payload, ReceiptProposal):
                return await self._process(
                    payload, context, evidence=evidence, archive_fallback=False
                )
        return await self._decide(
            payload,
            context,
            instrument,
            regime,
            origin=origin,
            advisory_id=advisory_id,
            receipt_context=receipt_context,
            event_control=event_control,
        )

    async def _decide(
        self,
        payload,
        context,
        instrument,
        regime,
        *,
        origin,
        advisory_id,
        receipt_context,
        event_control,
    ):
        validation = await self.validator.validate(payload, context.prices)
        if not validation.valid:
            numeric_inputs = None
            if validation.proposal is not None:
                numeric_inputs = validation.proposal.model_dump(
                    mode="json", exclude={"thesis", "invalidation_conditions"}
                )
            return await self._negative(
                context, instrument, "VALIDATION", validation.code, decision_inputs=numeric_inputs
            )
        proposal = validation.proposal
        research_code, sentiment_snapshot = await self._research_evidence(
            proposal, context, instrument
        )
        if research_code:
            return await self._negative(context, instrument, "VALIDATION", research_code)
        code = None
        if proposal.strategy != context.strategy.id or proposal.instrument != instrument.id:
            code = "PROPOSAL_CONTEXT_MISMATCH"
        elif (
            abs(proposal.target - proposal.entry) / abs(proposal.entry - proposal.stop_loss)
            < context.strategy.risk.minimum_reward_risk
        ):
            code = "STRATEGY_REWARD_RISK"
        if code:
            return await self._negative(context, instrument, "VALIDATION", code)
        if origin == "LLM" and advisory_id is None:
            raise ReceiptError("LLM_RECEIPT_REQUIRED")
        identifier = new_id("prp")
        inputs, policy, sized, context = self._costed_sizing(
            proposal, context, instrument, identifier
        )
        risk_proposal = None
        decision = None
        code = sized.zero_reason
        if sized.quantity:
            risk_proposal = self._risk_proposal(proposal, context, instrument, identifier, sized)
            decision = await self._evaluate(risk_proposal, context)
            code = "RISK_APPROVED" if decision.approved else decision.rejection_code
        quantity = decision.approved_quantity if decision else 0
        candidate = self._candidate(context, instrument, "RISK" if decision else "SIZING", code)
        async with db_session.session_scope() as session:
            call = await self._link_advisory(session, advisory_id, identifier)
            if origin == "LLM":
                await resolve_receipt(
                    session,
                    advisory_id,
                    receipt_context,
                    clock=self.validator.clock,
                    linked_to=identifier,
                    evidence_snapshots=validation.evidence_snapshots,
                )
            if quantity:
                event_control = await self.safety.require_entries_in_session(
                    session, context.market, context.limits, strategy_id=context.strategy.id
                )
            session.add(
                Proposal(
                    id=identifier,
                    instrument_id=instrument.id,
                    trading_symbol=instrument.trading_symbol,
                    segment=instrument.segment,
                    product=context.strategy.product,
                    direction=proposal.direction,
                    strategy_id=proposal.strategy,
                    strategy_version=context.strategy.version,
                    entry_price=sized.entry,
                    stop_loss=sized.stop,
                    target_price=proposal.target,
                    suggested_quantity=proposal.quantity if origin == "LLM" else None,
                    approved_quantity=quantity,
                    confidence=proposal.confidence,
                    thesis=proposal.thesis,
                    evidence=[item.model_dump() for item in proposal.evidence],
                    invalidation_conditions=list(proposal.invalidation_conditions),
                    status="RISK_APPROVED" if quantity else "RISK_REJECTED",
                    rejection_code=None if quantity else code,
                    origin=origin,
                    llm_call_id=advisory_id,
                    prompt_version=call.prompt_version if call else None,
                    model_id=call.model_id if call else None,
                    regime=RegimeDecision.model_validate(regime.decision).label,
                    context_snapshot={
                        **context.model_dump(mode="json"),
                        **(
                            {"event_control": event_control.model_dump(mode="json")}
                            if event_control.event_id
                            else {}
                        ),
                        **({"news_sentiment": sentiment_snapshot} if sentiment_snapshot else {}),
                        "validated_evidence": validation.evidence_snapshots,
                        "regime_snapshot": freeze_snapshot(regime.decision),
                    },
                    mode=context.market.mode,
                    correlation_id=context.cycle_id,
                )
            )
            session.add(
                SizingRecord(
                    proposal_id=identifier,
                    method="RISK_BASED",
                    formula_version=sized.formula_version,
                    capital=sized.capital,
                    risk_budget=sized.risk_budget,
                    risk_per_unit=sized.risk_per_unit,
                    raw_quantity=sized.raw_quantity,
                    lot_size=instrument.lot_size,
                    final_quantity=sized.quantity,
                    binding_constraint=sized.binding_constraint,
                    zero_reason=sized.zero_reason,
                    inputs={
                        "request": inputs.model_dump(mode="json"),
                        "policy": policy.model_dump(mode="json"),
                        "result": sized.model_dump(mode="json"),
                    },
                )
            )
            if decision:
                recorded_risk = RiskDecision(
                    proposal_id=identifier,
                    approved=decision.approved,
                    binding_rule=decision.binding_rule,
                    rejection_code=decision.rejection_code,
                    approved_quantity=quantity,
                    risk_amount=decision.risk_amount,
                    risk_config_version=context.limits.version,
                    mode=context.market.mode,
                    evaluated_at=_utc(context.market.as_of),
                    rules_evaluated=[rule.model_dump(mode="json") for rule in decision.rules],
                    state_snapshot={
                        "proposal": risk_proposal.model_dump(mode="json"),
                        "portfolio": context.portfolio.model_dump(mode="json"),
                        "market": context.market.model_dump(mode="json"),
                        "config": context.limits.model_dump(mode="json"),
                        "decision": decision.model_dump(mode="json"),
                    },
                )
                session.add(recorded_risk)
                await bind_decision(
                    session, recorded_risk, clock=self.validator.clock, actor="decision_pipeline"
                )
            session.add(candidate)
            await AuditService(self.validator.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=identifier,
                    event_type="DECISION",
                    actor="decision_pipeline",
                    mode=context.market.mode,
                ),
                {
                    "proposal_id": identifier,
                    "instrument_id": instrument.id,
                    "strategy_id": proposal.strategy,
                    "correlation_id": context.cycle_id,
                    "market_state": context.market.model_dump(mode="json"),
                    "data_used": {
                        "advisory_call_id": advisory_id,
                        "context": context.model_dump(mode="json"),
                        **({"news_sentiment": sentiment_snapshot} if sentiment_snapshot else {}),
                        "validated_evidence": validation.evidence_snapshots,
                        "regime_snapshot": regime.decision,
                    },
                    "signal": proposal.model_dump(mode="json"),
                    "risk_calculation": decision.model_dump(mode="json") if decision else None,
                    "position_size": sized.model_dump(mode="json"),
                    "decision": "APPROVED" if quantity else "REJECTED",
                    "risk_verdict": "APPROVED" if quantity else "REJECTED",
                    "result": {"reason_code": code, "stage": "RISK" if decision else "SIZING"},
                },
            )
            await record_rejection(
                session,
                candidate,
                context,
                self.validator.clock,
                proposal_id=identifier,
                approved_quantity=quantity,
            )
        return PipelineResult(
            candidate_id=candidate.id, proposal_id=identifier, code=code, approved_quantity=quantity
        )

    async def _research_evidence(self, proposal, context, instrument):
        if "news" not in context.strategy.required_inputs:
            return None, None
        research = await research_at(
            instrument.id,
            as_of=context.market.as_of,
            origin=context.market.data_origin,
            max_age_seconds=context.strategy.max_input_age_seconds,
        )
        cited = {reference.source_id for reference in proposal.evidence if reference.kind == "NEWS"}
        admitted = {article["source_id"] for article in research.articles}
        if not cited or not cited.issubset(admitted):
            return "NEWS_EVIDENCE_REQUIRED", None
        if "sentiment" not in context.strategy.required_inputs:
            return None, None
        sentiment = await sentiment_at(
            instrument.id,
            as_of=context.market.as_of,
            origin=context.market.data_origin,
            max_age_seconds=context.strategy.max_input_age_seconds,
        )
        if sentiment.status != "AVAILABLE":
            return sentiment.code, None
        if not set(sentiment.admission_ids).issubset(cited):
            return "SENTIMENT_EVIDENCE_REQUIRED", None
        return None, sentiment.model_dump(mode="json")

    async def _prepare_signal(self, signal, context, instrument, evidence, archive):
        signal = Signal.model_validate(signal.model_dump())
        if (
            signal.instrument_id != instrument.id
            or signal.strategy_id != context.strategy.id
            or signal.strategy_version != context.strategy.version
            or signal.generated_at != context.market.as_of
            or signal.data_origin != context.market.data_origin
            or signal.product != context.strategy.product
            or signal.timeframe_seconds != context.strategy.timeframe_seconds
            or signal.instrument_key not in context.strategy.universe
        ):
            return None, None, "SIGNAL_CONTEXT_MISMATCH"
        identifier, proposal = await self._signal_proposal(
            signal, context, instrument, evidence, archive
        )
        code = None if proposal is not None else "ADVISORY_ABSTAIN"
        age = self.validator.clock.now() - context.market.observed_at
        if age > timedelta(
            seconds=min(
                context.limits.max_market_age_seconds, self.validator.policy.max_market_age_seconds
            )
        ) or age < timedelta(0):
            code = "STALE_AFTER_ADVISORY"
        return identifier, proposal, code

    async def _signal_proposal(self, signal, context, instrument, evidence, archive):
        inputs = ProposalInputs(
            signal=signal,
            lot_size=instrument.lot_size,
            evidence=evidence,
            exit_rules=context.strategy.exit_rules,
        )
        identifier = None
        settings = get_settings()
        if archive and settings.llm_reference_review_enabled:
            preliminary = await self.validator.validate(
                DeterministicFallbackProvider().propose(inputs).model_dump(), context.prices
            )
            if not preliminary.valid:
                return None, preliminary.proposal.model_dump()
            inputs = inputs.model_copy(update={"grounded_evidence": preliminary.evidence_snapshots})
            identifier, proposal = await review_signal(
                inputs,
                settings,
                clock=self.validator.clock,
                mode=context.market.mode,
                correlation_id=context.cycle_id,
                task=PROPOSAL_AGENT if settings.llm_reference_task == "proposal" else None,
                decision_binding=context_digest(context),
            )
        elif archive:
            identifier, proposal = await record_fallback(
                inputs,
                clock=self.validator.clock,
                mode=context.market.mode,
                correlation_id=context.cycle_id,
            )
        else:
            proposal = DeterministicFallbackProvider().propose(inputs)
        return identifier, (
            proposal
            if isinstance(proposal, ReceiptProposal)
            else proposal.model_dump()
            if proposal is not None
            else None
        )

    async def _link_advisory(self, session, advisory_id, proposal_id):
        if advisory_id is None:
            return
        claimed = await session.execute(
            sa.update(LLMCall)
            .where(
                LLMCall.id == advisory_id,
                LLMCall.proposal_id.is_(None),
            )
            .values(proposal_id=proposal_id)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ReceiptError("LLM_RECEIPT_CONSUMED_OR_MISSING")
        return await session.get(LLMCall, advisory_id, populate_existing=True)

    def _risk_proposal(self, proposal, context, instrument, identifier, sized):
        premium = (
            sized.entry
            if instrument.is_option and proposal.direction == SignalDirection.LONG
            else Decimal(0)
        )
        return RiskProposal(
            id=identifier,
            instrument_id=instrument.id,
            strategy_id=proposal.strategy,
            sector=instrument.sector,
            underlying=instrument.underlying or instrument.trading_symbol,
            generated_at=context.market.as_of,
            data_origin=context.market.data_origin,
            direction=proposal.direction,
            quantity=sized.quantity,
            lot_size=instrument.lot_size,
            tick_size=instrument.tick_size,
            entry=sized.entry,
            stop=sized.stop,
            first_target=proposal.target,
            exposure_per_unit=max(premium, context.costs.exposure_per_unit),
            margin_per_unit=max(premium, context.costs.margin_per_unit),
            risk_cost_per_unit=context.costs.risk_cost_per_unit,
            is_option=instrument.is_option,
            defined_max_loss_per_unit=max(
                premium, context.costs.defined_max_loss_per_unit or Decimal(0)
            )
            or None,
        )

    def _costed_sizing(self, proposal, context, instrument, identifier):
        for _attempt in range(16):
            inputs, policy = self._sizing(proposal, context, instrument, identifier)
            sized = size_position(inputs, policy)
            schedule = context.costs.fee_schedule
            if not sized.quantity or schedule is None:
                return inputs, policy, sized, context
            risk_proposal = self._risk_proposal(proposal, context, instrument, identifier, sized)
            side = (
                TransactionType.BUY
                if proposal.direction == SignalDirection.LONG
                else TransactionType.SELL
            )
            reserve = risk_cost_reserve(schedule, risk_proposal, side, as_of=context.market.as_of)
            if reserve <= context.costs.risk_cost_per_unit:
                return inputs, policy, sized, context
            context = context.model_copy(
                update={"costs": context.costs.model_copy(update={"risk_cost_per_unit": reserve})}
            )
        raise ValueError("quantity-dependent fee sizing did not converge")

    async def _evaluate(self, proposal, context):
        decision = evaluate(proposal, context.portfolio, context.market, context.limits)
        async with db_session.session_scope() as session:
            decision = apply_entry_policy(
                decision,
                await load_entry_history(
                    session, context.market, get_settings(), calendar=self.calendar
                ),
            )
        if decision.rejection_code == "RISK_ENGINE_ERROR":
            await self.safety.trip_error(context.market.mode, context.market.data_origin)
        return decision

    async def _safety_gate(self, context):
        async with db_session.session_scope() as session:
            configured = await active_limits(session)
        if configured is not None and configured != context.limits:
            return "RISK_CONFIG_VERSION_MISMATCH"
        safety = await self.safety.observe(context.portfolio, context.market, context.limits)
        return safety.code or (
            "TRADING_GATE_BLOCKED" if not self.safety.gate.new_entries_allowed else None
        )

    async def _gate(self, context, instrument, regime, origin):
        registered = await StrategyRegistry().get(context.strategy.id, context.strategy.version)
        if registered is None or registered.parameter_hash != specification_hash(context.strategy):
            return "UNREGISTERED_OR_CHANGED_STRATEGY"
        if (
            context.market.mode != TradingMode.PAPER
            or not registered.enabled_paper
            or registered.auto_disabled
        ):
            return "STRATEGY_MODE_DISABLED"
        if instrument is None or not instrument.sector:
            return "INSTRUMENT_OR_SECTOR_UNAVAILABLE"
        if (
            instrument.key not in context.strategy.universe
            and f"{instrument.exchange.value}:{instrument.trading_symbol}"
            not in context.strategy.universe
        ):
            return "OUTSIDE_UNIVERSE"
        if (
            context.prices.instrument_id != instrument.id
            or context.costs.instrument_id != instrument.id
            or context.portfolio.strategy_id != context.strategy.id
            or context.prices.as_of != context.market.as_of
            or len(
                {
                    context.prices.data_origin,
                    context.market.data_origin,
                    context.costs.data_origin,
                    context.portfolio.data_origin,
                }
            )
            != 1
        ):
            return "CONTEXT_IDENTITY_OR_ORIGIN_MISMATCH"
        news_code = None
        if instrument.is_option or context.market.greeks is not None:
            try:
                await require_option_observation(
                    instrument,
                    context.market.greeks,
                    as_of=context.market.as_of,
                    origin=context.market.data_origin,
                    max_age_seconds=context.limits.max_greeks_age_seconds,
                )
            except ValueError:
                news_code = "OPTION_OBSERVATION_UNAVAILABLE_OR_CHANGED"
        if news_code is None and "news" in context.strategy.required_inputs:
            research = await research_at(
                instrument.id,
                as_of=context.market.as_of,
                origin=context.market.data_origin,
                max_age_seconds=context.strategy.max_input_age_seconds,
            )
            if research.status != "AVAILABLE":
                news_code = research.code
        if news_code is None and {"news", "sentiment"}.issubset(context.strategy.required_inputs):
            sentiment = await sentiment_at(
                instrument.id,
                as_of=context.market.as_of,
                origin=context.market.data_origin,
                max_age_seconds=context.strategy.max_input_age_seconds,
            )
            if sentiment.status != "AVAILABLE":
                news_code = sentiment.code
        return news_code or self._evidence_gate(context, instrument, regime, origin)

    def _evidence_gate(self, context, instrument, regime, origin):
        as_of = context.market.as_of
        if (
            regime is None
            or _utc(regime.ts) > as_of
            or _utc(regime.created_at) > as_of
            or regime.data_origin != context.market.data_origin.value
        ):
            return "REGIME_UNAVAILABLE"
        decision = RegimeDecision.model_validate(regime.decision)
        if (
            not evidence_fresh_at(decision, as_of)
            or decision.label not in context.strategy.permitted_regimes
            or not timedelta(0)
            <= as_of - decision.inputs.as_of
            <= timedelta(seconds=context.strategy.max_input_age_seconds)
        ):
            return "REGIME_NOT_PERMITTED"
        if context.strategy.requires_llm and origin != "LLM":
            return "LLM_RECEIPT_REQUIRED"
        if origin == "LLM" and not context.llm_available:
            return "LLM_UNAVAILABLE"
        reason = self._contract_gate(context, instrument)
        return reason or self._context_freshness(context, instrument)

    def _contract_gate(self, context, instrument):
        blocked = product_blocker(
            context.strategy.product,
            instrument.segment,
            allow_positional=get_settings().allow_positional,
        )
        if blocked:
            return blocked
        as_of = context.market.as_of
        if context.costs.valid_until is not None and as_of >= context.costs.valid_until:
            return "EXPIRED_CONTRACT_POLICY"
        schedule = context.costs.fee_schedule
        if schedule is not None:
            if schedule.charge_basis == "OPTION_PREMIUM" and not instrument.is_option:
                return "CONTRACT_FEE_SOURCE_MISMATCH"
            if (schedule.exchange, schedule.segment, schedule.product) != (
                instrument.exchange,
                instrument.segment,
                context.strategy.product,
            ):
                return "CONTRACT_FEE_SOURCE_MISMATCH"
            try:
                schedule.require_at(as_of)
            except ValueError:
                return "CONTRACT_FEE_SOURCE_UNAVAILABLE"
        return None

    def _context_freshness(self, context, instrument):
        as_of = context.market.as_of
        for item, age in (
            (context.costs, context.limits.max_market_age_seconds),
            (context.portfolio, context.limits.max_portfolio_age_seconds),
        ):
            if (
                not item.observed_at <= item.available_at <= as_of
                or as_of - item.observed_at > timedelta(seconds=age)
            ):
                return "STALE_OR_FUTURE_CONTEXT"
        if context.strategy.requires_event_calendar or context.blackout is not None:
            blackout = context.blackout
            if (
                blackout is None
                or blackout.status != "CLEAR"
                or blackout.symbol not in (instrument.trading_symbol, instrument.key)
                or not timedelta(0)
                <= as_of - blackout.as_of
                <= timedelta(seconds=context.strategy.max_input_age_seconds)
            ):
                return "EVENT_BLACKOUT_OR_UNAVAILABLE"
        return None

    def _sizing(self, proposal, context, instrument, identifier):
        account, limits, costs = context.portfolio, context.limits, context.costs
        premium = (
            protective_ticks(
                proposal.entry, proposal.stop_loss, instrument.tick_size, proposal.direction
            )[0]
            if instrument.is_option and proposal.direction == SignalDirection.LONG
            else Decimal(0)
        )
        policy = SizingPolicy(
            configured_capital=limits.capital,
            per_trade_risk_pct=limits.per_trade_risk_pct,
            daily_loss_limit_pct=limits.daily_loss_limit_pct,
            max_gross_exposure_multiple=limits.max_gross_exposure_multiple,
            max_instrument_exposure_pct=limits.max_instrument_exposure_pct,
            margin_buffer_pct=limits.margin_buffer_pct,
            max_age_seconds=min(limits.max_market_age_seconds, limits.max_portfolio_age_seconds),
        )
        inputs = SizingInputs(
            proposal_id=identifier,
            instrument_id=instrument.id,
            as_of=context.market.as_of,
            observed_at=min(account.observed_at, costs.observed_at),
            available_at=max(account.available_at, costs.available_at),
            source_ids=(account.source_id, costs.source_id),
            data_origin=context.market.data_origin,
            equity=account.equity,
            available_margin=account.available_margin,
            gross_exposure=sum((item.notional for item in account.exposures), Decimal(0)),
            instrument_exposure=sum(
                (
                    item.notional
                    for item in account.exposures
                    if item.instrument_id == instrument.id
                ),
                Decimal(0),
            ),
            daily_loss=max(Decimal(0), -account.realised_day_pnl - account.unrealised_day_pnl),
            reserved_risk=account.reserved_risk,
            entry=proposal.entry,
            stop=proposal.stop_loss,
            direction=proposal.direction,
            lot_size=instrument.lot_size,
            tick_size=instrument.tick_size,
            margin_per_unit=max(premium, costs.margin_per_unit),
            exposure_per_unit=max(premium, costs.exposure_per_unit),
            risk_cost_per_unit=costs.risk_cost_per_unit,
            defined_max_loss_per_unit=max(premium, costs.defined_max_loss_per_unit or Decimal(0))
            or None,
            requested_risk_pct=context.strategy.risk.requested_risk_fraction * 100,
        )
        return inputs, policy

    def _candidate(self, context, instrument, stage, code):
        return ConsideredCandidate(
            id=new_id("cnd"),
            cycle_id=context.cycle_id,
            instrument_id=context.market.instrument_id,
            trading_symbol=instrument.trading_symbol
            if instrument
            else context.market.instrument_id,
            strategy_id=context.strategy.id,
            stopped_at_stage=stage,
            reason_code=code,
            inputs=context.model_dump(mode="json"),
            mode=context.market.mode,
        )

    async def _negative(self, context, instrument, stage, code, *, decision_inputs=None):
        row = self._candidate(context, instrument, stage, code)
        async with db_session.session_scope() as session:
            session.add(row)
            await AuditService(self.validator.clock).append_in_session(
                session,
                AuditIdentity(
                    chain_id=row.id,
                    event_type="NEGATIVE_DECISION",
                    actor="decision_pipeline",
                    mode=context.market.mode,
                ),
                {
                    "instrument_id": context.market.instrument_id,
                    "strategy_id": context.strategy.id,
                    "correlation_id": context.cycle_id,
                    "data_used": {
                        "context": context.model_dump(mode="json"),
                        "validation_policy": self.validator.policy.model_dump(mode="json"),
                    },
                    "signal": decision_inputs,
                    "decision": "REJECTED",
                    "risk_verdict": "REJECTED",
                    "result": {"stage": stage, "candidate_id": row.id, "reason_code": code},
                },
            )
            await record_rejection(session, row, context, self.validator.clock)
        return PipelineResult(candidate_id=row.id, proposal_id=None, code=code, approved_quantity=0)
