"""Versioned JSON recordings with bounded reads and reproducible content hashes."""

import argparse
import hashlib
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analysis.equity import EvidenceModel
from app.core.data_origin import DataOrigin
from app.core.errors import ATSError
from app.marketdata.models import Bar, InstrumentRef, OHLCQuote, OptionChain, Quote
from app.marketdata.recorded import RecordedSnapshot, SnapshotTape
from app.marketdata.replay import ReplayMarketDataProvider, ReplaySeries

MAX_RECORDING_BYTES = 64 * 1024 * 1024


class QuoteRecord(EvidenceModel):
    kind: Literal["QUOTE"]
    source: str = Field(min_length=1)
    available_at: AwareDatetime
    value: Quote


class OHLCRecord(EvidenceModel):
    kind: Literal["OHLC"]
    source: str = Field(min_length=1)
    available_at: AwareDatetime
    value: OHLCQuote


class ChainRecord(EvidenceModel):
    kind: Literal["CHAIN"]
    source: str = Field(min_length=1)
    available_at: AwareDatetime
    value: OptionChain


SnapshotRecord = Annotated[QuoteRecord | OHLCRecord | ChainRecord, Field(discriminator="kind")]


def snapshot_record(snapshot: RecordedSnapshot):
    variants = {
        Quote: (QuoteRecord, "QUOTE"),
        OHLCQuote: (OHLCRecord, "OHLC"),
        OptionChain: (ChainRecord, "CHAIN"),
    }
    if type(snapshot.value) not in variants:
        raise ValueError("unsupported recording payload")
    model, kind = variants[type(snapshot.value)]
    return model(
        kind=kind, source=snapshot.source, available_at=snapshot.available_at, value=snapshot.value
    )


class CandleRecording(EvidenceModel):
    source: str = Field(min_length=1)
    original_data_origin: DataOrigin
    availability_model: Literal["NOMINAL_BAR_CLOSE"]
    instrument: InstrumentRef
    interval_minutes: int = Field(gt=0, strict=True)
    bars: tuple[Bar, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_series(self):
        if not self.source.strip():
            raise ValueError("candle source required")
        series = ReplaySeries(self.instrument, self.interval_minutes, list(self.bars))
        if list(self.bars) != series.bars:
            raise ValueError("recorded candles must be chronological")
        return self


class RecordingBundle(EvidenceModel):
    format_version: Literal[1]
    source: str = Field(min_length=1)
    candles: tuple[CandleRecording, ...]
    snapshots: tuple[SnapshotRecord, ...]

    @model_validator(mode="after")
    def consistent(self):
        if not self.source.strip() or not (self.candles or self.snapshots):
            raise ValueError("nonempty sourced recording required")
        identities = [(row.instrument.key, row.interval_minutes) for row in self.candles]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate recorded candle series")
        SnapshotTape().load(self.recorded_snapshots())
        return self

    def recorded_snapshots(self):
        return [RecordedSnapshot(row.source, row.available_at, row.value) for row in self.snapshots]

    def content_hash(self):
        return hashlib.sha256(_canonical(self.model_dump(mode="json"))).hexdigest()

    def provider(self, *, clock):
        verified = RecordingBundle.model_validate(self.model_dump())
        provider = ReplayMarketDataProvider(clock)
        provider.recording_sha256 = verified.content_hash()
        for row in verified.candles:
            provider.load(row.instrument, row.interval_minutes, row.bars)
        provider.load_snapshots(verified.recorded_snapshots())
        return provider

    def coverage(self):
        return {
            "content_sha256": self.content_hash(),
            "candles": [
                {
                    "instrument": row.instrument.key,
                    "interval_minutes": row.interval_minutes,
                    "count": len(row.bars),
                    "first_available_at": row.bars[0].ts + timedelta(minutes=row.interval_minutes),
                    "last_available_at": row.bars[-1].ts + timedelta(minutes=row.interval_minutes),
                    "availability_model": row.availability_model,
                    "source": row.source,
                    "original_data_origin": row.original_data_origin,
                }
                for row in self.candles
            ],
            "snapshots": [
                {
                    "identity": row.key,
                    "source": row.source,
                    "available_at": row.available_at,
                    "observed_at": row.value.observed_at,
                    "original_data_origin": row.value.data_origin,
                }
                for row in self.recorded_snapshots()
            ],
            "continuous_coverage_verified": False,
        }


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def write_recording(path: Path, bundle: RecordingBundle):
    bundle = RecordingBundle.model_validate(bundle.model_dump())
    payload = _canonical(
        {"content_sha256": bundle.content_hash(), "recording": bundle.model_dump(mode="json")}
    )
    if len(payload) > MAX_RECORDING_BYTES:
        raise ValueError("recording exceeds size limit")
    with Path(path).open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    return bundle.content_hash()


def read_recording(path: Path):
    with Path(path).open("rb") as source:
        payload = source.read(MAX_RECORDING_BYTES + 1)
    if len(payload) > MAX_RECORDING_BYTES:
        raise ValueError("recording exceeds size limit")
    envelope = json.loads(payload, object_pairs_hook=_unique_object)
    if not isinstance(envelope, dict) or set(envelope) != {"recording", "content_sha256"}:
        raise ValueError("invalid recording envelope")
    bundle = RecordingBundle.model_validate(envelope["recording"])
    if envelope["content_sha256"] != bundle.content_hash():
        raise ValueError("recording content hash mismatch")
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate recorded replay data without executing trades"
    )
    parser.add_argument("recording", type=Path)
    arguments = parser.parse_args(argv)
    try:
        bundle = read_recording(arguments.recording)
    except (OSError, ValueError, ATSError) as error:
        print(f"Recording unavailable or invalid: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(bundle.coverage(), default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
