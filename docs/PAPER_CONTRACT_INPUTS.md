# PAPER contract evidence

The reference worker can derive fresh CASH/EQUITY contract observations from
the ingested quote, the recovered PAPER account's configured margin model and
an explicitly published effective fee schedule. This does not verify Groww
margin, broker charges or external market connectivity.

Publish reference inputs through the authenticated
`POST /api/v1/strategies/reference/inputs` endpoint. Within its `inputs` object,
provide either legacy `costs` or `contract_source`, never both. The latter has:

- `instrument_id` and `data_origin` matching the ingested instrument and provider;
- `source`, identifying the owner's policy/restriction attestation;
- timezone-aware `known_at`, `valid_from` and `valid_until`;
- an explicit nonnegative `risk_cost_reserve_per_unit` floor.

The source must already be known when published. Its validity window is at most
one day and excludes its end timestamp. Existing restriction flags in the
publication are manual attestations bounded by this window, not an automatic
exchange ban/news feed. Publish a new attestation when those inputs change.
Do not extend validity merely to conceal missing external evidence.

Each worker evaluation requires a fresh valid quote, a verified input publication
and an effective point-in-time fee schedule. Missing or expired inputs cause an
audited stand-down. The derivation does not submit any order. Its audit links the
original publication, market snapshot, margin model, tariff and derived values.
Quote observation times and original policy dates are retained.

The shared deterministic sizer estimates round-trip charges for its actual
candidate quantity, including brokerage minima/caps. It conservatively increases
the fee reserve and re-sizes with a bounded convergence loop; it never silently
upsizes to overcome fees. The owner reserve is a floor, not permission to omit
the tariff. Final execution preflight independently rechecks costs and risk.
Policy/tariff expiry is also enforced at submission and final entry dispatch.
Expired entry evidence does not disable risk-reducing exits.

Legacy fixed `costs` inputs retain their existing market-data freshness limit.
Regime IV, breadth and event-calendar evidence retain their independent source
dates and expiry checks. This producer alone does not provide sustained-session
regime refresh, full-session acceptance, real billing or external verification.

Verification: `backend/tests/integration/test_contract_production.py` exercises
two actual worker trades beyond the legacy cost TTL, quantity-dependent fees,
missing tariffs, policy expiry at three execution boundaries, and authenticated
publication. Deterministic market data and tariffs are isolated test fixtures.
