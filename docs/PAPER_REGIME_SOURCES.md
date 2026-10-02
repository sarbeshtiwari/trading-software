# Provider-backed PAPER regime inputs

The existing reference worker can obtain breadth and implied volatility through
the existing `MarketDataProvider.get_ohlc` and `get_option_chain` interfaces.
There is no generated production data or replacement broker adapter.

In an authenticated reference-input publication, set `regime_source.breadth`
and `regime_source.implied_volatility` to null and provide
`regime_source.provider_source` with:

- `source`: owner-attested constituent/underlying mapping provenance;
- `known_at`, `valid_from`, `valid_until`: aware timestamps, already known,
  with a validity window no longer than one day;
- `breadth_instrument_ids`: explicit unique CASH/EQUITY instrument IDs;
- `option_underlying` and `option_expiry`: the declared chain to request.

Manual observations and this producer cannot be mixed in one publication.
The existing event calendar remains independently required; neither its dates
nor the constituent declaration are automatically renewed. Instruments must
exist and be active, but this does not assert that they are tradable candidates.
The owner declares the historical universe; the application does not infer an
index's point-in-time constituents from today's instrument database.

## Definitions and rejection behavior

Breadth is the number of constituents whose current close exceeds their previous
close divided by **all declared constituents**, including unchanged ones. It is
not an assertion of complete NSE/BSE breadth. Every constituent must return a
valid, fresh, same-origin OHLC observation. Partial responses, duplicate market
identities, missing previous close and malformed prices fail closed.

IV is the arithmetic mean of call and put IV at the strike nearest spot, choosing
the lower strike on a tie. It uses the existing chain model's percentage units.
It is not India VIX and does not substitute a different strike or expiry to find
convenient values. Both ATM IV values and their original Greek timestamps are
required. Missing ATM IV remains unavailable and produces an UNKNOWN regime;
stale/malformed/mismatched sources trigger the existing audited regime-refresh
failure and worker-review path. No old manual observation is used as fallback.

The conservative observation timestamp is the oldest contributing source time.
Collection time records availability, not a renewed observation. Full sanitized
OHLC/chain evidence, the declared policy and method names are stored in the
existing regime-input audit chain. Regime history retains policy expiry, enforced
by the shared freshness gate when strategies, decisions and preflight consume it.
Risk-reducing exits retain their existing independent safety path.

## Verified scope

`backend/tests/integration/test_regime_sources.py` drives the actual worker with
isolated provider fixtures. It verifies a costed entry/exit and second entry more
than one evidence TTL later, newly ingested equity/index candles, fresh breadth
and IV, unchanged source declarations, persisted lineage and API-visible regime.
Negative cases cover missing constituents, stale/future observations, provenance
mismatch, stale Greeks and missing IV. Existing browser lifecycle tests remain
separate integration evidence.

The same worker scenario also runs with a fresh executor/worker restored while
the first position is open. It supplies a new post-entry provider quote, resumes
monitoring without another entry, exits and subsequently takes the second signal
from the unchanged persisted declarations. Reusing a pre-entry quote for this
exit-state check is correctly rejected; the fixture must supply an actual new
observation rather than relax the chronology gate.

`test_provider_session.py` additionally drives this production path to square-off
and EOD. A stale exit quote leaves the position open and an unresolved exit order;
restart plus a fresh recorded fixture quote closes it without another entry.
The worker-review latch remains active into the next trading session. Successful
EOD cycles expose actual open-MIS/nonterminal-order counts, not an old strategy
approval message. Phase/failure transitions are audited without duplicating the
same transition on every heartbeat. This is boundary/recovery acceptance, not a
continuous real-market full-day validation certificate.

This is not external feed verification, a full-session/restart certificate,
profitability evidence or Groww LIVE validation. A provider without real OHLC
or option-chain data must remain unavailable. No subscription is provisioned
automatically and no credentials are fabricated.
