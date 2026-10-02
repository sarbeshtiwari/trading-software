"""Core primitives: clock, money, ids, errors, provenance.

Covers ARCH-012, ARCH-013, ARCH-014, ARCH-008, MD-011, ERR-001.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import errors
from app.core.clock import (
    IST,
    FakeClock,
    SystemClock,
    ensure_ist,
    get_clock,
    reset_clock,
    set_clock,
    to_ist,
    to_utc,
)
from app.core.data_origin import DataOrigin, ExecutionRealism, assert_safe_for_execution
from app.core.errors import ATSError, SyntheticDataInLiveError
from app.core.ids import (
    new_id,
    new_ulid,
    reset_ulid_state_for_testing,
    to_broker_reference_id,
    ulid_timestamp_ms,
)
from app.core.money import (
    FloatNotAllowedError,
    floor_to_lot,
    lots_to_quantity,
    pct_change,
    percent_of,
    quantity_to_lots,
    quantize_money,
    round_to_tick,
    to_decimal,
)
from app.modes import TradingMode

pytestmark = pytest.mark.unit


# --- ARCH-012 -------------------------------------------------------------


def test_clock_tz_and_injection() -> None:
    system = SystemClock()
    assert system.now().tzinfo is not None
    assert system.now().utcoffset() == timedelta(hours=5, minutes=30)
    assert system.utcnow().utcoffset() == timedelta(0)

    fake = FakeClock(start=datetime(2026, 3, 2, 9, 15, tzinfo=IST))
    previous = set_clock(fake)
    try:
        assert get_clock() is fake
        assert get_clock().now().hour == 9
        fake.advance(timedelta(minutes=45))
        assert get_clock().now().hour == 10
        assert get_clock().today() == fake.now().date()
    finally:
        set_clock(previous)
        reset_clock()


def test_fake_clock_monotonic_tracks_wall_clock_but_not_jumps() -> None:
    fake = FakeClock(start=datetime(2026, 3, 2, 9, 15, tzinfo=IST), monotonic_start=100.0)
    fake.advance_seconds(30)
    assert fake.monotonic() == pytest.approx(130.0)

    # A wall-clock jump (e.g. an NTP correction) must not move monotonic time,
    # otherwise latency and timeout measurements become nonsense.
    fake.set_to(datetime(2026, 3, 2, 14, 0, tzinfo=IST))
    assert fake.monotonic() == pytest.approx(130.0)
    assert fake.now().hour == 14


def test_naive_datetimes_are_interpreted_as_ist() -> None:
    # Session times and expiries are written naive in this domain; treating them
    # as the host timezone would silently shift every cutoff.
    naive = datetime(2026, 3, 2, 15, 10)
    assert ensure_ist(naive).utcoffset() == timedelta(hours=5, minutes=30)
    assert ensure_ist(naive).hour == 15

    utc_moment = datetime(2026, 3, 2, 9, 40, tzinfo=timezone.utc)
    assert to_ist(utc_moment).hour == 15
    assert to_ist(utc_moment).minute == 10
    assert to_utc(ensure_ist(naive)) == utc_moment


# --- ARCH-014 -------------------------------------------------------------


def test_money_precision() -> None:
    assert quantize_money("100.005") == Decimal("100.01")  # ROUND_HALF_UP
    assert quantize_money("100.004") == Decimal("100.00")
    assert quantize_money(Decimal("2.675")) == Decimal("2.68")
    assert quantize_money(1234) == Decimal("1234.00")

    # Repeated addition of a value with no exact binary representation must not drift.
    total = Decimal("0")
    for _ in range(10):
        total += to_decimal("0.1")
    assert total == Decimal("1.0")


def test_float_is_rejected_in_money_paths() -> None:
    with pytest.raises(FloatNotAllowedError):
        to_decimal(0.1)  # type: ignore[arg-type]
    with pytest.raises(FloatNotAllowedError):
        quantize_money(19.99)  # type: ignore[arg-type]
    with pytest.raises(FloatNotAllowedError):
        to_decimal(True)  # type: ignore[arg-type]


def test_round_to_tick_directions() -> None:
    # A buy limit rounds down (never pay more); a sell limit rounds up.
    assert round_to_tick("100.123", "0.05", direction="down") == Decimal("100.10")
    assert round_to_tick("100.123", "0.05", direction="up") == Decimal("100.15")
    assert round_to_tick("100.123", "0.05", direction="nearest") == Decimal("100.10")
    assert round_to_tick("100.13", "0.05", direction="nearest") == Decimal("100.15")

    with pytest.raises(ValueError):
        round_to_tick("100", "0")


def test_percent_and_change_helpers() -> None:
    assert percent_of("1000000", "0.5") == Decimal("5000.00")
    assert percent_of("100000", "2") == Decimal("2000.00")
    assert pct_change("110", "100") == Decimal("10.0000")
    assert pct_change("90", "100") == Decimal("-10.0000")
    assert pct_change("110", "0") == Decimal("0")


def test_lot_arithmetic_always_rounds_down() -> None:
    # 1.9 lots must become 1 lot, never 2: rounding up would take more risk than
    # the sizer approved.
    assert floor_to_lot(142, 75) == 75
    assert floor_to_lot(74, 75) == 0
    assert floor_to_lot(150, 75) == 150
    assert lots_to_quantity(2, 75) == 150
    assert quantity_to_lots(150, 75) == 2
    with pytest.raises(ValueError):
        quantity_to_lots(100, 75)


# --- ARCH-013 -------------------------------------------------------------


def test_id_uniqueness_ordering() -> None:
    ids = [new_ulid() for _ in range(10_000)]
    assert len(set(ids)) == len(ids)
    assert all(len(value) == 26 for value in ids)
    # Lexicographic order must equal creation order.
    assert ids == sorted(ids)


def test_ulid_timestamp_roundtrip_and_prefixes() -> None:
    fake = FakeClock(start=datetime(2026, 3, 2, 9, 15, tzinfo=IST))
    previous = set_clock(fake)
    reset_ulid_state_for_testing()
    try:
        value = new_ulid()
        expected_ms = int(fake.utcnow().timestamp() * 1000)
        assert ulid_timestamp_ms(value) == expected_ms
    finally:
        set_clock(previous)
        reset_ulid_state_for_testing()

    prefixed = new_id("prp")
    assert prefixed.startswith("prp_")
    with pytest.raises(ValueError):
        new_id("bad prefix")


def test_broker_reference_id_is_deterministic_and_groww_compatible() -> None:
    intent = new_id("oin")
    reference = to_broker_reference_id(intent)
    # Groww accepts 8-20 alphanumeric characters with at most two hyphens.
    assert 8 <= len(reference) <= 20
    assert reference.isalnum()
    # Determinism is what makes retry-after-timeout safe.
    assert to_broker_reference_id(intent) == reference
    assert to_broker_reference_id(new_id("oin")) != reference


# --- ERR-001 --------------------------------------------------------------


def test_exception_hierarchy() -> None:
    public_errors = [
        getattr(errors, name)
        for name in errors.__all__
        if isinstance(getattr(errors, name), type)
    ]
    assert public_errors
    for error_cls in public_errors:
        assert issubclass(error_cls, ATSError), error_cls

    assert errors.TimeoutError_("x").retryable is True
    assert errors.RateLimitError("x").retryable is True
    assert errors.DuplicateError("x").retryable is False
    # A safety error is never retryable: retrying is exactly what it forbids.
    assert errors.TradingDisabledError("x").retryable is False
    assert errors.SyntheticDataInLiveError("x").retryable is False

    assert errors.TimeoutError_("x").category() == "TRANSIENT"
    assert errors.AuthenticationError("x").category() == "PERMANENT"
    assert errors.StaleDataError("x").category() == "DATA_QUALITY"
    assert errors.TradingDisabledError("x").category() == "SAFETY"
    assert errors.TimeoutError_("boom").code == "TIMEOUT"

    payload = errors.RiskRejectedError("too big", rule="per_trade_risk").to_dict()
    assert payload["code"] == "RISK_REJECTED"
    assert payload["context"]["rule"] == "per_trade_risk"


# --- ARCH-008 / MD-011 ----------------------------------------------------


def test_data_origin_tagging() -> None:
    assert DataOrigin.LIVE.is_real
    assert DataOrigin.HISTORICAL.is_real
    assert not DataOrigin.SYNTHETIC.is_real
    assert not DataOrigin.REPLAY.is_real
    assert ExecutionRealism.SIMULATED.value == "SIMULATED"


@pytest.mark.safety
@pytest.mark.parametrize(
    "origin",
    [DataOrigin.SYNTHETIC, DataOrigin.REPLAY, DataOrigin.HISTORICAL],
)
def test_synthetic_blocked_in_live(origin: DataOrigin) -> None:
    with pytest.raises(SyntheticDataInLiveError):
        assert_safe_for_execution(origin, TradingMode.LIVE)

    # Live data is the only provenance permitted to drive execution in LIVE.
    assert_safe_for_execution(DataOrigin.LIVE, TradingMode.LIVE)
    # PAPER and SUPERVISED are permissive: simulation is the point of one, and
    # the other has a human in front of every order.
    assert_safe_for_execution(origin, TradingMode.PAPER)
    assert_safe_for_execution(origin, TradingMode.SUPERVISED)
