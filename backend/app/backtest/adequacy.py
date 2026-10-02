"""Recorded window disclosure: elapsed span is never proof of continuous data or confidence."""

from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field

from app.analysis.equity import EvidenceModel
from app.core.clock import UTC


class WindowAdequacy(EvidenceModel):
    source_run_id: str
    role: Literal["SINGLE", "OUT_OF_SAMPLE"]
    start_at: AwareDatetime
    end_at: AwareDatetime
    elapsed_seconds: Decimal = Field(gt=0)
    minimum_calendar_days: int = Field(gt=0, strict=True)
    span_state: Literal["BELOW_MINIMUM", "SPAN_MEETS_MINIMUM"]
    recorded_publications: int = Field(ge=0, strict=True)
    first_publication_at: AwareDatetime | None
    last_publication_at: AwareDatetime | None
    continuous_coverage_verified: Literal[False] = False
    statistical_confidence_verified: Literal[False] = False


class WindowDisclosure(EvidenceModel):
    version: Literal[1] = 1
    scope: Literal["STRATEGY_INSTRUMENT_RECORDED_PUBLICATIONS"] = (
        "STRATEGY_INSTRUMENT_RECORDED_PUBLICATIONS"
    )
    limitation: str = (
        "Calendar span is not trading-session coverage. Recorded publication counts do not "
        "prove completeness, statistical confidence or profitability. Groww intraday history "
        "is limited by provider availability (project design: three months); longer research "
        "requires available local archives. External source coverage remains unverified."
    )
    windows: tuple[WindowAdequacy, ...]


def disclose_window(manifest, recording, minimum_days):
    selected = next(
        item for item in manifest.instruments if item.id == manifest.strategy_instrument_id
    )
    publications = []
    for series in recording.candles:
        if (
            series.instrument.exchange == selected.exchange
            and series.instrument.segment == selected.segment
            and series.instrument.trading_symbol == selected.trading_symbol
        ):
            publications.extend(
                bar.ts + timedelta(minutes=series.interval_minutes) for bar in series.bars
            )
    for row in recording.snapshots:
        if row.kind == "CHAIN":
            matches = (
                row.value.underlying == selected.underlying
                and row.value.expiry == selected.expiry_date
            )
        else:
            matches = (
                row.value.instrument.exchange == selected.exchange
                and row.value.instrument.segment == selected.segment
                and row.value.instrument.trading_symbol == selected.trading_symbol
            )
        if matches:
            publications.append(row.available_at)
    start = manifest.start_at.astimezone(UTC)
    end = manifest.end_at.astimezone(UTC)
    visible = sorted(
        (moment for moment in publications if start <= moment.astimezone(UTC) <= end),
        key=lambda moment: moment.astimezone(UTC),
    )
    duration = end - start
    seconds = (
        Decimal(duration.days * 86400 + duration.seconds) + Decimal(duration.microseconds) / 1000000
    )
    window = WindowAdequacy(
        source_run_id=manifest.run_id,
        role="SINGLE",
        start_at=manifest.start_at,
        end_at=manifest.end_at,
        elapsed_seconds=seconds,
        minimum_calendar_days=minimum_days,
        span_state="BELOW_MINIMUM" if seconds < minimum_days * 86400 else "SPAN_MEETS_MINIMUM",
        recorded_publications=len(visible),
        first_publication_at=visible[0] if visible else None,
        last_publication_at=visible[-1] if visible else None,
    )
    return WindowDisclosure(windows=(window,)).model_dump(mode="json")


def window_warning(disclosure):
    report = WindowDisclosure.model_validate(disclosure)
    return "; ".join(
        f"{window.source_run_id}: {window.span_state}; {window.elapsed_seconds} elapsed seconds; "
        f"configured minimum {window.minimum_calendar_days} calendar days; "
        "continuous coverage and statistical confidence UNVERIFIED"
        for window in report.windows
    )
