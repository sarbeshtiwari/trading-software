"""Recorded snapshot tape; publication time is distinct from observation time."""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from app.core.data_origin import DataOrigin
from app.core.errors import ValidationError
from app.fno.chain.model import validate_chain
from app.marketdata.models import OHLCQuote, OptionChain, Quote
from app.marketdata.validation import validate_quote


@dataclass(frozen=True)
class RecordedSnapshot:
    source: str
    available_at: datetime
    value: Quote | OHLCQuote | OptionChain

    @property
    def key(self):
        if isinstance(self.value, OptionChain):
            return ("chain", self.value.underlying, self.value.expiry)
        return ("quote" if isinstance(self.value, Quote) else "ohlc", self.value.instrument.key)


def validate_snapshot(snapshot):
    value = snapshot.value
    if not isinstance(value, (Quote, OHLCQuote, OptionChain)):
        raise ValidationError("Unsupported recorded snapshot")
    if (
        not snapshot.source.strip()
        or snapshot.available_at.utcoffset() is None
        or value.observed_at.utcoffset() is None
        or value.observed_at > snapshot.available_at
        or not isinstance(value.data_origin, DataOrigin)
    ):
        raise ValidationError("Invalid recorded snapshot provenance or chronology")
    if isinstance(value, OptionChain):
        validate_chain(value)
        if value.expiry is None:
            raise ValidationError("Recorded chain expiry required")
    elif isinstance(value, Quote):
        if (
            not _price(value.ltp)
            or not validate_quote(value).ok
            or any(
                not _price(level.price) or type(level.quantity) is not int or level.quantity < 0
                for level in (*value.bids, *value.asks)
            )
        ):
            raise ValidationError("Invalid recorded quote")
    elif isinstance(value, OHLCQuote):
        if (
            any(not _price(price) for price in (value.open, value.high, value.low, value.close))
            or not value.low
            <= min(value.open, value.close)
            <= max(value.open, value.close)
            <= value.high
            or (value.previous_close is not None and not _price(value.previous_close))
        ):
            raise ValidationError("Invalid recorded OHLC")
    else:
        raise ValidationError("Unsupported recorded snapshot")


def _price(value):
    return isinstance(value, Decimal) and value.is_finite() and value > 0


class SnapshotTape:
    def __init__(self):
        self.events = []
        self.cursor = 0
        self.visible = {}
        self._keys = set()

    def load(self, snapshots):
        candidate = deepcopy([*self.events, *snapshots])
        identities = set()
        for snapshot in candidate:
            validate_snapshot(snapshot)
            identity = (snapshot.key, snapshot.available_at)
            if identity in identities:
                raise ValidationError("Duplicate recorded snapshot publication")
            identities.add(identity)
        candidate.sort(key=lambda item: item.available_at)
        last = {}
        for snapshot in candidate:
            if snapshot.key in last and snapshot.value.observed_at < last[snapshot.key]:
                raise ValidationError("Recorded observations cannot regress")
            last[snapshot.key] = snapshot.value.observed_at
        self.events = candidate
        self._keys = {snapshot.key for snapshot in candidate}

    @property
    def next_at(self):
        return self.events[self.cursor].available_at if self.cursor < len(self.events) else None

    def publish(self, moment):
        quotes = []
        while self.next_at == moment:
            snapshot = self.events[self.cursor]
            self.visible[snapshot.key] = snapshot
            if isinstance(snapshot.value, Quote):
                quotes.append(snapshot.value)
            self.cursor += 1
        return quotes

    def read(self, key, now):
        snapshot = self.visible.get(key)
        if snapshot is None or snapshot.available_at > now:
            return None
        return replace(deepcopy(snapshot.value), data_origin=DataOrigin.REPLAY)

    def declared(self, key):
        return key in self._keys
