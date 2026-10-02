"""EQ-005: ATR percent, annualised log-return volatility and simple-return beta."""

from datetime import datetime, timedelta
from decimal import Decimal

from app.analysis.equity import EquitySnapshot, EvidenceModel, aligned_history
from app.analysis.technical.volatility import atr_percent, historical_volatility


class VolatilityProfile(EvidenceModel):
    atr_percent: Decimal | None
    realised_volatility: Decimal | None
    beta: Decimal | None
    period: int
    periods_per_year: int


def volatility_profile(
    member: EquitySnapshot,
    benchmark: EquitySnapshot,
    *,
    as_of: datetime,
    period: int,
    periods_per_year: int,
    max_history_age: timedelta,
) -> VolatilityProfile:
    if (
        type(period) is not int
        or period < 2
        or type(periods_per_year) is not int
        or periods_per_year < 1
    ):
        raise ValueError("invalid volatility policy")
    stock, index = aligned_history(
        member, benchmark, as_of=as_of, lookback=period, max_age=max_history_age
    )
    stock_returns = [
        stock[number].close / stock[number - 1].close - 1 for number in range(1, len(stock))
    ]
    index_returns = [
        index[number].close / index[number - 1].close - 1 for number in range(1, len(index))
    ]
    stock_mean, index_mean = sum(stock_returns) / period, sum(index_returns) / period
    covariance = sum(
        (stock_return - stock_mean) * (index_return - index_mean)
        for stock_return, index_return in zip(stock_returns, index_returns, strict=True)
    )
    variance = sum((index_return - index_mean) ** 2 for index_return in index_returns)
    return VolatilityProfile(
        atr_percent=atr_percent([bar.technical_bar() for bar in stock], period)[-1],
        realised_volatility=historical_volatility(
            [bar.close for bar in stock], period, periods_per_year
        )[-1],
        beta=covariance / variance if variance else None,
        period=period,
        periods_per_year=periods_per_year,
    )
