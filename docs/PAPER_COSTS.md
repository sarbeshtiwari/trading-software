# PAPER cost estimates

The CASH/MIS PAPER lifecycle accepts explicit owner-published `FeeSchedule`
evidence through `POST /api/v1/strategies/reference/fees` (also available in the
existing Strategies view). Authentication and a reason are required. No tariff
rates are supplied by the application; use dated, attributable source evidence.
`known_at` must not be in the future and the effective interval must cover the
order/fill. Publication and the exact schedule are auditable.

Brokerage, STT, exchange fees, SEBI fees, IPFT, stamp duty and GST have separate
components. Rates are fractions, not percentages. Brokerage has an explicit
floor, cap and small-trade cap. GST applies to brokerage, exchange, SEBI and
IPFT components. Rounding quanta are explicit. Partial fills charge the difference
between cumulative order estimates, avoiding repeated brokerage minima/caps.

Preflight includes the greater of the declared cost reserve and the estimated
entry plus exit costs at the planned stop/target/entry prices in the deterministic
risk calculation. An over-budget order is rejected, not silently resized or
submitted. The entry tariff is frozen with the order. This is not a guarantee
against gaps, later tariff changes or unbounded market losses.

PAPER snapshots retain fees and their source schedules across restart. Charges
flow into cash, equity, daily-loss input, persisted fills, position totals, the
journal and workspace API. Journal `net_pnl` is available only when every fill
has a schedule; otherwise charges/net remain unavailable. Reported gross P&L
remains separate from estimated charges and estimated net P&L.

This is **not verified broker billing**. Contract-note/day-level rounding,
delivery/DP charges, derivatives/exercise/expiry charges and other segment/product
rules are unfinished. Unsupported or missing tariffs remain unavailable, never
an assertion of zero charges. PNL-003 therefore remains partial.

Primary references for owner tariff evidence and component definitions:
- [Groww pricing](https://groww.in/pricing)
- [Groww charge explanations](https://groww.in/help/stocks/sx-reports/what-are-the-different-charges-mentioned-in-the-report)
- [NSE statutory levies](https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies)

Tests use explicitly synthetic rates, hand-computed component values, two-fill
orders, process reconstruction, idempotent synchronization, actual journal/API
state and rejection when fees breach the risk budget. No external fees or
Groww execution were verified by these tests.
