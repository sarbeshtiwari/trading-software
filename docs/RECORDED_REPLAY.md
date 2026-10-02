# Recorded snapshots through the PAPER path

`ReplayMarketDataProvider.load_snapshots` accepts `RecordedSnapshot` values with
an explicit source, timezone-aware availability time and a provider-native
`Quote`, `OHLCQuote` or `OptionChain`. Existing candle replay remains available.
This is a production provider extension, not another trading pipeline.

The replay timeline merges closed candles and snapshot publications. All events
at a timestamp are published before callbacks. Observation timestamps are never
renewed to the replay clock. Reads expose only published snapshots whose original
availability time is not in the future. Rewinding the clock hides future state
and cannot resume a started replay. Inputs cannot change after stepping begins.

Recorded quote/depth and OHLC streams take precedence over candle-derived views
for their declared instrument. Before their first publication they are unavailable,
not replaced with a candle price. Later candles do not refresh an old quote or
invent depth. Option-chain reads require an explicit expiry; there is no synthetic
chain fallback. A missing previous close remains missing. Duplicate publication
identities, regressing observations and malformed records are rejected atomically.
Records and returned values are defensively copied.

Returned values are labelled `REPLAY`, never `LIVE`. Original source labels,
origins, observation times and availability times are retained in visible replay
evidence, persisted by the existing market/regime audit services alongside the
actual consumed observations. This records provenance; it does not certify an
external vendor's accuracy.

## Verification and remaining work

`test_recorded_worker.py` loads isolated deterministic fixture recordings into
the real replay provider. The existing reference worker ingests candles/depth,
refreshes provider-based breadth/IV, generates a strategy proposal, passes shared
validation/sizing/risk/preflight, executes PAPER entry/exit and records the actual
fixture costs, net P&L, journal and API-visible state. No strategy, risk or OMS
layer is mocked. Fixture data is not a claimed live-market observation or paper
validation history.

`test_recorded_replay.py` verifies publication/observation separation, atomic
same-time visibility, no candle substitution, mutable-input isolation, missing
data and malformed/future/regressing records.

This is not yet a historical-run application or full-day certification. Isolated
run/account state, continuous coverage checks, historical fill conventions,
metrics, API/UI run control and walk-forward remain pending. Never rewind a
running PAPER database, clock or account to run a
backtest. Candle-only history cannot satisfy depth, previous-close or option-chain
requirements; missing coverage must be reported, not fabricated.

## Durable recording files

`app.marketdata.recordings` provides a versioned JSON bundle containing typed
candle series and tagged quote/OHLC/chain snapshots. Candle sources/origins and
the explicit `NOMINAL_BAR_CLOSE` availability assumption are required. Snapshot
availability is recorded independently of its original observation time.

`write_recording(path, bundle)` creates a new file exclusively, flushes it and
syncs it to disk. It never overwrites an existing recording. Interrupted writes
may leave an incomplete file; the loader rejects it rather than treating it as
usable evidence. `read_recording(path)` validates schema/version, chronology,
series/publication identities and the canonical-content SHA-256. Duplicate JSON
keys, malformed/truncated files and content mismatches fail closed. Reads/writes
have a 64 MiB payload limit. A content hash detects mismatched content; it is not
a signature or proof of vendor authenticity.

From `backend`, inspect a local recording without executing any trade:

```powershell
.\.venv\Scripts\python.exe -m app.marketdata.recordings path\recording.json
```

The report gives actual series boundaries/counts, snapshot availability/source
metadata and the content hash. `continuous_coverage_verified` remains false: first
and last timestamps do not prove a complete session or complete instrument set.
Invalid files return exit code 2 without echoing their contents in an error.

`bundle.provider(clock=isolated_clock)` builds the existing replay provider.
The recording hash accompanies consumed snapshot provenance in the existing
market/regime audit chain. The integration fixture now writes the bundle to disk,
reloads it and verifies the actual PAPER lifecycle and journal from that file.
