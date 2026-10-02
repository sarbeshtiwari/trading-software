# Historical universe and simulation disclosure

New historical runs persist a structured `universe_disclosure` in their report
assumptions. The actual construction method is `OWNER_DECLARED_STATIC_LISTS`:
an owner supplies the instrument metadata in each input manifest. This is not
an exchange-certified historical constituent universe. The selected strategy
instrument is distinguished from context-only instruments (including indices
and breadth inputs). Each record retains its declared source, knowledge time,
active/restricted flags and window. These flags do not prove historical listing
or eligibility. Listing/delisting coverage and completeness remain unverified;
survivorship and selection bias are possible.

Walk-forward reports retain all frozen child input lists, including training
candidates and OOS inputs, not just the first child's list. Inclusion in this
disclosure does not mean a candidate was selected or a child run completed.
Existing training-only selection and window diagnostics remain authoritative.

The disclosure is included in the catalog digest sealed by the final audit
event. Publication copies that sealed catalog without modifying it. Authenticated
historical detail, trade and sample reads check the report's audit binding and
refuse altered finalized catalogs. This is an internal integrity check, not
external certification. Discovery lists are not evidence-integrity checks.

The existing Backtests page displays the actual stored disclosure, with expandable
window details. Legacy reports without this field show explicit unavailability;
their sealed data is not retroactively rewritten. Historical detail, trade and
sample payloads carry `simulated: true`; this is research, never LIVE performance
or approval.

## Authenticated report export

`GET /api/v1/backtests/{run_id}/export` downloads a versioned JSON report through
the existing Backtests page's **Export simulated report JSON** button. It requires
a finalized, internally audit-bound simulated catalog. Unfinished, unbound or
altered reports are refused, not relabeled as valid. The export includes typed
run metadata, metrics, equity/drawdown curves, closed trades, OOS trade source
links and the stored universe disclosure. It is not a full audit/database backup
and does not export credentials, execution configuration or arbitrary manifest
parameters. Legacy absent disclosure remains null.

The response carries `simulated: true`, a catalog digest and a no-store cache
header. A separate authenticated audit event records the exported bytes' SHA-256,
catalog digest, owner and record counts without changing the sealed source report.
Failure to record that audit event prevents success. More than 10,000 results or
trades, or a response larger than 16 MiB, is refused rather than silently truncated.
Historical discovery, result/trade/sample records, job/plan/launch responses,
OOS reviews and the runner/publication CLI also carry explicit simulation labels.
The controller rejects child outcomes without the simulation marker; discovery
refuses a stored false marker instead of silently replacing it. Error responses
still report failure: a simulation label is never a success or completion claim.

For legacy reports, both detail and export add a general universe warning outside
the sealed source catalog. The stored disclosure remains null and the catalog is
unchanged. This makes the limitation visible without inventing construction
evidence or rewriting old report history.

PAPER remains the default. This change does not verify Groww, deployed PostgreSQL,
historical source accuracy, profitability, M1 or M2.

## Captured data-window policy

New runs capture `min_backtest_days` with their historical execution settings.
Its existing default is 60 positive calendar days; an owner can supply a different
positive value in the isolated settings file. Changing it after preparation is
refused like other captured settings. This is a research warning threshold, not
proof of confidence, a strategy approval or a bypass of trading risk controls.

The sealed `window_disclosure` records exact requested start/end instants, elapsed
seconds and the captured minimum. `BELOW_MINIMUM` identifies a short window;
`SPAN_MEETS_MINIMUM` means only that elapsed calendar time meets the threshold.
Neither state certifies continuous exchange-session data or statistical adequacy.
Duration uses UTC instants, not local wall-clock subtraction across DST.

Publication counts and first/last timestamps refer only to the selected strategy
instrument's recorded snapshots and nominally closed candles available inside
that window. Context instruments, later publications and earlier warm-up
publications do not inflate the count. Missing observations produce zero count
and unavailable timestamps, never invented coverage. Counts are not measures of
unique trading sessions, independent samples, liquidity or data completeness.

The authenticated detail/export API and existing Backtests page expose these
stored values. OOS parents carry only the selected OOS children's captured
windows, not a fabricated continuous span or a sum of training history. Legacy
reports without the field remain unavailable; current settings are never applied
retroactively to their sealed contents. The project's Groww intraday-history
design limitation is three months plus available local archives; actual external
provider coverage and archived-data sufficiency remain unverified.
