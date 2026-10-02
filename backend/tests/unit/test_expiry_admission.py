from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.clock import IST, UTC
from app.core.enums import Segment
from app.execution.expiry import expiry_blocker


def test_configured_cutoff_exact_boundary_and_timezone():
    contract = SimpleNamespace(segment=Segment.FNO, expiry_date=date(2026, 9, 21))
    now = datetime(2026, 9, 21, 14, 30, tzinfo=IST)
    assert expiry_blocker(contract, now - timedelta(microseconds=1), "14:30") is None
    assert expiry_blocker(contract, now, "14:30") == "FNO_EXPIRY_ENTRY_CUTOFF"
    assert expiry_blocker(contract, now.astimezone(UTC), "14:30") == "FNO_EXPIRY_ENTRY_CUTOFF"
    assert expiry_blocker(contract, now + timedelta(days=1), "14:30") == "FNO_CONTRACT_EXPIRED"
    contract.expiry_date = None
    assert expiry_blocker(contract, now, "14:30") == "FNO_EXPIRY_UNAVAILABLE"
    contract.segment = Segment.CASH
    assert expiry_blocker(contract, now, "14:30") is None


@pytest.mark.parametrize("value", ["25:00", "14:30:00", "14:30+05:30"])
def test_cutoff_configuration_refuses_invalid_time(value):
    with pytest.raises(ValidationError):
        Settings(fno_expiry_entry_cutoff_time=value)
