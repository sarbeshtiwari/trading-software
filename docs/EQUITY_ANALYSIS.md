# Equity analysis

Equity evidence is an explicitly sourced, timestamped snapshot. It includes
membership, sector/industry, restrictions, F&O eligibility, a contemporaneous
quote, and closed daily bars. Historical snapshots are immutable through the
store API; revisions create new knowledge timestamps. Reads filter knowledge
and local receipt timestamps and separate data origins. A live instrument master
is not treated as a historical constituent list. No constituent/sector data is
shipped or invented; absent evidence remains unavailable.

Universe resolution records an IST session date, configuration, accepted symbols
and every rejection reason. It checks exchange, membership, activity/restrictions,
price bounds, optional F&O eligibility, sector coverage, and intended-position
liquidity. Missing intended quantity prevents inclusion. Each invocation uses
stored evidence available at its decision time. A daily engine scheduling hook
is still required; EQ-001 therefore remains in progress.

Average daily traded value uses **supplied actual turnover**, not close times
volume disguised as turnover. Missing turnover is unavailable. The minimum
turnover, maximum position-notional/turnover participation, spread, quote age and
history age are caller-configured. Passing this screen does not guarantee an
exit fill or substitute for execution-time depth/risk checks.

Relative strength is `(stock_end/stock_start)/(index_end/index_start)-1`.
Ranking ties sort by instrument key. Histories must have identical closed session
timestamps; missing sessions are not bridged silently. Volatility uses existing
Wilder ATR%, annualised population log-return volatility, and covariance of
simple stock/index returns divided by index return variance. Zero index variance
produces unavailable beta. Daily refresh wiring for profiles is still pending,
so EQ-005 remains in progress despite passing mathematical tests.

Gap analysis uses a current-session pre-open observation, falling back to an
explicit official-open observation. Both require freshness. The caller provides
the expected prior trading-session date; a missing prior close makes the gap
unavailable. Current LTP is not substituted for an opening price. Candidates sort
by absolute gap and instrument key.

Event blackout context reuses the sourced event calendar's explicit windows and
coverage. It records BLACKOUT/CLEAR/UNAVAILABLE, evidence ids, and affected strategy
ids. No calendar means UNAVAILABLE. EQ-007 remains in progress until risk and AI
context consumers are connected. External corporate-calendar ingestion is FUND-006.

Migration 0004 creates equity evidence storage; SQLite upgrade/rollback and
metadata equivalence are tested by the migration suite. PostgreSQL runtime and
Groww live verification remain unverified. PAPER remains the default.
