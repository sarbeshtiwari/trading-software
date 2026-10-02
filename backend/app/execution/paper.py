"""Serialized PAPER lifecycle using durable intents and the existing broker.

The initial worker reserves the entire account for one trade lifecycle. It never
resubmits an ambiguous intent. Costs require an explicit effective PAPER tariff.
No public API accepts order parameters or caller-supplied account evidence.
"""

import asyncio
import hashlib
from datetime import timedelta
from decimal import Decimal
from functools import wraps

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.agents.pipeline import DecisionContext
from app.agents.validation import _utc
from app.audit.service import AuditIdentity, AuditService
from app.brokers.models import OrderRequest
from app.brokers.paper.constraints import FillConstraints
from app.brokers.paper.provider import PaperBrokerProvider
from app.brokers.paper.state import PaperStateStore
from app.config import get_settings
from app.core.clock import IST, UTC, get_clock
from app.core.data_origin import DataOrigin, ExecutionRealism
from app.core.enums import (
    ExitReason,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionState,
    SignalDirection,
    TransactionType,
)
from app.core.errors import SafetyError
from app.core.ids import new_id
from app.core.money import quantize_money
from app.db import session as db_session
from app.db.models.decision import Proposal, RiskDecision
from app.db.models.execution import PaperExecutionSlot
from app.db.models.instrument import Instrument
from app.db.models.journal import JournalEntry
from app.db.models.system import SINGLETON_ID, PortfolioSnapshot, SystemState
from app.db.models.trading import Order, Position, Trade
from app.emergency.rejections import observe_rejection
from app.execution.freshness import require_entry_sources
from app.execution.replacement_state import replacement_parent
from app.execution.state import transition
from app.marketdata.circuits import circuit_status
from app.marketdata.models import InstrumentRef
from app.modes import TradingMode
from app.monitoring.gate import get_trading_gate
from app.notifications.incidents import PaperIncidents
from app.notifications.outbox import enqueue
from app.notifications.runtime import emit_critical
from app.portfolio.cost_store import CostStore
from app.portfolio.costs import FeeSchedule, risk_cost_reserve
from app.portfolio.fifo import Lot, match_fill
from app.portfolio.fill_evidence import record_fill
from app.portfolio.journal_integrity import bind_journal
from app.risk.active import active_limits
from app.risk.audit import RiskAudit
from app.risk.engine import evaluate
from app.risk.entry_history import load_entry_history
from app.risk.entry_policy import apply_entry_policy, entry_rules
from app.risk.models import BookLevel, Exposure, PortfolioState, RiskProposal
from app.risk.safety import RiskSafety
from app.strategies.base import StrategySpec
from app.strategies.monitor import enforce as enforce_degradation
from app.strategies.products import product_blocker
from app.strategies.registry import StrategyRegistry, specification_hash


def stable_id(prefix, identity, size=32):
    return prefix + hashlib.sha256(identity.encode()).hexdigest()[:size]


def replay_fills(history):
    if any(not trade.cost_breakdown or "fifo" not in trade.cost_breakdown for trade in history):
        raise SafetyError("FIFO_HISTORY_RECONSTRUCTION_REQUIRED")
    history = sorted(history, key=lambda trade: trade.cost_breakdown["fifo"]["sequence"])
    lots, realised = (), Decimal(0)
    for sequence, trade in enumerate(history, start=1):
        if trade.cost_breakdown["fifo"]["sequence"] != sequence:
            raise SafetyError("FIFO_SEQUENCE_MISMATCH")
        result = match_fill(
            lots, Lot(trade.quantity * trade.transaction_type.sign, trade.price, trade.id)
        )
        lots, realised = result.lots, realised + result.realised
    return lots, realised


async def approved_risk(proposal, risk):
    replay = await RiskAudit().replay(risk.id)
    if not replay.approved or not await AuditService().verify(proposal.id, expected_count=1):
        raise SafetyError("APPROVAL_INTEGRITY_FAILURE")
    planned = RiskProposal.model_validate(risk.state_snapshot["proposal"])
    if (
        planned.id != proposal.id
        or planned.instrument_id != proposal.instrument_id
        or planned.strategy_id != proposal.strategy_id
        or planned.entry != proposal.entry_price
        or planned.stop != proposal.stop_loss
        or planned.first_target != proposal.target_price
        or planned.direction != proposal.direction
        or planned.quantity != proposal.approved_quantity
        or risk.approved_quantity != proposal.approved_quantity
    ):
        raise SafetyError("APPROVAL_LINEAGE_MISMATCH")
    return planned


async def require_enabled_strategy(proposal, settings, *, clock=None):
    registered = await StrategyRegistry().get(proposal.strategy_id, proposal.strategy_version)
    if registered is None or not registered.enabled_paper or registered.auto_disabled:
        raise SafetyError("STRATEGY_DISABLED")
    specification = StrategySpec.model_validate(registered.parameters)
    if (
        specification_hash(specification) != registered.parameter_hash
        or proposal.product != specification.product
        or proposal.context_snapshot.get("strategy") != specification.model_dump(mode="json")
    ):
        raise SafetyError("STRATEGY_PRODUCT_OR_CONTRACT_MISMATCH")
    evaluation = await enforce_degradation(
        proposal.strategy_id, proposal.strategy_version, clock=clock
    )
    if evaluation.auto_disabled:
        raise SafetyError("STRATEGY_AUTO_DISABLED")
    blocked = product_blocker(
        proposal.product, proposal.segment, allow_positional=settings.allow_positional
    )
    if blocked:
        raise SafetyError(blocked)


def storage_guard(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except SQLAlchemyError:
            self.ready = False
            self.gate.block("paper_storage", "PAPER_STORAGE_UNAVAILABLE_REVIEW_REQUIRED")
            raise

    return guarded


class PaperExecution:
    def __init__(self, quote_source, *, settings=None, clock=None, fill_config=None, calendar=None):
        self.calendar = calendar
        self.settings = settings or get_settings()
        if self.settings.trading_mode != TradingMode.PAPER:
            raise SafetyError("PAPER execution cannot dispatch in another mode")
        self.clock = clock or get_clock()
        self.source = quote_source
        self.fill_config = fill_config
        self.broker = PaperBrokerProvider(
            self.settings,
            clock=self.clock,
            quote_source=self.quote,
            state_store=PaperStateStore(),
            fill_config=fill_config,
            cost_source=self._fees_for_order,
            constraint_source=self._constraints_for_order,
        )
        self.gate = get_trading_gate()
        self.safety = RiskSafety(clock=self.clock)
        self.ready = False

    async def _constraints_for_order(self, request, as_of):
        async with db_session.session_scope() as session:
            order = await session.scalar(
                sa.select(Order).where(
                    Order.broker_reference_id == request.reference_id,
                    Order.mode == TradingMode.PAPER,
                )
            )
            instrument = await session.get(Instrument, order.instrument_id) if order else None
        if instrument is None:
            return None
        return FillConstraints(
            instrument_key=instrument.key,
            instrument_id=instrument.id,
            captured_at=as_of,
            lot_size=instrument.lot_size,
            tick_size=instrument.tick_size,
            instrument_type=instrument.instrument_type,
            expiry_date=instrument.expiry_date,
            option_type=instrument.option_type,
            strike_price=instrument.strike_price,
            underlying=instrument.underlying,
        )

    async def _fees_for_order(self, request, as_of):
        async with db_session.session_scope() as session:
            order = await session.scalar(
                sa.select(Order).where(
                    Order.broker_reference_id == request.reference_id,
                    Order.mode == TradingMode.PAPER,
                )
            )
        if order is not None and order.role == "ENTRY" and "fee_schedule" in order.request_payload:
            raw = order.request_payload["fee_schedule"]
            if raw is None:
                return None
            schedule = FeeSchedule.model_validate(raw)
            schedule.require_at(as_of)
            return schedule
        return await CostStore(self.clock).for_order(request, as_of)

    async def quote(self, instrument):
        quote = await self.source(instrument)
        if quote is None:
            return None
        if (
            quote.instrument != instrument
            or quote.observed_at.tzinfo is None
            or not timedelta(0)
            <= self.clock.utcnow() - quote.observed_at
            <= timedelta(seconds=self.settings.tick_staleness_seconds)
            or not quote.ltp.is_finite()
            or quote.ltp <= 0
            or quote.is_crossed
            or any(
                not level.price.is_finite() or level.price <= 0 or level.quantity <= 0
                for level in (*quote.bids, *quote.asks)
            )
        ):
            raise SafetyError("STALE_OR_INVALID_QUOTE")
        return quote

    async def _audit(self, session, order, event, result):
        source = await AuditService(self.clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=order.id, event_type=event, actor="paper_execution", mode=TradingMode.PAPER
            ),
            {
                "proposal_id": order.proposal_id,
                "order_id": order.id,
                "position_id": order.position_id,
                "result": {"execution_realism": "SIMULATED", **result},
            },
        )
        if event == "ORDER_SYNCHRONIZED" and (
            order.filled_quantity or order.status == OrderStatus.REJECTED
        ):
            rejected = order.status == OrderStatus.REJECTED
            if rejected:
                await observe_rejection(
                    session, order, source, self.clock, self.settings.paper_broker_rejection_limit
                )
            stop = (
                order.exit_reason in {ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP}
                and order.filled_quantity > 0
            )
            await enqueue(
                session,
                key=f"{order.id}:{order.status.value}:{order.filled_quantity}",
                event_type="STOP_LOSS_HIT"
                if stop
                else ("PAPER_ORDER_REJECTED" if rejected else "PAPER_ORDER_STATE"),
                severity="CRITICAL" if stop else ("WARNING" if rejected else "INFO"),
                message=(
                    f"PAPER SIMULATED {order.role} {order.trading_symbol}: {order.status.value}; "
                    f"filled={order.filled_quantity}/{order.quantity}; order={order.id}. "
                    "No live execution claim."
                ),
                clock=self.clock,
                source_event=source,
            )

    async def recover(self):
        self.ready = False
        self.gate.block("paper_recovery", "PAPER_RECONCILE_REQUIRED")
        await self.broker.connect()
        async with db_session.session_scope() as session:
            orders = list(
                (
                    await session.scalars(sa.select(Order).where(Order.mode == TradingMode.PAPER))
                ).all()
            )
        for order in orders:
            if order.status == OrderStatus.CREATED:
                async with db_session.session_scope() as session:
                    row = await session.get(Order, order.id, with_for_update=True)
                    transition(
                        session,
                        row,
                        OrderStatus.REJECTED,
                        self.clock.utcnow(),
                        source="recovery",
                        detail="Never submitted; new decision required",
                    )
                    row.rejection_reason = "RECOVERY_REQUIRES_NEW_DECISION"
            elif not order.status.is_terminal:
                await self.sync(order.id)
        known = {order.broker_reference_id for order in orders}
        if any(order.reference_id not in known for order in await self.broker.list_orders()):
            raise SafetyError("UNTRACKED_PAPER_BROKER_ORDER")
        await self._reconcile()
        self.gate.clear("paper_recovery")
        self.ready = True

    async def _account(self, context):
        return await self.portfolio_state(context.strategy.id, context.market.data_origin)

    async def portfolio_state(self, strategy_id, data_origin):
        account = self.broker.account
        as_of = self.clock.utcnow()
        async with db_session.session_scope() as session:
            positions = list(
                (
                    await session.scalars(
                        sa.select(Position).where(Position.mode == TradingMode.PAPER)
                    )
                ).all()
            )
            future_fill = await session.scalar(
                sa.select(Trade.id)
                .where(Trade.mode == TradingMode.PAPER, Trade.executed_at > as_of)
                .limit(1)
            )
            if future_fill or any(
                moment is not None and _utc(moment) > as_of
                for position in positions
                for moment in (position.opened_at, position.closed_at, position.marked_at)
            ):
                raise SafetyError("FUTURE_PAPER_ACCOUNT_STATE")
            peaks = await session.scalar(
                sa.select(sa.func.max(PortfolioSnapshot.equity)).where(
                    PortfolioSnapshot.mode == TradingMode.PAPER,
                    PortfolioSnapshot.ts <= as_of,
                )
            )
            session_start = (
                as_of.astimezone(IST)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .astimezone(UTC)
            )
            day_charges = await session.scalar(
                sa.select(sa.func.sum(Trade.brokerage + Trade.taxes + Trade.other_charges)).where(
                    Trade.mode == TradingMode.PAPER,
                    Trade.executed_at >= session_start,
                    Trade.executed_at <= as_of,
                )
            )
            day_trades = list(
                (
                    await session.scalars(
                        sa.select(Trade).where(
                            Trade.mode == TradingMode.PAPER,
                            Trade.executed_at >= session_start,
                            Trade.executed_at <= as_of,
                        )
                    )
                ).all()
            )
        open_positions = [position for position in positions if position.net_quantity]
        if any(
            not trade.cost_breakdown or "fifo" not in trade.cost_breakdown for trade in day_trades
        ):
            raise SafetyError("FIFO_HISTORY_RECONSTRUCTION_REQUIRED")
        realised = sum(
            (Decimal(trade.cost_breakdown["fifo"]["realised_delta"]) for trade in day_trades),
            Decimal(0),
        )
        exposures = []
        reserved = Decimal(0)
        for position in open_positions:
            async with db_session.session_scope() as session:
                instrument = await session.get(Instrument, position.instrument_id)
            exposures.append(
                Exposure(
                    instrument_id=position.instrument_id,
                    sector=instrument.sector,
                    underlying=instrument.underlying or instrument.trading_symbol,
                    notional=abs(position.net_quantity)
                    * (position.last_price or position.average_price),
                )
            )
            if position.stop_loss_price is None:
                raise SafetyError("POSITION_WITHOUT_STOP")
            reserved += await self._position_reserved_risk(position, instrument)
        return PortfolioState(
            observed_at=as_of,
            available_at=self.clock.utcnow(),
            source_id="paper-account-reconciliation",
            data_origin=data_origin,
            equity=max(Decimal(0), account.equity),
            peak_equity=max(account.starting_capital, account.equity, peaks or Decimal(0)),
            realised_day_pnl=realised - (day_charges or Decimal(0)),
            unrealised_day_pnl=account.unrealised_pnl(),
            available_margin=max(Decimal(0), account.available_margin),
            reserved_risk=reserved,
            exposures=tuple(exposures),
            open_and_pending_positions=len(open_positions),
            strategy_open_and_pending_positions=sum(
                position.strategy_id == strategy_id for position in open_positions
            ),
            strategy_id=strategy_id,
            entries_blocked=False,
            drawdown_disarmed=False,
        )

    async def _position_reserved_risk(self, position, instrument):
        if not instrument.is_option:
            return abs(position.net_quantity) * abs(
                position.average_price - position.stop_loss_price
            )
        async with db_session.session_scope() as session:
            proposal = await session.get(Proposal, position.proposal_id)
            entry = await session.get(Order, stable_id("ord", "entry:" + position.proposal_id))
            risk = await session.get(RiskDecision, entry.risk_decision_id) if entry else None
        if proposal is None or risk is None:
            raise SafetyError("OPTION_RISK_LINEAGE_UNAVAILABLE")
        planned = await approved_risk(proposal, risk)
        return abs(position.net_quantity) * (
            max(
                position.average_price,
                position.last_price or position.average_price,
                planned.defined_max_loss_per_unit or planned.entry,
            )
            + planned.risk_cost_per_unit
        )

    @storage_guard
    async def submit(self, proposal_id, *, replacement_chain=None):
        if not self.ready:
            raise SafetyError("RECOVERY_REQUIRED")
        identifier = stable_id("ord", "entry:" + proposal_id)
        async with db_session.session_scope() as session:
            existing = await session.get(Order, identifier)
            if existing is not None:
                return existing.id
            parent_order_id = await replacement_parent(session, proposal_id, replacement_chain)
            proposal = await session.get(Proposal, proposal_id)
            limits = await active_limits(session)
            risk = await session.scalar(
                sa.select(RiskDecision).where(
                    RiskDecision.proposal_id == proposal_id, RiskDecision.is_preflight.is_(False)
                )
            )
            slot = await session.get(PaperExecutionSlot, "PAPER")
        if slot is not None:
            raise SafetyError("PAPER_ACCOUNT_RESERVED")
        if (
            proposal is None
            or proposal.mode != TradingMode.PAPER
            or proposal.status != "RISK_APPROVED"
            or not proposal.approved_quantity
            or risk is None
            or not risk.approved_quantity
            or limits is None
        ):
            raise SafetyError("PERSISTED_APPROVAL_AND_ACTIVE_LIMITS_REQUIRED")
        context = DecisionContext.from_snapshot(proposal.context_snapshot)
        if context.limits != limits or limits.capital != self.broker.account.starting_capital:
            raise SafetyError("RISK_CONFIGURATION_MISMATCH")
        await require_entry_sources(proposal, limits, self.clock.utcnow())
        await require_enabled_strategy(proposal, self.settings, clock=self.clock)
        async with db_session.session_scope() as session:
            instrument = await session.get(Instrument, proposal.instrument_id)
        quote = await self.quote(
            InstrumentRef(instrument.trading_symbol, instrument.exchange, instrument.segment)
        )
        if (
            quote is None
            or not quote.bids
            or not quote.asks
            or quote.data_origin != context.market.data_origin
        ):
            raise SafetyError("QUOTE_DEPTH_OR_PROVENANCE_UNAVAILABLE")
        await self._circuit_preflight(proposal, quote)
        await self._reconcile()
        portfolio = await self._account(context)
        market = context.market.model_copy(
            update={
                "as_of": self.clock.utcnow(),
                "observed_at": quote.observed_at,
                "available_at": self.clock.utcnow(),
                "bids": tuple(
                    BookLevel(price=level.price, quantity=level.quantity) for level in quote.bids
                ),
                "asks": tuple(
                    BookLevel(price=level.price, quantity=level.quantity) for level in quote.asks
                ),
            }
        )
        risk_proposal = await approved_risk(proposal, risk)
        risk_proposal, tariff = await self._costed_risk(
            proposal, instrument, identifier, risk_proposal
        )
        await self.safety.observe(portfolio, market, limits)
        decision = evaluate(risk_proposal, portfolio, market, limits)
        if decision.rejection_code == "RISK_ENGINE_ERROR":
            await self.safety.trip_error(TradingMode.PAPER, market.data_origin)
        async with db_session.session_scope() as session:
            decision = apply_entry_policy(
                decision,
                await load_entry_history(session, market, self.settings, calendar=self.calendar),
            )
            preflight = RiskDecision(
                proposal_id=proposal.id,
                approved_quantity=decision.approved_quantity,
                approved=decision.approved,
                binding_rule=decision.binding_rule,
                rejection_code=decision.rejection_code,
                risk_amount=decision.risk_amount,
                risk_config_version=limits.version,
                mode=TradingMode.PAPER,
                is_preflight=True,
                evaluated_at=self.clock.utcnow(),
                rules_evaluated=[item.model_dump(mode="json") for item in decision.rules],
                state_snapshot={
                    "proposal": risk_proposal.model_dump(mode="json"),
                    "portfolio": portfolio.model_dump(mode="json"),
                    "market": market.model_dump(mode="json"),
                    "config": limits.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                },
            )
            session.add(preflight)
            await session.flush()
            if decision.approved:
                event_control = await self.safety.require_entries_in_session(
                    session, market, limits, strategy_id=proposal.strategy_id
                )
                session.add(PaperExecutionSlot(id="PAPER", proposal_id=proposal_id))
                order = Order(
                    id=identifier,
                    intent_id=stable_id("ent", proposal_id),
                    broker_reference_id=stable_id("p", identifier, 19),
                    instrument_id=instrument.id,
                    trading_symbol=instrument.trading_symbol,
                    exchange=instrument.exchange,
                    segment=instrument.segment,
                    product=proposal.product,
                    order_type=OrderType.LIMIT,
                    transaction_type=TransactionType.BUY
                    if proposal.direction == SignalDirection.LONG
                    else TransactionType.SELL,
                    quantity=decision.approved_quantity,
                    price=proposal.entry_price,
                    status=OrderStatus.CREATED,
                    proposal_id=proposal_id,
                    risk_decision_id=preflight.id,
                    strategy_id=proposal.strategy_id,
                    mode=TradingMode.PAPER,
                    execution_realism=ExecutionRealism.SIMULATED,
                    role="ENTRY",
                    parent_order_id=parent_order_id,
                    request_payload={
                        "data_origin": market.data_origin.value,
                        **(
                            {"event_control": event_control.model_dump(mode="json")}
                            if event_control.event_id
                            else {}
                        ),
                        "fee_schedule": tariff.model_dump(mode="json") if tariff else None,
                    },
                )
                session.add(order)
                await session.flush()
                await self._audit(
                    session,
                    order,
                    "ORDER_CREATED",
                    {
                        "quantity": order.quantity,
                        "fee_schedule": order.request_payload["fee_schedule"],
                    },
                )
        if not decision.approved:
            raise SafetyError(decision.rejection_code)
        await self._dispatch(identifier, market=market, limits=limits)
        return identifier

    async def _costed_risk(self, proposal, instrument, identifier, risk_proposal):
        request = OrderRequest(
            reference_id=stable_id("p", identifier, 19),
            trading_symbol=instrument.trading_symbol,
            exchange=instrument.exchange,
            segment=instrument.segment,
            product=proposal.product,
            order_type=OrderType.LIMIT,
            transaction_type=TransactionType.BUY
            if proposal.direction == SignalDirection.LONG
            else TransactionType.SELL,
            quantity=risk_proposal.quantity,
            price=risk_proposal.entry,
        )
        tariff = await CostStore(self.clock).for_order(request, self.clock.utcnow())
        if instrument.is_option and (tariff is None or tariff.charge_basis != "OPTION_PREMIUM"):
            raise SafetyError("OPTION_FEE_SOURCE_UNAVAILABLE")
        if tariff is not None:
            risk_proposal = risk_proposal.model_copy(
                update={
                    "risk_cost_per_unit": risk_cost_reserve(
                        tariff,
                        risk_proposal,
                        request.transaction_type,
                        as_of=self.clock.utcnow(),
                    ),
                }
            )
        return risk_proposal, tariff

    async def _circuit_preflight(self, proposal, quote, *, order_id=None):
        circuit = circuit_status(
            quote,
            [
                proposal.entry_price,
                quote.ltp,
                *(level.price for level in (*quote.bids, *quote.asks)),
            ],
        )
        await AuditService(self.clock).append(
            AuditIdentity(
                chain_id=new_id("cir"),
                event_type="ENTRY_CIRCUIT_PREFLIGHT",
                actor="paper_execution",
                mode=TradingMode.PAPER,
            ),
            {
                "proposal_id": proposal.id,
                "instrument_id": proposal.instrument_id,
                "order_id": order_id,
                "result": {
                    "status": circuit,
                    "stage": "DISPATCH" if order_id else "PREFLIGHT",
                    "data_origin": quote.data_origin.value,
                    "observed_at": quote.observed_at.isoformat(),
                    "lower": str(quote.lower_circuit) if quote.lower_circuit is not None else None,
                    "upper": str(quote.upper_circuit) if quote.upper_circuit is not None else None,
                    "entry": str(proposal.entry_price),
                },
            },
        )
        if circuit not in {"AVAILABLE", "UNAVAILABLE"}:
            raise SafetyError(circuit)
        if circuit == "UNAVAILABLE" and quote.data_origin == DataOrigin.LIVE:
            raise SafetyError("LIVE_SOURCE_CIRCUIT_BAND_UNAVAILABLE")

    async def _dispatch_circuit(self, identifier):
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
            if order.role != "ENTRY" or order.status != OrderStatus.CREATED:
                return
            proposal = await session.get(Proposal, order.proposal_id)
        try:
            quote = await self.quote(
                InstrumentRef(order.trading_symbol, order.exchange, order.segment)
            )
            if quote is None or quote.data_origin.value != order.request_payload.get("data_origin"):
                raise SafetyError("DISPATCH_QUOTE_OR_PROVENANCE_UNAVAILABLE")
            await self._circuit_preflight(proposal, quote, order_id=identifier)
        except SafetyError as error:
            async with db_session.session_scope() as session:
                current = await session.get(Order, identifier, with_for_update=True)
                if current.status != OrderStatus.CREATED:
                    raise SafetyError("CONCURRENT_DISPATCH") from error
                transition(
                    session,
                    current,
                    OrderStatus.REJECTED,
                    self.clock.utcnow(),
                    source="local",
                    detail=error.message,
                )
                current.rejection_reason = error.message
                await self._audit(
                    session, current, "ENTRY_DISPATCH_BLOCKED", {"reason": error.message}
                )
            await self._reconcile()
            raise

    async def _dispatch(self, identifier, *, market=None, limits=None):
        if self.settings.trading_mode != TradingMode.PAPER:
            raise SafetyError("PAPER_MODE_REQUIRED")
        await self._dispatch_entry_policy(identifier, market)
        await self._dispatch_circuit(identifier)
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier, with_for_update=True)
            if order.status != OrderStatus.CREATED:
                return
            if order.role == "ENTRY":
                proposal = await session.get(Proposal, order.proposal_id)
                await require_entry_sources(proposal, limits, self.clock.utcnow())
                await require_enabled_strategy(proposal, self.settings, clock=self.clock)
                if order.product != proposal.product:
                    raise SafetyError("ORDER_PRODUCT_CONTRACT_MISMATCH")
                if (
                    order.price != proposal.entry_price
                    or order.quantity != proposal.approved_quantity
                ):
                    raise SafetyError("DISPATCH_ORDER_APPROVAL_MISMATCH")
                event_control = await self.safety.require_entries_in_session(
                    session, market, limits, strategy_id=proposal.strategy_id
                )
                if event_control.event_id:
                    order.request_payload = {
                        **order.request_payload,
                        "event_control": event_control.model_dump(mode="json"),
                    }
            changed = await session.execute(
                sa.update(Order)
                .where(Order.id == identifier, Order.status == OrderStatus.CREATED)
                .values(status=OrderStatus.SUBMITTED)
            )
            if changed.rowcount != 1:
                raise SafetyError("CONCURRENT_DISPATCH")
            order.status = OrderStatus.CREATED
            transition(session, order, OrderStatus.SUBMITTED, self.clock.utcnow(), source="local")
            order.submitted_at = self.clock.utcnow()
            request = OrderRequest(
                trading_symbol=order.trading_symbol,
                exchange=order.exchange,
                segment=order.segment,
                product=order.product,
                order_type=order.order_type,
                transaction_type=order.transaction_type,
                quantity=order.quantity,
                reference_id=order.broker_reference_id,
                price=order.price,
            )
            await self._audit(
                session, order, "ORDER_SUBMITTED", {"reference_id": request.reference_id}
            )
        try:
            await asyncio.wait_for(self.broker.place_order(request), timeout=10)
        except Exception:
            await self._unknown(identifier)
            self.broker = PaperBrokerProvider(
                self.settings,
                clock=self.clock,
                quote_source=self.quote,
                state_store=PaperStateStore(),
                fill_config=self.fill_config,
                cost_source=self._fees_for_order,
                constraint_source=self._constraints_for_order,
            )
            await self.broker.connect()
        await self.sync(identifier)

    async def _dispatch_entry_policy(self, identifier, market):
        rejected = None
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
            if order.role != "ENTRY" or order.status != OrderStatus.CREATED:
                return
            if market is None:
                raise SafetyError("ENTRY_MARKET_CONTEXT_UNAVAILABLE")
            evidence = await load_entry_history(
                session,
                market.model_copy(update={"as_of": self.clock.utcnow()}),
                self.settings,
                exclude_order_id=identifier,
                calendar=self.calendar,
            )
            checks = entry_rules(evidence)
            rejected = next((check.rejection_code for check in checks if not check.passed), None)
            if (
                order.instrument_id != market.instrument_id
                or (order.request_payload or {}).get("data_origin") != market.data_origin.value
            ):
                rejected = "ENTRY_ORDER_CONTEXT_MISMATCH"
            await self._audit(
                session,
                order,
                "ENTRY_POLICY_CHECK",
                {
                    "entry_policy": evidence.model_dump(mode="json"),
                    "rejection_code": rejected,
                    "rules": [check.model_dump(mode="json") for check in checks],
                },
            )
        if rejected:
            raise SafetyError(rejected)

    async def _unknown(self, identifier):
        self.gate.block("paper_unknown", "PAPER_RECONCILE_REQUIRED")
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier, with_for_update=True)
            if not order.status.is_terminal:
                transition(
                    session, order, OrderStatus.UNKNOWN, self.clock.utcnow(), source="recovery"
                )
                await self._audit(session, order, "ORDER_UNKNOWN", {"retry": "LOOKUP_ONLY"})

    @storage_guard
    async def sync(self, identifier):
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
        try:
            remote = await self.broker.get_order_by_reference(
                order.broker_reference_id, order.segment
            )
        except Exception:
            await self._unknown(identifier)
            raise
        if remote is None:
            await self._unknown(identifier)
            raise SafetyError("ORDER_UNKNOWN_NO_BLIND_RESUBMISSION")
        fills = await self.broker.list_trades(remote.broker_order_id, order.segment)
        if (
            remote.quantity != order.quantity
            or remote.trading_symbol != order.trading_symbol
            or remote.transaction_type != order.transaction_type
            or remote.product != order.product
            or remote.segment != order.segment
            or remote.exchange != order.exchange
            or sum(fill.quantity for fill in fills) != remote.filled_quantity
        ):
            await self._unknown(identifier)
            raise SafetyError("BROKER_ORDER_OR_FILL_MISMATCH")
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier, with_for_update=True)
            proposal = await session.get(Proposal, order.proposal_id)
            position = await self._apply_fills(session, order, proposal, remote, fills)
            if remote.filled_quantity < order.filled_quantity:
                raise SafetyError("FILL_REGRESSION")
            transition(session, order, remote.status, self.clock.utcnow(), source="poll")
            order.broker_order_id = remote.broker_order_id
            order.acknowledged_at = order.acknowledged_at or self.clock.utcnow()
            order.filled_quantity, order.average_fill_price = (
                remote.filled_quantity,
                remote.average_fill_price,
            )
            order.rejection_reason = remote.rejection_reason
            if (
                position
                and position.net_quantity == 0
                and order.role == "EXIT"
                and position.state != PositionState.CLOSED
            ):
                position.state, position.closed_at = PositionState.CLOSED, self.clock.utcnow()
                position.exit_reason = order.exit_reason
                journal_id = stable_id("jrn", position.id)
                if not await session.get(JournalEntry, journal_id):
                    entry = await session.get(Order, stable_id("ord", "entry:" + proposal.id))
                    trades = list(
                        (
                            await session.scalars(
                                sa.select(Trade).where(Trade.position_id == position.id)
                            )
                        ).all()
                    )
                    costs_known = bool(trades) and all(
                        trade.cost_breakdown and trade.cost_breakdown.get("status") == "ESTIMATED"
                        for trade in trades
                    )
                    exit_ids, actual_exit = await self._exit_summary(session, position.id, trades)
                    session.add(
                        JournalEntry(
                            id=journal_id,
                            proposal_id=proposal.id,
                            risk_decision_id=entry.risk_decision_id,
                            position_id=position.id,
                            entry_order_id=entry.id,
                            exit_order_ids=sorted(exit_ids),
                            audit_chain_id=order.id,
                            instrument_id=position.instrument_id,
                            trading_symbol=position.trading_symbol,
                            strategy_id=proposal.strategy_id,
                            strategy_version=proposal.strategy_version,
                            regime_at_entry=proposal.context_snapshot["regime_snapshot"]["label"],
                            direction=proposal.direction,
                            planned_entry=proposal.entry_price,
                            actual_entry=entry.average_fill_price,
                            planned_stop=proposal.stop_loss,
                            planned_target=proposal.target_price,
                            quantity=entry.filled_quantity,
                            holding_period_seconds=int(
                                (
                                    _utc(position.closed_at) - _utc(position.opened_at)
                                ).total_seconds()
                            ),
                            invalidation_fired=order.exit_reason == ExitReason.INVALIDATION,
                            entry_slippage=(entry.average_fill_price - proposal.entry_price)
                            * entry.transaction_type.sign,
                            exit_slippage=self._exit_slippage(trades, exit_ids),
                            ai_thesis=proposal.thesis if proposal.origin == "LLM" else None,
                            ai_confidence=proposal.confidence if proposal.origin == "LLM" else None,
                            opened_at=position.opened_at,
                            actual_exit=actual_exit,
                            closed_at=position.closed_at,
                            exit_reason=order.exit_reason,
                            gross_pnl=position.realised_pnl,
                            charges=position.total_charges if costs_known else None,
                            **await self._journal_outcome(session, entry, position, costs_known),
                            mode=TradingMode.PAPER,
                            indicator_snapshot={
                                "decision_context": proposal.context_snapshot,
                                "cost_status": "ESTIMATED" if costs_known else "UNAVAILABLE",
                            },
                        )
                    )
                    await bind_journal(session, journal_id, self.clock)
            await self._audit(
                session,
                order,
                "ORDER_SYNCHRONIZED",
                {
                    "status": remote.status.value,
                    "filled_quantity": remote.filled_quantity,
                    "pnl_basis": "GROSS_WITH_SEPARATE_CHARGE_ESTIMATES",
                },
            )
        if position and position.net_quantity:
            self.gate.block("paper_protection", "PAPER_POSITION_REQUIRES_MONITORING")
            emit_critical(
                event_id=order.id,
                event_type="PAPER_POSITION_MONITORING_REQUIRED",
                message="PAPER stop/target recorded; continuous supervision is not yet verified.",
                condition_key="paper_protection",
                occurred_at=self.clock.utcnow(),
            )
        await self._reconcile()
        if position and not position.net_quantity:
            context = DecisionContext.from_snapshot(proposal.context_snapshot)
            async with db_session.session_scope() as session:
                limits = await active_limits(session)
            if limits is None:
                raise SafetyError("RISK_CONFIGURATION_UNAVAILABLE")
            account = await self._account(context)
            await self.safety.observe(
                account, context.market.model_copy(update={"as_of": self.clock.utcnow()}), limits
            )
            await self._snapshot(account, context)

    async def _journal_outcome(self, session, entry, position, costs_known):
        approved = await session.get(RiskDecision, entry.risk_decision_id)
        net = position.realised_pnl - position.total_charges if costs_known else None
        return {
            "risk_amount": approved.risk_amount if approved else None,
            "net_pnl": net,
            "r_multiple": net / approved.risk_amount
            if net is not None and approved and approved.risk_amount
            else None,
            "outcome": None if net is None else "WIN" if net > 0 else "LOSS" if net < 0 else "FLAT",
        }

    @staticmethod
    def _exit_slippage(trades, exit_ids):
        closed_fills = [trade for trade in trades if trade.order_id in exit_ids]
        if not closed_fills or any(trade.slippage is None for trade in closed_fills):
            return None
        return sum(trade.slippage * trade.quantity for trade in closed_fills) / sum(
            trade.quantity for trade in closed_fills
        )

    async def _exit_summary(self, session, position_id, trades):
        identifiers = set(
            (
                await session.scalars(
                    sa.select(Order.id).where(
                        Order.position_id == position_id,
                        Order.role == "EXIT",
                    )
                )
            ).all()
        )
        fills = [trade for trade in trades if trade.order_id in identifiers]
        average = sum((trade.price * trade.quantity for trade in fills), Decimal(0)) / sum(
            trade.quantity for trade in fills
        )
        return identifiers, average

    async def _apply_fills(self, session, order, proposal, remote, fills):
        position_id = stable_id("pos", proposal.id)
        position = await session.get(Position, position_id, with_for_update=True)
        for fill in fills:
            if (
                not fill.exchange_trade_id
                or fill.executed_at is None
                or fill.price <= 0
                or fill.quantity <= 0
            ):
                raise SafetyError("INVALID_FILL")
            trade_id = stable_id("trd", fill.exchange_trade_id)
            stored = await session.get(Trade, trade_id)
            if stored:
                if (
                    stored.quantity != fill.quantity
                    or stored.price != fill.price
                    or stored.order_id != order.id
                ):
                    raise SafetyError("FILL_IDENTITY_CHANGED")
                continue
            if position is None:
                if order.role != "ENTRY":
                    raise SafetyError("EXIT_WITHOUT_POSITION")
                position = Position(
                    id=position_id,
                    instrument_id=order.instrument_id,
                    trading_symbol=order.trading_symbol,
                    segment=order.segment,
                    product=order.product,
                    side=PositionSide.LONG
                    if order.transaction_type == TransactionType.BUY
                    else PositionSide.SHORT,
                    state=PositionState.OPEN,
                    net_quantity=0,
                    average_price=0,
                    bought_quantity=0,
                    sold_quantity=0,
                    realised_pnl=0,
                    unrealised_pnl=0,
                    total_charges=0,
                    stop_loss_price=proposal.stop_loss,
                    target_price=proposal.target_price,
                    is_protected=False,
                    strategy_id=order.strategy_id,
                    proposal_id=proposal.id,
                    regime_at_entry=proposal.regime,
                    opened_at=fill.executed_at.astimezone(UTC),
                    mode=TradingMode.PAPER,
                    execution_realism=ExecutionRealism.SIMULATED,
                )
                session.add(position)
            signed = fill.quantity * order.transaction_type.sign
            if order.role == "EXIT":
                if position.net_quantity * signed >= 0 or fill.quantity > abs(
                    position.net_quantity
                ):
                    raise SafetyError("EXIT_WOULD_REVERSE_POSITION")
            accounting = await self._fifo_fill(session, position, trade_id, signed, fill.price)
            position.net_quantity += signed
            position.bought_quantity += fill.quantity if signed > 0 else 0
            position.sold_quantity += fill.quantity if signed < 0 else 0
            position.last_price, position.marked_at = fill.price, fill.executed_at.astimezone(UTC)
            position.unrealised_pnl = (fill.price - position.average_price) * position.net_quantity
            order.position_id = position.id
            breakdown = dict(fill.raw.get("costs", {"status": "UNAVAILABLE", "pnl_basis": "GROSS"}))
            breakdown["instrument_constraints"] = fill.raw.get("instrument_constraints")
            breakdown["fifo"] = accounting
            brokerage = taxes = other = Decimal(0)
            if breakdown.get("status") == "ESTIMATED":
                components = {key: Decimal(value) for key, value in breakdown["components"].items()}
                if any(not value.is_finite() or value < 0 for value in components.values()):
                    raise SafetyError("INVALID_FILL_CHARGES")
                brokerage = components["brokerage"]
                taxes = components["stt"] + components["stamp"] + components["gst"]
                other = components["exchange"] + components["sebi"] + components["ipft"]
                if brokerage + taxes + other != components["total"]:
                    raise SafetyError("FILL_CHARGES_DO_NOT_SUM")
                position.total_charges += components["total"]
            session.add(
                Trade(
                    id=trade_id,
                    order_id=order.id,
                    instrument_id=order.instrument_id,
                    position_id=position.id,
                    broker_order_id=remote.broker_order_id,
                    transaction_type=order.transaction_type,
                    quantity=fill.quantity,
                    price=fill.price,
                    executed_at=fill.executed_at.astimezone(UTC),
                    intended_price=order.price,
                    slippage=(fill.price - order.price) * order.transaction_type.sign
                    if order.price
                    else None,
                    brokerage=brokerage,
                    taxes=taxes,
                    other_charges=other,
                    cost_breakdown=breakdown,
                    mode=TradingMode.PAPER,
                    execution_realism=ExecutionRealism.SIMULATED,
                )
            )
            await session.flush()
            await record_fill(session, trade_id, order.trading_symbol, self.clock)
        return position

    async def _fifo_fill(self, session, position, trade_id, signed, price):
        history = list(
            (await session.scalars(sa.select(Trade).where(Trade.position_id == position.id))).all()
        )
        lots, realised = replay_fills(history)
        if sum(lot.quantity for lot in lots) != position.net_quantity:
            raise SafetyError("FIFO_POSITION_QUANTITY_MISMATCH")
        result = match_fill(lots, Lot(signed, price, trade_id))
        total = quantize_money(realised + result.realised)
        delta = total - position.realised_pnl
        position.average_price = result.average_price
        position.realised_pnl = total
        return {
            "sequence": len(history) + 1,
            "realised_delta": str(delta),
            "matches": [
                {
                    "entry_id": match.entry_id,
                    "exit_id": match.exit_id,
                    "quantity": match.quantity,
                    "entry_price": str(match.entry_price),
                    "exit_price": str(match.exit_price),
                    "gross_pnl": str(match.gross_pnl),
                }
                for match in result.matches
            ],
            "open_lots": [
                {"source_id": lot.source_id, "quantity": lot.quantity, "price": str(lot.price)}
                for lot in result.lots
            ],
        }

    @storage_guard
    async def exit(self, position_id, reason=ExitReason.MANUAL, *, retry_terminal=False):
        reason = ExitReason(reason)
        async with db_session.session_scope() as session:
            position = await session.get(Position, position_id, with_for_update=True)
            if position is None or position.mode != TradingMode.PAPER:
                raise SafetyError("PAPER_POSITION_REQUIRED")
            entry = await session.get(Order, stable_id("ord", "entry:" + position.proposal_id))
            existing = await self._latest_exit(session, position.id)
        if existing:
            if (
                not retry_terminal
                or existing.status not in {OrderStatus.CANCELLED, OrderStatus.REJECTED}
                or not position.net_quantity
            ):
                return existing.id
            if existing.submitted_at is not None:
                await self.sync(existing.id)
            await self._reconcile()
        if not entry.status.is_terminal:
            await self.cancel(entry.id)
        async with db_session.session_scope() as session:
            position = await session.get(Position, position_id, with_for_update=True)
            latest = await self._latest_exit(session, position.id)
            if (latest.id if latest else None) != (existing.id if existing else None):
                raise SafetyError("CONCURRENT_EXIT_INTENT")
            if latest and latest.status not in {OrderStatus.CANCELLED, OrderStatus.REJECTED}:
                return latest.id
            if not position.net_quantity:
                raise SafetyError("POSITION_ALREADY_FLAT")
            attempt = (
                int((latest.request_payload or {}).get("exit_attempt", 0)) + 1 if latest else 0
            )
            suffix = position.id if not attempt else f"{position.id}:{attempt}"
            order = Order(
                id=stable_id("ord", "exit:" + suffix),
                intent_id=stable_id("ext", suffix),
                broker_reference_id=stable_id("x", suffix, 19),
                instrument_id=position.instrument_id,
                trading_symbol=position.trading_symbol,
                exchange=entry.exchange,
                segment=position.segment,
                product=position.product,
                order_type=OrderType.MARKET,
                transaction_type=TransactionType.SELL
                if position.net_quantity > 0
                else TransactionType.BUY,
                quantity=abs(position.net_quantity),
                status=OrderStatus.CREATED,
                role="EXIT",
                exit_reason=reason,
                proposal_id=position.proposal_id,
                risk_decision_id=entry.risk_decision_id,
                strategy_id=position.strategy_id,
                position_id=position.id,
                mode=TradingMode.PAPER,
                execution_realism=ExecutionRealism.SIMULATED,
                request_payload={
                    "exit_attempt": attempt,
                    "supersedes": latest.id if latest else None,
                },
            )
            session.add(order)
            await session.flush()
            await self._audit(
                session,
                order,
                "EXIT_CREATED",
                {"reason": reason.value, "supersedes": latest.id if latest else None},
            )
        await self._dispatch(order.id)
        return order.id

    async def _latest_exit(self, session, position_id):
        exits = list(
            (
                await session.scalars(
                    sa.select(Order).where(
                        Order.position_id == position_id,
                        Order.mode == TradingMode.PAPER,
                        Order.role == "EXIT",
                    )
                )
            ).all()
        )
        attempts = [int((order.request_payload or {}).get("exit_attempt", 0)) for order in exits]
        if len(set(attempts)) != len(attempts):
            raise SafetyError("AMBIGUOUS_EXIT_INTENT_HISTORY")
        return (
            max(exits, key=lambda order: int((order.request_payload or {}).get("exit_attempt", 0)))
            if exits
            else None
        )

    async def cancel(self, identifier):
        async with db_session.session_scope() as session:
            order = await session.get(Order, identifier)
        if order.status.is_terminal:
            return
        if not order.broker_order_id:
            await self.sync(identifier)
            async with db_session.session_scope() as session:
                order = await session.get(Order, identifier)
            if order.status.is_terminal:
                return
        try:
            await asyncio.wait_for(
                self.broker.cancel_order(order.broker_order_id, order.segment), timeout=10
            )
        except Exception:
            await self._unknown(identifier)
        await self.sync(identifier)

    @storage_guard
    async def verify_protection(self, quote=None):
        async with db_session.session_scope() as session:
            positions = list(
                (
                    await session.scalars(
                        sa.select(Position).where(
                            Position.mode == TradingMode.PAPER, Position.net_quantity != 0
                        )
                    )
                ).all()
            )
        for position in positions:
            async with db_session.session_scope() as session:
                proposal = (
                    await session.get(Proposal, position.proposal_id)
                    if position.proposal_id
                    else None
                )
                entry = (
                    await session.get(Order, stable_id("ord", "entry:" + position.proposal_id))
                    if position.proposal_id
                    else None
                )
                risk = await session.get(RiskDecision, entry.risk_decision_id) if entry else None
                fills = list(
                    (
                        await session.scalars(
                            sa.select(Trade).where(Trade.position_id == position.id)
                        )
                    ).all()
                )
            quantity = sum(
                fill.quantity if fill.transaction_type == TransactionType.BUY else -fill.quantity
                for fill in fills
            )
            reason = None
            if proposal is None or entry is None or risk is None:
                reason = "PROTECTION_LINEAGE_UNAVAILABLE"
            elif quantity != position.net_quantity:
                reason = "PROTECTION_QUANTITY_MISMATCH"
            elif (
                position.stop_loss_price is None
                or position.stop_loss_price != proposal.stop_loss
                or position.target_price != proposal.target_price
            ):
                reason = "PROTECTION_MISSING_OR_CHANGED"
            if reason is None or reason == "PROTECTION_MISSING_OR_CHANGED":
                try:
                    await approved_risk(proposal, risk)
                except (ValueError, SafetyError, TypeError, KeyError):
                    reason = "PROTECTION_APPROVAL_INTEGRITY_FAILURE"
            if reason is None:
                if quote is not None:
                    async with db_session.session_scope() as session:
                        await AuditService(self.clock).append_in_session(
                            session,
                            AuditIdentity(
                                chain_id=position.id,
                                event_type="PROTECTION_CHECKED",
                                actor="paper_watchdog",
                                mode=TradingMode.PAPER,
                            ),
                            {
                                "position_id": position.id,
                                "proposal_id": position.proposal_id,
                                "result": {
                                    "quantity": position.net_quantity,
                                    "stop": str(position.stop_loss_price),
                                    "target": str(position.target_price),
                                    "quote_observed_at": quote.observed_at.isoformat(),
                                    "data_origin": quote.data_origin.value,
                                    "valid_until": min(
                                        quote.observed_at
                                        + timedelta(seconds=self.settings.tick_staleness_seconds),
                                        self.clock.utcnow()
                                        + timedelta(seconds=3 * self.settings.paper_cycle_seconds),
                                    ).isoformat(),
                                    "protection_kind": "PAPER_SOFTWARE_MONITOR",
                                },
                            },
                        )
                continue
            self.gate.block("paper_protection", reason)
            async with db_session.session_scope() as session:
                await AuditService(self.clock).append_in_session(
                    session,
                    AuditIdentity(
                        chain_id=position.id,
                        event_type="PROTECTION_FAILURE",
                        actor="paper_watchdog",
                        mode=TradingMode.PAPER,
                    ),
                    {
                        "position_id": position.id,
                        "proposal_id": position.proposal_id,
                        "result": {
                            "reason": reason,
                            "fill_quantity": quantity,
                            "position_quantity": position.net_quantity,
                        },
                    },
                )
            if proposal is not None:
                origin = proposal.context_snapshot["market"]["data_origin"]
                await self.safety.trip_error(TradingMode.PAPER, origin)
            if reason == "PROTECTION_MISSING_OR_CHANGED":
                await self._reconcile()
                await self.exit(position.id, ExitReason.EMERGENCY)
            raise SafetyError(reason)

    @storage_guard
    async def monitor_once(self):
        if not self.ready:
            raise SafetyError("RECOVERY_REQUIRED")
        await self.verify_protection()
        async with db_session.session_scope() as session:
            slot = await session.get(PaperExecutionSlot, "PAPER")
            if slot is None:
                return
            proposal = await session.get(Proposal, slot.proposal_id)
            limits = await active_limits(session)
            instrument = await session.get(Instrument, proposal.instrument_id)
        if limits is None:
            raise SafetyError("RISK_CONFIGURATION_UNAVAILABLE")
        context = DecisionContext.from_snapshot(proposal.context_snapshot)
        quote = await self.quote(
            InstrumentRef(instrument.trading_symbol, instrument.exchange, instrument.segment)
        )
        if (
            quote is None
            or not quote.bids
            or not quote.asks
            or quote.data_origin != context.market.data_origin
        ):
            self.gate.block("paper_market", "PAPER_MONITOR_DATA_UNAVAILABLE")
            raise SafetyError("PAPER_MONITOR_DATA_UNAVAILABLE")
        await self.broker.settle_open_orders()
        async with db_session.session_scope() as session:
            pending = list(
                (
                    await session.scalars(
                        sa.select(Order).where(
                            Order.mode == TradingMode.PAPER,
                            Order.proposal_id == proposal.id,
                            Order.status.not_in(
                                [OrderStatus.EXECUTED, OrderStatus.CANCELLED, OrderStatus.REJECTED]
                            ),
                        )
                    )
                ).all()
            )
        for order in pending:
            await self.sync(order.id)
        await self._reconcile()
        await self.verify_protection(quote)
        position_id = stable_id("pos", proposal.id)
        self.broker.account.mark(
            instrument.trading_symbol, instrument.segment, proposal.product, quote.ltp
        )
        await self.broker.persist()
        async with db_session.session_scope() as session:
            position = await session.get(Position, position_id, with_for_update=True)
            if position and position.net_quantity:
                position.last_price, position.marked_at = (
                    quote.ltp,
                    quote.observed_at.astimezone(UTC),
                )
                history = list(
                    (
                        await session.scalars(
                            sa.select(Trade).where(Trade.position_id == position.id)
                        )
                    ).all()
                )
                lots, _realised = replay_fills(history)
                if sum(lot.quantity for lot in lots) != position.net_quantity:
                    raise SafetyError("FIFO_POSITION_QUANTITY_MISMATCH")
                position.unrealised_pnl = quantize_money(
                    sum((lot.quantity * (quote.ltp - lot.price) for lot in lots), Decimal(0))
                )
        await self._observe_account(context, quote, limits, position_id)
        self.gate.clear("paper_market")
        if position and position.net_quantity:
            price = quote.best_bid if position.net_quantity > 0 else quote.best_ask
            if price is None:
                raise SafetyError("EXIT_DEPTH_UNAVAILABLE")
            sign = 1 if position.net_quantity > 0 else -1
            if sign * (price - position.stop_loss_price) <= 0:
                await self.exit(position.id, ExitReason.STOP_LOSS)
            elif position.target_price and sign * (price - position.target_price) >= 0:
                await self.exit(position.id, ExitReason.TARGET)
        await self._observe_account(context, quote, limits, position_id)

    async def refresh_account(self, proposal, quote):
        context = DecisionContext.from_snapshot(proposal.context_snapshot)
        async with db_session.session_scope() as session:
            limits = await active_limits(session)
        if limits is None:
            raise SafetyError("RISK_CONFIGURATION_UNAVAILABLE")
        await self._observe_account(context, quote, limits, stable_id("pos", proposal.id))

    async def _observe_account(self, context, quote, limits, source_id):
        account = await self._account(context)
        market = context.market.model_copy(
            update={
                "as_of": self.clock.utcnow(),
                "source_id": source_id,
                "observed_at": quote.observed_at,
                "available_at": self.clock.utcnow(),
                "bids": tuple(
                    BookLevel(price=level.price, quantity=level.quantity) for level in quote.bids
                ),
                "asks": tuple(
                    BookLevel(price=level.price, quantity=level.quantity) for level in quote.asks
                ),
            }
        )
        await self.safety.observe(account, market, limits)
        await self._snapshot(account, context)

    async def _snapshot(self, account, context):
        async with db_session.session_scope() as session:
            session.add(
                PortfolioSnapshot(
                    ts=self.clock.utcnow(),
                    mode=TradingMode.PAPER,
                    equity=account.equity,
                    cash=self.broker.account.cash,
                    realised_pnl=self.broker.account.realised_pnl,
                    unrealised_pnl=self.broker.account.unrealised_pnl(),
                    charges=self.broker.account.charges_paid,
                    gross_exposure=sum((item.notional for item in account.exposures), Decimal(0)),
                    available_margin=account.available_margin,
                    used_margin=self.broker.account.used_margin,
                    open_positions=account.open_and_pending_positions,
                    positions=[
                        {
                            "data_origin": context.market.data_origin.value,
                            "execution_realism": "SIMULATED",
                            "pnl_basis": "GROSS_WITH_SEPARATE_CHARGE_ESTIMATES",
                        }
                    ],
                )
            )

    async def _reconcile(self):
        incidents = PaperIncidents(self.clock)
        try:
            await self._reconcile_state()
        except Exception:
            self.gate.block("paper_reconciliation", "PAPER_POSITION_OR_ORDER_DISCREPANCY")
            await incidents.observe("RECONCILIATION_DISCREPANCY", active=True)
            raise
        await incidents.observe("RECONCILIATION_DISCREPANCY", active=False)

    async def _reconcile_state(self):
        remote = {
            (item.exchange, item.trading_symbol, item.segment, item.product): item.net_quantity
            for item in await self.broker.get_positions()
            if item.net_quantity
        }
        async with db_session.session_scope() as session:
            positions = list(
                (
                    await session.scalars(
                        sa.select(Position).where(
                            Position.mode == TradingMode.PAPER, Position.net_quantity != 0
                        )
                    )
                ).all()
            )
            local = {}
            for position in positions:
                instrument = await session.get(Instrument, position.instrument_id)
                if instrument is None:
                    raise SafetyError("POSITION_INSTRUMENT_UNAVAILABLE")
                key = (
                    instrument.exchange,
                    position.trading_symbol,
                    position.segment,
                    position.product,
                )
                local[key] = local.get(key, 0) + position.net_quantity
            pending = await session.scalar(
                sa.select(sa.func.count())
                .select_from(Order)
                .where(
                    Order.mode == TradingMode.PAPER,
                    Order.status.not_in(
                        [OrderStatus.EXECUTED, OrderStatus.CANCELLED, OrderStatus.REJECTED]
                    ),
                )
            )
            unknown = await session.scalar(
                sa.select(sa.func.count())
                .select_from(Order)
                .where(Order.mode == TradingMode.PAPER, Order.status == OrderStatus.UNKNOWN)
            )
            if local != remote or unknown or not await self._accounting_matches(session, positions):
                self.gate.block("paper_reconciliation", "PAPER_POSITION_OR_ORDER_DISCREPANCY")
                raise SafetyError("PAPER_RECONCILE_REQUIRED")
            if not positions and not pending:
                await session.execute(
                    sa.delete(PaperExecutionSlot).where(PaperExecutionSlot.id == "PAPER")
                )
            system = await session.get(SystemState, SINGLETON_ID)
            if system is None:
                system = SystemState(id=SINGLETON_ID, mode=TradingMode.PAPER)
                session.add(system)
            if system.mode != TradingMode.PAPER or system.open_discrepancies:
                raise SafetyError("UNRESOLVED_SYSTEM_RECONCILIATION")
            system.last_reconciliation_at = self.clock.utcnow()
        self.gate.clear("paper_reconciliation")
        self.gate.clear("paper_unknown")
        if not positions:
            self.gate.clear("paper_protection")
        else:
            self.gate.block("paper_protection", "PAPER_POSITION_REQUIRES_MONITORING")

    async def _accounting_matches(self, session, positions):
        gross = await session.scalar(
            sa.select(sa.func.sum(Position.realised_pnl)).where(
                Position.mode == TradingMode.PAPER,
            )
        ) or Decimal(0)
        charges = await session.scalar(
            sa.select(
                sa.func.sum(
                    Trade.brokerage + Trade.taxes + Trade.other_charges,
                )
            ).where(Trade.mode == TradingMode.PAPER)
        ) or Decimal(0)
        account = self.broker.account
        if (
            gross != account.realised_pnl
            or charges != account.charges_paid
            or account.cash != account.starting_capital + gross - charges
        ):
            return False
        for position in positions:
            matching = [
                row
                for row in account.open_positions()
                if (row.trading_symbol, row.segment, row.product)
                == (position.trading_symbol, position.segment, position.product)
            ]
            history = list(
                (
                    await session.scalars(sa.select(Trade).where(Trade.position_id == position.id))
                ).all()
            )
            lots, _realised = replay_fills(history)
            quantum = Decimal(1).scaleb(-Position.__table__.c.average_price.type.scale)
            if len(matching) != 1 or (
                [(lot.quantity, lot.price) for lot in lots]
                != [(lot.quantity, lot.price) for lot in matching[0].fifo_lots]
                or abs(matching[0].average_price - position.average_price) > quantum / 2
            ):
                return False
        return True
