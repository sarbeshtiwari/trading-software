"""Recorded-file integrity, limits and coverage using isolated fixtures."""

import json

import pytest

from app.core.clock import FakeClock
from app.core.data_origin import DataOrigin
from app.marketdata import recordings
from app.marketdata.recordings import (
    CandleRecording,
    RecordingBundle,
    read_recording,
    snapshot_record,
    write_recording,
)
from tests.unit.test_recorded_replay import snapshots
from tests.unit.test_replay_safety import BASE, FIRST, candle


def bundle():
    return RecordingBundle(
        format_version=1,
        source="isolated-test-recording",
        candles=(
            CandleRecording(
                source="isolated-bars",
                original_data_origin=DataOrigin.SYNTHETIC,
                availability_model="NOMINAL_BAR_CLOSE",
                instrument=FIRST,
                interval_minutes=1,
                bars=(candle(),),
            ),
        ),
        snapshots=tuple(snapshot_record(item) for item in snapshots()),
    )


async def test_file_roundtrip_preserves_prices_times_and_hash(tmp_path):
    original = bundle()
    path = tmp_path / "recording.json"
    digest = write_recording(path, original)
    loaded = read_recording(path)
    assert loaded.model_dump(mode="json") == original.model_dump(mode="json")
    assert loaded.content_hash() == digest
    coverage = loaded.coverage()
    assert coverage["continuous_coverage_verified"] is False
    assert coverage["candles"][0]["count"] == 1
    assert coverage["snapshots"][0]["original_data_origin"] == DataOrigin.SYNTHETIC
    provider = loaded.provider(clock=FakeClock(BASE))
    assert provider.recording_evidence() == []
    await provider.step()
    assert (await provider.get_quote(FIRST)).best_bid == 100
    assert provider.recording_evidence()[0]["recording_sha256"] == digest
    with pytest.raises(FileExistsError):
        write_recording(path, original)


@pytest.mark.parametrize("problem", ["price", "version", "duplicate_key", "truncated", "size"])
def test_corrupt_or_oversized_files_fail_closed(tmp_path, monkeypatch, problem):
    path = tmp_path / "recording.json"
    write_recording(path, bundle())
    text = path.read_text(encoding="utf-8")
    if problem in {"price", "version"}:
        payload = json.loads(text)
        if problem == "price":
            payload["recording"]["snapshots"][0]["value"]["ltp"] = "102"
        else:
            payload["recording"]["format_version"] = 2
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif problem == "duplicate_key":
        path.write_text(
            text.replace('"format_version":1', '"format_version":1,"format_version":1'),
            encoding="utf-8",
        )
    elif problem == "truncated":
        path.write_text(text[: len(text) // 2], encoding="utf-8")
    else:
        monkeypatch.setattr(recordings, "MAX_RECORDING_BYTES", 10)
        with pytest.raises(ValueError, match="size limit"):
            write_recording(tmp_path / "too-large.json", bundle())
        assert not (tmp_path / "too-large.json").exists()
    with pytest.raises(ValueError):
        read_recording(path)


def test_ambiguous_series_and_unacknowledged_candle_model_are_rejected():
    payload = bundle().model_dump()
    payload["candles"] = (*payload["candles"], payload["candles"][0])
    with pytest.raises(ValueError, match="duplicate"):
        RecordingBundle.model_validate(payload)
    payload = bundle().model_dump()
    del payload["candles"][0]["availability_model"]
    with pytest.raises(ValueError):
        RecordingBundle.model_validate(payload)


def test_inspection_cli_reports_actual_coverage_and_redacts_errors(tmp_path, capsys):
    path = tmp_path / "recording.json"
    digest = write_recording(path, bundle())
    assert recordings.main([str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["content_sha256"] == digest
    assert report["continuous_coverage_verified"] is False
    path.write_text('{"private-fixture-value":', encoding="utf-8")
    assert recordings.main([str(path)]) == 2
    error = capsys.readouterr()
    assert "private-fixture-value" not in error.err
    assert "JSONDecodeError" in error.err
