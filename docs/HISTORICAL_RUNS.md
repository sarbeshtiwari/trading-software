# Isolated historical runs

Historical execution must not rewind a running application's database, account,
clock or trading gates. The implementation prepares an isolated account and
drives its shared PAPER worker over a bounded recording window. The dedicated
child-process launcher and current-database API/UI inspection are available;
API/UI run creation remains pending. Do not
call the bootstrap/engine services inside the serving application process.

`app.backtest.bootstrap.HistoricalManifest` requires:

- a run identity and verified recording content hash;
- aware decision-window timestamps;
- explicit instrument identities, lot/tick sizes, state, provenance and knowledge
  times, including required index and breadth constituent metadata;
- the reference strategy instrument and requested risk fraction;
- complete deterministic risk limits, including explicitly supplied capital;
- effective, already-known fee schedules and reference/regime input declarations;
- an explicit calendar source, knowledge date, holidays, special sessions and
  declared complete years;
- simulated fill configuration, including the reproducibility seed.

Future knowledge, incomplete declared calendar coverage, incompatible source
origins and missing instrument dependencies are rejected. Recorded historical
decisions use `REPLAY`; execution remains `PAPER`/`SIMULATED`. Selected universe
metadata is owner supplied and is not external survivorship verification.

## Database and clock guards

The target database must be named `ats_history_<run_id>` (the SQLite test file
stem uses the same convention). Every application table must be empty. A normal
database name or any existing application state causes refusal before writes.
The database schema must already exist; bootstrap neither deletes existing data
nor creates databases. PostgreSQL remains the production target; integration
tests use isolated SQLite files, not evidence of PostgreSQL execution.

The installed application clock must be the exact supplied historical clock,
at the manifest start. An active application worker, non-PAPER broker/mode,
capital mismatch or recording-hash mismatch also blocks preparation. These guards
alone are not an operating-system process boundary: use the launcher to run
this code in a dedicated child process, never in the serving API process.

`prepare_run` transactionally reserves a `BacktestRun`, source metadata, initial
risk configuration and a unique bootstrap audit chain. It then uses existing
fee/input publication services, strategy registration and PAPER account recovery.
The result is an unstarted production `PaperWorker` with a reconciled simulated
account. No proposal or order is manufactured by bootstrap, and no trading gate
is cleared. The row is `PREPARED`, **not completed**. Errors after reservation
record `FAILED` and the exception class, not arbitrary error payloads. Incomplete
state is never silently overwritten or reused.

## Verification

`test_historical_bootstrap.py` verifies the actual isolated account/configuration,
source audit, refusal to reuse it, wrong-database refusal without writes,
capital/clock/hash mismatches, future knowledge, missing capital and a failed
registration path. Preparation is not strategy validation or a LIVE approval gate.

## Bounded execution

`run_prepared(worker, manifest)` requires the original PREPARED manifest, matching
database/hash/capital/PAPER provider and unchanged execution/session settings.
Warm-up publishes only evidence already available at the start, without rewinding
the application clock or notifying subscribers. Subsequent cycles run at recorded
publications or the configured worker cadence, never beyond the requested end.
The existing worker performs analysis, strategy, validation, sizing, risk,
preflight, fills, protection, exits and costed journal processing. There is no
separate signal generator, order emulator or duplicated P&L calculation.

The fill convention is explicitly `RECORDED_DEPTH_AT_PUBLICATION`, not next-bar
open. A recorded quote is not refreshed by a later candle. The run clears only
the initial startup gate after worker recovery; other safety blockers remain.
Progress and journal-derived gross P&L, charges and net P&L are persisted.
Missing outcome values remain unavailable. Open positions, pending orders,
source stand-downs or unavailable costs produce INCOMPLETE; worker failures
produce FAILED. Ending the window never invents a closing fill. Exceptions
persist a sanitized error class when the database remains writable.

COMPLETED describes this bounded timeline only, not continuous/full-day data
coverage, profitability, external verification or strategy approval. A zero-trade
run can complete when its gates correctly prevent entry. Metrics, curves, OOS,
walk-forward and API/UI launch controls remain pending.
`test_historical_engine.py` verifies a real recorded trade, hand-computed gross/
net results, post-window withholding, immutable settings, preserved safety blocks
and failure cleanup; replay tests verify warm-up clock isolation and fail-closed
recovery. PostgreSQL execution remains unverified.

## Local process launcher

From `backend` on Windows:

```powershell
.\.venv\Scripts\python.exe -m app.backtest.launcher C:\private\manifest.json C:\private\recording.json C:\private\historical-settings.json --timeout-seconds 3600
```

Supply your own validated manifest, actual recording and explicit settings; the
command does not download data, invent capital or create a sample trade. Migrate
the dedicated database first. The settings JSON requires `database_url` and
`starting_capital`; optional keys are `paper_cycle_seconds`,
`tick_staleness_seconds`, `intraday_squareoff_time`, `entry_blackout_open_minutes`,
`entry_blackout_close_minutes`, `max_trades_per_day` and `max_slippage_pct`.
Unspecified execution values use the application's defaults and are persisted
with the run. Unknown/nested/null settings are rejected. Keep the settings file
outside version control, restrict its permissions and never share its database
credentials. Mode and broker are fixed to PAPER/paper; LIVE cannot be requested.

The launcher uses the current Python interpreter, an argument list (no shell),
a temporary working directory without the serving `.env`, and a restricted
environment. Parent database/clock/gates and API/Groww secrets are not inherited.
Windows children do not open a console window. The child writes the same durable
audit/database records but suppresses console diagnostics; failures report only
the exception class, never configuration payloads or database passwords.

Exit codes: `0` bounded timeline completed; `3` incomplete; `1` worker failed;
`2` setup/launcher exception. The JSON report is not a strategy-approval signal.
A timeout or OS termination cannot guarantee a final database write: RUNNING
may remain and must be treated as interrupted/unverified, never successful.
Existing account/run databases are never overwritten or resumed automatically.
Use a new explicit run identity/database after reviewing a failed run.

Actual subprocess tests verify costed completion, an incomplete open position,
no parent-state mutation despite conflicting parent configuration, refusal to
reuse the database, and sanitized malformed-input errors. They use SQLite test
databases; production PostgreSQL connectivity is not thereby verified.

## Stored session regression scope

`test_historical_session.py` saves and reloads a deterministic fixture covering
09:15–15:40. The real worker processes all session phases, one reference-strategy
trade, protection, target exit, charges and EOD reconciliation. Its declared
60-second worker cadence and 30-minute opening entry blackout are simulation
inputs, not recommended production settings. The blackout allows the strategy's
21 closed candles and regime's 29 closed index candles to accumulate normally.
Minute-sampled quotes do not establish continuous tick/depth coverage.

Deleting one scheduled quote causes a stale-input audit and the existing worker
error latch: the run fails before any order, rather than silently resuming on a
later price. Both the successful session and gap refusal are automated; fixture
outcomes are not external trading validation. A COMPLETED arbitrary recording
still needs independent coverage and adequacy review before strategy validation.

## Persisted account and trade evidence

The historical audit chain stores an account baseline and an observation after
each actual worker cycle. Observations include net equity, cash, charge estimates,
gross realised P&L, unrealised P&L, exposure, margin and actual open position IDs.
Equity comes from the shared PAPER account, not a parallel backtest calculator.
Unknown fill costs, stale/missing position marks or a failed worker produce an
explicit unavailable observation; a curve gap is never forward-filled.

Final results retain the sampled equity curve and its audit-event references.
Closed journal entries populate `backtest_trades`; persisted links connect them
to journal/proposal/risk/order/position/audit identities. Open positions remain
open in the actual OMS and are not fabricated as completed backtest trades.
These records survive a child-process exit and can support read APIs. Report
APIs and frontend rendering are available for the configured database, including
owner-published reports from isolated historical databases.

## Descriptive metric conventions

The shared `app.portfolio.metrics.performance` implementation consumes net-equity
observations and closed-trade net P&L, not gross returns. Historical reports use
it directly. Total return is final/initial equity minus one; sampled drawdown is
the positive fraction below the running equity peak. Recovery factor uses net
equity gain divided by maximum absolute sampled drawdown. These are sampled
observations, not a guarantee of the true intrabar maximum drawdown.

Win rate counts positive trades over all closed trades, including breakevens in
the denominator. Average loss is signed negative; profit factor divides summed
positive trade P&L by absolute summed negative trade P&L. Expectancy is arithmetic
mean net P&L. Holding time comes from actual journal entry/exit timestamps.
Zero denominators, empty samples and missing values remain unavailable. Missing
equity invalidates aggregate equity metrics rather than bridging the gap.
Negative ending equity is retained, including drawdown exceeding 100%.

Persisted numeric ratio columns retain their existing six-decimal precision;
the descriptive-metric JSON and drawdown curve retain Decimal strings. The
integer average-holding column truncates fractional seconds; exact precision
remains in JSON. Annualized CAGR/Sharpe/Sortino and exposure-duration metrics
remain unavailable: these irregular event/cadence samples are not a verified
regular return series, and no benchmark/risk-free input is invented. Sharpe's
[original definition](https://web.stanford.edu/~wfsharpe/art/sr/sr.htm) uses
differential returns; a constant zero benchmark is not silently assumed.
Metrics are descriptive evidence, never strategy approval or a forecast.

## Authenticated inspection

The Backtests dashboard view reads actual records through four owner-authenticated
GET APIs: `/api/v1/backtests`, `/api/v1/backtests/{run_id}`, and that run's
`/trades` and `/samples` resources. Lists use nonnegative `offset` and bounded
`limit`; responses explicitly indicate whether another page exists. Configuration
payloads, database URLs and error traces are not exposed. Launch uses the separate
owner-plan API below, never a client-selected database or historical API clock.

The UI polls every ten seconds, distinguishes loading/empty/error/unavailable
states, shows incomplete/failed status, and renders sampled equity/drawdown
without connecting points across gaps. Charts are labelled as the displayed
sample page, not an entire dataset. Zero-trade and undefined-ratio results remain
truthful. RUNNING describes persisted state, not proof of a live child process.

Inspection is limited to the API's configured database. Isolated child-run
databases are not automatically discovered; use the owner publication CLI below.
Do not repoint an active trading server or import arbitrary client database URLs.
The real-browser regression now publishes from a separate historical database,
then verifies actual React/API/authentication inspection and browser reload of
the persisted trade/metrics/curves in the application catalog. It now launches
the isolated process through the actual authenticated dashboard controls.

## Owner-controlled report publication

Newly finalized runs bind the complete stored report payload (run, results and
backtest trades) to `catalog_sha256` in their immutable historical audit chain.
After reviewing a run, execute this from a separate terminal in `backend`, using
the normal application's destination `.env` and the source settings file used
for historical execution:

```powershell
.\.venv\Scripts\python.exe -m app.backtest.publish C:\private\historical-settings.json YOUR_RUN_ID YOUR_RECORDING_SHA256
```

Replace both identity/hash arguments with the actual manifest values; do not
copy example values into a run. The source must be the explicitly named
`ats_history_<run_id>` database. The destination comes from application settings
and must not be another historical database. Local OS access and database
credentials authorize this owner operation; no HTTP database-URL input exists.
The source settings file may contain the same permitted execution settings as
the launcher, but only its database URL is used for the read connection.

Publication verifies terminal status, simulated designation, expected recording
hash, historical audit integrity and the bound report digest. It atomically
copies only backtest catalog rows and the historical report audit chain, then
records a separate publication audit with the source database alias and audit
head. It never copies orders, fills, positions, journals, active risk configuration, latches,
sessions, broker accounts or market tables into the destination trading account.
All original trade lineage remains in the source database: retain it as evidence;
the published catalog is not a complete source-account backup.

Identical publication is idempotent after rechecking destination integrity.
Identity conflicts, tampered results/audits, wrong recordings, unfinished runs
and missing audit bindings are rejected without overwriting anything. An audit
write failure rolls back the entire publication. Older runs lacking the new
digest must be replayed under a new identity with reviewed inputs; a digest is
never retroactively invented. Exceptions report a sanitized class and nonzero
exit status. Hash verification is not independent external certification or
proof against a privileged owner rewriting an entire source database.

## Server-owned launch plans

Apply migration `0008_historical_jobs` to the application catalog and each empty,
dedicated historical database before use. PostgreSQL execution of this migration
still requires environment-specific validation. Set `HISTORICAL_PLANS_FILE` to
an owner-controlled JSON registry (blank disables launch). Its structure is:

```json
{"plans":[{"id":"research01","label":"Owner research plan","manifest":"manifest.json","recording":"recording.json","settings":"settings.json","timeout_seconds":3600}]}
```

This is a configuration example, not market data or a runnable result. Supply
the actual validated manifest, recording and explicit capital/database settings
described above. All three files must be within the registry directory; absolute
paths and traversal/symlink escapes are rejected. The manifest run ID must equal
the plan ID and the database must be named `ats_history_<plan_id>`. Protect these
files with owner-only OS permissions; settings can contain database credentials.
Validated inputs are copied to private temporary process inputs for each launch.

Owner-authenticated APIs:
- `GET /api/v1/historical-jobs/plans`: identity and label only.
- `POST /api/v1/historical-jobs`: `plan_id`, a research `reason`, and exact
  `confirmation` of `RUN HISTORICAL PAPER`. No path/URL/manifest body is accepted.
- `GET /api/v1/historical-jobs`: latest 200 durable states and actual progress.

The dashboard polls job state every five seconds and published reports every
ten seconds. A durable unique slot permits one historical job across controllers.
The audit and reservation commit before process launch. Duplicate IDs never
relaunch; choose a reviewed new identity and new empty source database for another
experiment. The child uses the existing shared PAPER worker and report publisher,
not the serving process's clock, account or gates. Timeout/shutdown kills and
reaps the owned child before releasing its slot. Failed setup is never presented
as a completed report; failure details are sanitized exception classes.

After abrupt controller death, a reserved job is **UNVERIFIED_OWNER_REVIEW_REQUIRED**.
The server cannot prove whether its old child is alive and does not automatically
clear, retry or adopt it. Preserve its source database/audit and verify OS process
state before operator recovery. An authenticated recovery/reset workflow is not
yet implemented; do not manually clear reservations while a child might run.
Graceful shutdown records INTERRUPTED. A publication interrupted around commit
may require owner inspection of the existing catalog and idempotent CLI publication.
Only locally owned active tasks are reported CONFIRMED_LOCAL; that is controller
liveness, not external market validation or a successful trade.

## Reproducibility evidence

Newly finalized runs store version-1 input, outcome, metric and trade SHA-256
fingerprints in their final historical audit event. The existing detail API and
Backtests view expose those recorded hashes; legacy/unfinished runs show
UNAVAILABLE, not retroactively invented evidence. Original run-local IDs and the
complete lineage remain in the source database and catalog integrity digest.

The economic fingerprint excludes only run-local IDs, result persistence
timestamps and lineage reference arrays; it retains the number of linked trades
and open positions. Trade economic rows are canonically sorted with duplicates
preserved. It includes all stored metrics, curves, sample values, rejection
counts, actual prices/quantities/fees, outcomes and descriptive limitations.
The separate input fingerprint includes strategy version, seed, recording hash,
manifest (except run ID), execution assumptions and risk limits. This is not a
signature, independent market validation or an assertion of profitable behavior.

The isolated-process regression runs the actual shared worker repeatedly in
separate empty databases. Identical inputs yield identical economic fingerprints.
An unavailable future candle changes the recording/input fingerprint but not
the earlier trade or outcomes. A strict instrument concentration limit creates
a persisted deterministic risk rejection and no order. An explicit synthetic
fee change alters actual net results by its hand-computed amount. These fixture
tariffs are tests only, not current Indian broker charges. CASH/MIS and narrowly
authorized NSE/MIS single-long-option runs use the costed historical path;
see `docs/HISTORICAL_OPTIONS.md`. Broader product costs, general parameter
searches and statistical validation remain pending.

Full sampled-session parity now compares the historical runner with direct PAPER
worker cycling against the same recorded provider. Signals, sizing, risk,
orders, fills, FIFO links, costs, journal and session transitions match after
normalizing only run-local fill identifiers. This is deterministic software
evidence, not continuous live-market or profitability evidence.

Deliberately adversarial entry implementations attempt next-bar indexing,
undeclared future inputs and a future-dated signal through the actual strategy
engine. Each yields an audited rejection, no order and an INCOMPLETE historical
report. Indexing violations have the explicit STRATEGY_DATA_BOUNDARY_VIOLATION
reason; other invalid output uses the existing invalid-input/output reason.
These tests verify declared application data boundaries, not a security sandbox
against arbitrary hostile Python code or its filesystem/network access.

The chronological owner-controlled walk-forward path is now described in
`docs/WALK_FORWARD.md`. Its OOS-only reset-account reports are distinct from
continuous-portfolio returns and from statistical/LIVE validation, which remain
unverified.

## Owner cancellation

The existing Backtests controls offer cancellation only for confirmed local tasks.
Supply a reason and type `CANCEL HISTORICAL RESEARCH`. Authenticated endpoints:

- `POST /api/v1/historical-jobs/{id}/cancel`
- `POST /api/v1/historical-jobs/experiments/{id}/cancel`

Requests are audited before cancellation. The response waits for local child
termination and durable terminal state; repeated requests do not cancel twice.
If completion wins the race, the original terminal result is retained rather
than relabelled interrupted. Cancelling an experiment stops its active child;
individual walk-forward child controls are deliberately refused. Parent
cancellation also settles an in-flight child reservation before reaping that
child, so a cancelled parent does not detach a running process.

Owner cancellation records `OWNER_CANCELLED`; controller shutdown remains
`CONTROLLER_STOPPED`. Interrupted research is not published as completed research.
Already completed training/OOS children retain their original evidence, not a
fictional aggregate completion. A disconnected HTTP caller does not discard the
controller's cleanup task. Audit/cleanup failures do not claim cancellation or
silently free unresolved reservations. Unknown-owner jobs remain reserved with
`UNVERIFIED_OWNER_REVIEW_REQUIRED`; this control is not orphan recovery or proof
that an old process has stopped. It never cancels trading/broker orders.
