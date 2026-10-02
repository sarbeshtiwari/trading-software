"""Shared configured monetary limits for veto rules, latches and reporting."""

from app.risk.config import stricter


def daily_loss_limit(equity, config):
    return stricter(
        min(equity, config.capital) * config.daily_loss_limit_pct / 100,
        config.max_daily_loss_amount,
    )


def drawdown_limit(peak_equity, config):
    return stricter(peak_equity * config.max_drawdown_pct / 100, config.max_drawdown_amount)


def exposure_limit(equity, config):
    return stricter(
        min(equity, config.capital) * config.max_gross_exposure_multiple,
        config.max_gross_exposure_amount,
    )
