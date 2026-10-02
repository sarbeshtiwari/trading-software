"""Individually testable numerical veto rules, evaluated without short-circuiting."""

from datetime import timedelta
from decimal import Decimal

from app.core.data_origin import DataOrigin
from app.core.enums import SignalDirection
from app.modes import TradingMode
from app.risk.budgets import daily_loss_limit, drawdown_limit, exposure_limit
from app.risk.config import stricter
from app.risk.models import RuleResult


def result(rule, passed, code, **inputs):
    return RuleResult(
        rule=rule,
        passed=passed,
        rejection_code=code,
        inputs={
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in inputs.items()
        },
    )


def capital(portfolio, config):
    return min(portfolio.equity, config.capital)


def freshness(proposal, portfolio, market, config):
    def fresh(evidence, seconds):
        return (
            evidence.observed_at <= evidence.available_at <= market.as_of
            and market.as_of - evidence.observed_at <= timedelta(seconds=seconds)
        )

    return (
        result(
            "market_freshness",
            fresh(market, config.max_market_age_seconds),
            "STALE_MARKET_DATA",
            max_age_seconds=config.max_market_age_seconds,
        ),
        result(
            "portfolio_freshness",
            fresh(portfolio, config.max_portfolio_age_seconds),
            "STALE_PORTFOLIO",
            max_age_seconds=config.max_portfolio_age_seconds,
        ),
        result(
            "greeks_freshness",
            not proposal.is_option
            or (market.greeks is not None and fresh(market.greeks, config.max_greeks_age_seconds)),
            "STALE_GREEKS",
            required=proposal.is_option,
            max_age_seconds=config.max_greeks_age_seconds,
        ),
        result(
            "proposal_freshness",
            timedelta(0)
            <= market.as_of - proposal.generated_at
            <= timedelta(seconds=config.max_market_age_seconds),
            "STALE_PROPOSAL",
            max_age_seconds=config.max_market_age_seconds,
        ),
    )


def provenance(proposal, portfolio, market, config):
    matching = proposal.data_origin == portfolio.data_origin == market.data_origin
    if market.greeks is not None:
        matching = matching and market.greeks.data_origin == market.data_origin
    return (
        result(
            "provenance",
            matching
            and (market.mode == TradingMode.PAPER or market.data_origin == DataOrigin.LIVE),
            "INVALID_DATA_ORIGIN",
            origin=market.data_origin.value,
            mode=market.mode.value,
        ),
        result(
            "identity",
            proposal.instrument_id == market.instrument_id
            and portfolio.strategy_id == proposal.strategy_id,
            "STATE_IDENTITY_MISMATCH",
            instrument=market.instrument_id,
            strategy=portfolio.strategy_id,
        ),
    )


def per_trade(proposal, portfolio, market, config):
    risk = proposal.planned_risk_per_unit * proposal.quantity
    limit = stricter(
        capital(portfolio, config) * config.per_trade_risk_pct / 100,
        config.max_per_trade_risk_amount,
    )
    return (result("per_trade", risk <= limit, "PER_TRADE_RISK_EXCEEDED", risk=risk, limit=limit),)


def exposure(proposal, portfolio, market, config):
    existing = sum((item.notional for item in portfolio.exposures), Decimal(0))
    proposed = proposal.quantity * proposal.exposure_per_unit
    limit = exposure_limit(portfolio.equity, config)
    return (
        result(
            "exposure",
            existing + proposed <= limit,
            "MAX_EXPOSURE_EXCEEDED",
            existing=existing,
            proposed=proposed,
            limit=limit,
        ),
    )


def daily_loss(proposal, portfolio, market, config):
    loss = max(Decimal(0), -portfolio.realised_day_pnl - portfolio.unrealised_day_pnl)
    limit = daily_loss_limit(portfolio.equity, config)
    risk = proposal.quantity * proposal.planned_risk_per_unit
    return (
        result(
            "daily_loss",
            loss < limit
            and loss + risk + portfolio.reserved_risk <= limit
            and not portfolio.entries_blocked,
            "DAILY_LOSS_LIMIT",
            loss=loss,
            proposed_risk=risk,
            limit=limit,
            latched=portfolio.entries_blocked,
            reserved_risk=portfolio.reserved_risk,
        ),
    )


def drawdown(proposal, portfolio, market, config):
    loss = portfolio.peak_equity - portfolio.equity
    fraction = loss / portfolio.peak_equity * 100
    limit = drawdown_limit(portfolio.peak_equity, config)
    return (
        result(
            "drawdown",
            loss < limit and not portfolio.drawdown_disarmed,
            "MAX_DRAWDOWN_DISARM",
            drawdown_pct=fraction,
            limit_pct=config.max_drawdown_pct,
            loss=loss,
            limit=limit,
            latched=portfolio.drawdown_disarmed,
        ),
    )


def concentration(proposal, portfolio, market, config):
    results = []
    for dimension in ("instrument", "sector", "underlying"):
        attribute = "instrument_id" if dimension == "instrument" else dimension
        key = getattr(proposal, attribute)
        existing = sum(
            (item.notional for item in portfolio.exposures if getattr(item, attribute) == key),
            Decimal(0),
        )
        proposed = proposal.quantity * proposal.exposure_per_unit
        limit = capital(portfolio, config) * getattr(config, f"max_{dimension}_exposure_pct") / 100
        limit = stricter(limit, getattr(config, f"max_{dimension}_exposure_amount"))
        results.append(
            result(
                f"{dimension}_concentration",
                existing + proposed <= limit,
                f"{dimension.upper()}_CONCENTRATION_EXCEEDED",
                existing=existing,
                proposed=proposed,
                limit=limit,
            )
        )
    return tuple(results)


def position_count(proposal, portfolio, market, config):
    return (
        result(
            "position_count",
            portfolio.open_and_pending_positions < config.max_concurrent_positions,
            "MAX_POSITIONS",
            existing=portfolio.open_and_pending_positions,
            limit=config.max_concurrent_positions,
        ),
        result(
            "strategy_position_count",
            portfolio.strategy_open_and_pending_positions < config.max_positions_per_strategy,
            "MAX_STRATEGY_POSITIONS",
            existing=portfolio.strategy_open_and_pending_positions,
            limit=config.max_positions_per_strategy,
        ),
    )


def stop_loss(proposal, portfolio, market, config):
    sign = 1 if proposal.direction == SignalDirection.LONG else -1
    distance = (
        None
        if proposal.stop is None
        else sign * (proposal.entry - proposal.stop) / proposal.entry * 100
    )
    valid = (
        distance is not None
        and config.min_stop_distance_pct <= distance <= config.max_stop_distance_pct
    )
    aligned = all(
        price % proposal.tick_size == 0
        for price in (proposal.entry, proposal.stop, proposal.first_target)
        if price is not None
    )
    return (
        result(
            "stop_loss",
            valid,
            "INVALID_STOP_LOSS",
            distance_pct=distance,
            minimum=config.min_stop_distance_pct,
            maximum=config.max_stop_distance_pct,
        ),
        result(
            "lot_tick",
            aligned and proposal.quantity % proposal.lot_size == 0,
            "INVALID_LOT_OR_TICK",
            lot_size=proposal.lot_size,
            tick_size=proposal.tick_size,
            quantity=proposal.quantity,
        ),
        result(
            "defined_risk",
            not proposal.is_option or proposal.defined_max_loss_per_unit is not None,
            "UNDEFINED_OPTION_RISK",
            max_loss=proposal.defined_max_loss_per_unit,
        ),
    )


def reward_risk(proposal, portfolio, market, config):
    sign = 1 if proposal.direction == SignalDirection.LONG else -1
    risk = proposal.planned_risk_per_unit
    reward = (
        None if proposal.first_target is None else sign * (proposal.first_target - proposal.entry)
    )
    ratio = reward / risk if reward is not None and risk > 0 else None
    return (
        result(
            "reward_risk",
            ratio is not None and ratio >= config.min_reward_risk_ratio,
            "MIN_REWARD_RISK",
            ratio=ratio,
            minimum=config.min_reward_risk_ratio,
        ),
    )


def margin(proposal, portfolio, market, config):
    required = proposal.quantity * proposal.margin_per_unit
    usable = portfolio.available_margin * (1 - config.margin_buffer_pct / 100)
    return (
        result(
            "margin", required <= usable, "INSUFFICIENT_MARGIN", required=required, usable=usable
        ),
    )


def liquidity(proposal, portfolio, market, config):
    levels = market.asks if proposal.direction == SignalDirection.LONG else market.bids
    remaining, notional = proposal.quantity, Decimal(0)
    for level in levels:
        taken = min(remaining, level.quantity)
        notional += taken * level.price
        remaining -= taken
        if remaining == 0:
            break
    sign = 1 if proposal.direction == SignalDirection.LONG else -1
    slippage = (
        max(Decimal(0), sign * (notional / proposal.quantity - proposal.entry))
        if remaining == 0
        else None
    )
    limit = proposal.planned_risk_per_unit * config.max_slippage_risk_pct / 100
    return (
        result(
            "liquidity",
            slippage is not None and slippage <= limit,
            "SLIPPAGE_OR_DEPTH_LIMIT",
            slippage_per_unit=slippage,
            limit=limit,
            unfilled_quantity=remaining,
        ),
    )


def blocks(proposal, portfolio, market, config):
    return tuple(
        result(name, not getattr(market, name), name.upper(), blocked=getattr(market, name))
        for name in ("ban_listed", "news_halt", "event_blackout", "manually_blocked")
    )


def option_premium(proposal, portfolio, market, config):
    if not proposal.is_option:
        return ()
    long_option = proposal.is_option and proposal.direction == SignalDirection.LONG
    return (
        result(
            "option_short",
            not proposal.is_option or long_option,
            "NAKED_SHORT_DISABLED",
            direction=proposal.direction,
        ),
        result(
            "option_premium",
            not long_option
            or proposal.defined_max_loss_per_unit is None
            or (
                proposal.defined_max_loss_per_unit >= proposal.entry
                and proposal.margin_per_unit >= proposal.entry
                and proposal.exposure_per_unit >= proposal.entry
            ),
            "OPTION_PREMIUM_UNDERSTATED",
            entry=proposal.entry,
            max_loss=proposal.defined_max_loss_per_unit,
            margin=proposal.margin_per_unit,
            exposure=proposal.exposure_per_unit,
        ),
    )


RULES = (
    freshness,
    provenance,
    option_premium,
    per_trade,
    exposure,
    daily_loss,
    drawdown,
    concentration,
    position_count,
    stop_loss,
    reward_risk,
    margin,
    liquidity,
    blocks,
)
