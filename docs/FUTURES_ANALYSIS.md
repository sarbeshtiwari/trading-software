# Futures analysis

FUT-001…FUT-003 implement analysis, not rollover execution.

Contract resolution uses supplied instrument-master records and their actual
maturities, filtered by exchange, underlying and the 15:30 IST expiry cutoff.
Near/next/far refer to ordered available maturities; no symbol or weekday is
invented. A restricted target is unavailable rather than silently replaced by
another maturity. Lot/tick metadata must be valid. Historical callers must supply
the master known at that time; the current resolver has no historical master archive.

Basis is future minus spot, with fractional basis divided by spot and simple
annualised carry using ACT/365. Calendar spread is far minus near. These are
mathematical helpers: callers must supply synchronous prices available at the
decision timestamp. They do not imply financing, dividends or an arbitrage profit.

Rollover planning validates two same-underlying/exchange contracts in expiry
order, quote age and synchronisation, expiry, minimum volume, maximum spread,
tick size, and quantity divisibility by both lot sizes. It never rounds quantities
silently when lot sizes change. The caller supplies the next contract from the
resolver and all policy thresholds. Outside the configured window, no plan is
generated. A long position exits at bid and enters at ask; a short does the reverse.

All costs are `ESTIMATED`. Spread cost is crossing half the spread on each leg,
multiplied by units. Fees are an explicit input; missing fees make total estimated
cost unavailable. The signed difference between entry and exit reference prices
is labelled a **notional price difference**, not an account cash debit, margin
requirement or realised P&L. Quotes do not establish fill depth or fill certainty.
Execution must independently validate margin, risk, depth, fees and current prices.

No futures margin-monitoring, rollover order execution or live verification is
claimed by these components. Those remain subsequent requirements/phases.
