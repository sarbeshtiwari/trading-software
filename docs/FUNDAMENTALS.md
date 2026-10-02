# Point-in-time fundamentals and corporate calendars

Groww fundamentals are not available in this design. ManualJSONSource and
ManualCSVSource validate supplied evidence; FundamentalStore imports it into an
immutable version ledger. No missing metric, company statement or event is invented.

JSON is an array of records with instrument_id, source, known_at (aware timestamp),
period_end (date), and metrics. Each non-null metric contains value and as_of
(aware timestamp). CSV requires the four metadata columns, then metric columns
such as pe_ratio and pe_ratio_as_of. A blank metric is unavailable; a nonblank
metric without its timestamp is rejected. Allowed metric names are defined in
`app/analysis/fundamental/source.py`. Imports validate the entire batch and run
within one transaction. Instruments must already exist in the instrument master.

Each import supplies max_age. Identical retries return the original ids;
conflicting same-source/same-timestamp records fail. Corrections use a later
known_at timestamp, including within the same calendar day. Queries require a
source (no arbitrary mixing of vendors), as_of and max_age, and filter both known
and received times. Metric-level freshness is checked individually, logged, and
stale values are excluded from scores. Old revisions remain queryable; late
ingestion cannot change what was known earlier.

The legacy date-only fundamentals table remains untouched. Migration 0005 adds
fundamental_versions and corporate_calendar_versions to support intraday revisions
without rewriting the baseline schema. Every fundamental version stores its
initial score, component contributions, formula version and freshness policy.

Read-only API: `GET /api/v1/fundamentals/{instrument_id}?source=...`.
Optional as_of must be timezone-aware and cannot be future; max_age_days defaults
to 365 for this research endpoint. The response retains exclusions, evidence and
Decimal strings. Missing values are null, including missing ratios. The trading
context must apply its declared strategy freshness policy independently.

Valuation metrics are ingested, not inferred. Growth is current/prior - 1 when
prior is positive; negative/zero bases are unavailable. ROE, ROCE, operating and
net margins use net_income/equity, ebit/capital_employed, operating_profit/revenue
and net_income/revenue. Ratios, growth, yields and pledge are fractions, not percent.

BALANCE_HEALTH_V1 averages four binary components: debt/equity <= 1,
interest coverage >= 3, current ratio >= 1, promoter pledge == 0.
QUALITY_HEALTH_V1 averages balance health, positive revenue growth and positive
ROE. These are transparent research heuristics, not risk limits, credit ratings
or trading recommendations. Missing components make the aggregate unavailable;
no missing value receives a favourable or unfavourable invented score.

CorporateCalendarStore imports a strict JSON calendar snapshot with instrument_id,
symbol, source, known_at, coverage_start/end and events (id, kind, aware at timestamp).
It preserves revised/cancelled event sets. Calendar queries are point-in-time and
coverage-bounded. Results/board meetings become high-impact windows for EQ-007;
dividends/splits/bonuses and other documented types are retained as events. Blackout
coverage shrinks conservatively to account for before/after windows, so an unknown
event just beyond source coverage cannot be silently treated as no event.

FUND-007 still needs strategy-context opt-in enforcement in P3. No automatic vendor
fetch, corporate-action price adjustment, trade generation, real Groww validation
or profitable strategy is claimed. SQLite migrations and synthetic evidence are
tested; PostgreSQL runtime validation remains unavailable.
