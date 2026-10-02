"""Groww option chain and Greeks — GRW-019, GRW-020.

Both endpoints exist in the documented SDK surface (``get_option_chain``,
``get_greeks``). Two parsing rules matter more than the shapes:

* **A missing Greek is ``None``, never ``0``.** A delta of zero is a real,
  meaningful value for a far OTM option; substituting it for "unknown" would feed
  a confident wrong number into portfolio Greek aggregation (GRK-006).
* **Broker-supplied Greeks are labelled as such** (``GreekSource.BROKER``) so the
  resolver can prefer them over locally computed ones and flag divergence
  (GRK-004).

Strikes that fail to parse are reported, not skipped silently: a chain missing
half its ladder should look broken, not thin.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Optional

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.endpoints import Endpoints
from app.brokers.groww.mapping import pick
from app.brokers.groww.ratelimit import RateCategory
from app.core.clock import Clock, get_clock
from app.core.data_origin import DataOrigin
from app.core.enums import GreekSource, OptionType
from app.core.errors import InvalidResponseError
from app.core.logging import get_logger
from app.fno.chain.model import validate_chain
from app.marketdata.models import Greeks, OptionChain, OptionLeg, OptionStrike

logger = get_logger("brokers.groww.options")

__all__ = ["GrowwOptionsApi"]


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("non-finite number")
        return result
    except Exception:  # noqa: BLE001 - malformed numbers are reported by the caller
        raise InvalidResponseError("Malformed option-chain number") from None


def _int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed != parsed.to_integral_value():
            raise ValueError("non-integral count")
        return int(parsed)
    except (TypeError, ValueError, ArithmeticError):
        raise InvalidResponseError("Malformed option-chain count") from None


def _parse_greeks(payload: Any, *, observed_at: datetime) -> Optional[Greeks]:
    if not isinstance(payload, Mapping):
        return None
    greeks = Greeks(
        delta=_decimal(pick(payload, "delta")),
        gamma=_decimal(pick(payload, "gamma")),
        theta=_decimal(pick(payload, "theta")),
        vega=_decimal(pick(payload, "vega")),
        rho=_decimal(pick(payload, "rho")),
        implied_volatility=_decimal(
            pick(payload, "implied_volatility", "iv")
        ),
        source=GreekSource.BROKER,
        computed_at=observed_at,
    )
    # All-None means the broker sent an empty object; say so rather than pretend.
    if all(
        getattr(greeks, field) is None
        for field in ("delta", "gamma", "theta", "vega", "rho", "implied_volatility")
    ):
        return None
    return greeks


class GrowwOptionsApi:
    def __init__(self, client: GrowwClient, *, clock: Optional[Clock] = None) -> None:
        self._client = client
        self._clock = clock or get_clock()

    async def chain(self, underlying: str, expiry: Optional[date] = None) -> OptionChain:
        params: dict[str, Any] = {"trading_symbol": underlying}
        if expiry is not None:
            params["expiry"] = expiry.isoformat()

        payload = await self._client.get(
            Endpoints.OPTION_CHAIN.resolve(),
            category=RateCategory.LIVE_DATA,
            params=params,
        )
        if not isinstance(payload, Mapping):
            raise InvalidResponseError(
                "Groww option chain payload was not an object",
                context={"underlying": underlying, "type": type(payload).__name__},
            )
        return self._parse_chain(underlying, expiry, payload)

    def _parse_chain(
        self, underlying: str, expiry: Optional[date], payload: Mapping[str, Any]
    ) -> OptionChain:
        observed_at = self._clock.now()
        rows = pick(payload, "option_chains", "chains", "strikes", default=[])
        if not isinstance(rows, list):
            raise InvalidResponseError(
                "Groww option chain carried no strike ladder",
                context={"underlying": underlying, "keys": sorted(payload)},
            )

        strikes: list[OptionStrike] = []
        unparsed = 0
        for row in rows:
            strike = self._parse_strike(row, observed_at=observed_at)
            if strike is None:
                unparsed += 1
                continue
            strikes.append(strike)

        if unparsed:
            logger.warning(
                "Option chain rows could not be parsed",
                extra={"underlying": underlying, "unparsed": unparsed, "parsed": len(strikes)},
            )
            raise InvalidResponseError("Incomplete option-chain ladder: malformed strikes")

        strikes.sort(key=lambda item: item.strike)
        chain = OptionChain(
            underlying=underlying,
            expiry=expiry or _parse_expiry(pick(payload, "expiry", "expiry_date")),
            observed_at=observed_at,
            spot=_decimal(pick(payload, "spot_price", "underlying_price", "last_price")),
            strikes=tuple(strikes),
            data_origin=DataOrigin.LIVE,
            raw=dict(payload),
        )
        try:
            return validate_chain(chain)
        except ValueError as exc:
            raise InvalidResponseError("Invalid option-chain ladder") from exc

    def _parse_strike(
        self, row: Any, *, observed_at: datetime
    ) -> Optional[OptionStrike]:
        if not isinstance(row, Mapping):
            return None
        strike = _decimal(pick(row, "strike_price", "strike"))
        if strike is None:
            return None

        return OptionStrike(
            strike=strike,
            call=self._parse_leg(
                pick(row, "call_option", "ce", "call"), OptionType.CE, observed_at
            ),
            put=self._parse_leg(
                pick(row, "put_option", "pe", "put"), OptionType.PE, observed_at
            ),
        )

    def _parse_leg(
        self, payload: Any, option_type: OptionType, observed_at: datetime
    ) -> Optional[OptionLeg]:
        if not isinstance(payload, Mapping):
            return None
        return OptionLeg(
            trading_symbol=str(pick(payload, "trading_symbol", "symbol", default="")),
            option_type=option_type,
            ltp=_decimal(pick(payload, "last_price", "ltp")),
            bid=_decimal(pick(payload, "bid_price", "bid")),
            ask=_decimal(pick(payload, "ask_price", "ask", "offer_price")),
            volume=_int(pick(payload, "volume", "total_traded_volume")),
            open_interest=_int(pick(payload, "open_interest", "oi")),
            open_interest_change=_int(
                pick(payload, "open_interest_change", "oi_day_change", "change_in_oi")
            ),
            greeks=_parse_greeks(
                pick(payload, "greeks", default=payload), observed_at=observed_at
            ),
        )

    async def greeks(
        self,
        underlying: str,
        trading_symbol: str,
        expiry: Optional[date] = None,
    ) -> Optional[Greeks]:
        """Greeks for one contract. ``None`` when the broker has none to give."""
        params: dict[str, Any] = {
            "underlying": underlying,
            "trading_symbol": trading_symbol,
        }
        if expiry is not None:
            params["expiry"] = expiry.isoformat()

        payload = await self._client.get(
            Endpoints.GREEKS.resolve(), category=RateCategory.LIVE_DATA, params=params
        )
        return _parse_greeks(payload, observed_at=self._clock.now())


def _parse_expiry(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
