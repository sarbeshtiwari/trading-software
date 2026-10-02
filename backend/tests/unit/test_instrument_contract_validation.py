import pytest

from app.core.enums import OptionType
from app.instruments.loader import LoadResult, parse_instrument_csv


def contract_csv(*, kind="CE", lot="50", tick="0.05", expiry="2026-11-23", strike="840"):
    return (
        "exchange,segment,trading_symbol,instrument_type,lot_size,tick_size,expiry_date,strike_price\n"
        f"NSE,FNO,TEST,{kind},{lot},{tick},{expiry},{strike}\n"
    )


@pytest.mark.parametrize("kind", ["CE", "PE"])
def test_groww_contract_type_supplies_option_side(kind):
    rows = parse_instrument_csv(contract_csv(kind=kind))
    assert len(rows) == 1
    assert rows[0].option_type is OptionType(kind)
    assert rows[0].lot_size == 50
    assert rows[0].expiry_date.isoformat() == "2026-11-23"


@pytest.mark.parametrize("field", ["lot", "tick"])
@pytest.mark.parametrize("value", ["", "broken", "NaN", "Infinity", "0", "-1"])
def test_invalid_contract_sizes_never_become_defaults(field, value):
    result = LoadResult()
    assert parse_instrument_csv(contract_csv(**{field: value}), result) == []
    assert result.skipped == 1


@pytest.mark.parametrize("lot", ["1.5", "2147483648"])
def test_lot_size_is_exact_and_database_representable(lot):
    assert parse_instrument_csv(contract_csv(lot=lot)) == []


@pytest.mark.parametrize(
    "changes",
    [{"expiry": ""}, {"expiry": "bad"}, {"strike": "NaN"}, {"strike": "0"}, {"kind": "OPTION"}],
)
def test_incomplete_option_contract_is_rejected(changes):
    assert parse_instrument_csv(contract_csv(**changes)) == []
