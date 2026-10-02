# Option-chain analysis

Requirements OC-001 through OC-008 reuse the broker-neutral models in
`backend/app/marketdata/models.py` and the existing snapshot table.

- Max pain minimizes total intrinsic payout weighted by OI over the supplied
  strike ladder. OI must have consistent units. Ties select the lower strike.
- PCR divides put totals by call totals, for OI or volume, over an inclusive
  strike band. Missing legs/counts, empty bands and zero denominators return
  `None` (UNAVAILABLE). Missing observations are never treated as zero.
- Build-up compares price and OI between the same two timestamped snapshots.
  Day-change OI is not combined with an unrelated intraday price change.
- OI support/resistance uses the highest put/call OI respectively, with lower
  strike tie-breaking. Output always carries `heuristic=True`; these are not
  guaranteed market levels or trade triggers.
- IV curves retain missing points without interpolation. IV is in percentage
  points, consistent with existing Greeks. Term structure uses each expiry's
  nearest spot strike, with lower strike tie-breaking. It rejects mixed
  underlyings/origins, duplicate expiries, expired, stale and future observations.

`build_chain_context` is the typed boundary for subsequent strategy and AI
consumers. Its required `as_of` and `max_age` arguments enforce freshness and
expiry at decision time. Raw broker text is excluded from this context.
Strategies and AI prompt integration are still future phases.

`ChainSnapshotStore(cadence=..., clock=...)` records accepted captures in the
existing SQLAlchemy table. Cadence is per underlying/expiry and checked against
persisted timestamps, including after a restart. The future market-data/engine
loop must call `capture` for incoming chains; no background collector is running
at this checkpoint. Capture follows the application's single-writer ownership
contract. Conflicting duplicates and out-of-order writes fail; identical retries
are no-ops. Snapshots preserve the full model, source payload, provenance and
Decimal precision in a versioned JSON envelope.

Historical reads require `as_of` and filter both observation and receipt time.
Data received late cannot influence an earlier decision. No historical option
chains or collection history are manufactured. SQLite integration tests verify
round trips and temporal boundaries; PostgreSQL runtime verification remains
unavailable on this machine. Groww fixtures verify parsing only, not live access.
