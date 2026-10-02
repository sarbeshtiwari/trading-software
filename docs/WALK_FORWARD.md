# Chronological walk-forward experiments

This is simulated research through the existing historical child runner, shared
strategy/validation/sizing/risk pipeline and PAPER accounting. It does not grant
strategy approval, certify profitability or verify external market data.

## Owner configuration

Apply migration `0009_walkforward_jobs` to the application catalog and each new,
empty historical source database. PostgreSQL execution remains environment-dependent
and unverified without the PostgreSQL test service. Keep ordinary PAPER mode.

Extend the owner-controlled `HISTORICAL_PLANS_FILE` registry with an `experiments`
array alongside its existing `plans`. Every experiment requires:

- `id` and `label`: a new lowercase alphanumeric identity (8–26 characters) and
  display label. The identity must differ from all child plan identities.
- `start_at`, `end_at`: explicit timezone-aware historical bounds.
- `train_seconds`, `test_seconds`, `step_seconds`: positive integers.
- Optional `embargo_seconds`: nonnegative time between training and testing;
  defaults to zero. This is recorded policy, not automatically optimised.
- `minimum_training_trades`: explicit positive eligibility threshold. It is
  not a statistical-significance or LIVE-approval threshold.
- `objective`: `NET_RETURN`, the currently supported training selection metric.
- `candidates`: one group per generated window. Each group contains objects
  with `name`, `train_plan` and `test_plan`, referencing existing owner plans.

Windows are half-open and generated in UTC. The first training window starts
at `start_at`; its OOS window follows training plus the configured embargo.
Advance by `step_seconds` until a full train/embargo/test window no longer fits.
Incomplete trailing coverage is not invented. Step must be at least the test
duration so OOS decision windows do not overlap. The inclusive child manifest
end must equal its experiment window's exclusive end minus one microsecond.

Limits: 25 windows, eight candidates per window, 50 distinct child identities,
and 100,000 unique recorded observations across an experiment. Candidate names,
risk limits and strategy parameters stay fixed across windows. Currently only
the reference strategy's declared risk fraction may vary between candidates;
other settings/data within each comparison must agree. All child accounts use
the same explicitly configured capital. Broader strategy-family optimisation is
not implemented. Each source database remains `ats_history_<child_plan_id>`.

Metadata and observation consistency are validated before reservation. Shared
candle/publication identities cannot contain contradictory values across input
files, including overlapping warm-up histories. Duplicate child identities,
already-used runs, changed frozen input hashes, unknown plans and window mismatches
fail closed. Owner files/database credentials never come from HTTP request bodies.

## Execution and selection

Use the existing Backtests view's Walk-forward controls with an owner login,
reason and exact `RUN WALK FORWARD` confirmation. The corresponding authenticated
APIs are `/api/v1/historical-jobs/experiments/plans` (GET) and
`/api/v1/historical-jobs/experiments` (GET/POST). The POST body contains only
`plan_id`, `reason`, and `confirmation`.

The controller audits and freezes the specification/input hashes, then executes
all training candidates through the existing bounded historical job controller.
Only completed, costed training reports meeting the explicit trade threshold
enter the selector. It receives typed training scores, never OOS results. Highest
stored net return wins; ties use candidate name deterministically. The selection
and source evidence are committed to audit **before** the selected OOS child
starts. Unselected OOS children do not run. Audit failure or no eligible candidate
halts the experiment without testing candidates opportunistically on OOS data.

The controller does not change risk limits, trading permissions or the serving
process's clock/account. Existing global historical-child reservations still
permit only one child at a time. Another owner job can cause an experiment to
fail closed on a busy slot rather than silently queue or steal execution.

## Reports and recovery

The parent `WALKFORWARD` run appears in the existing Backtests view. It contains
OOS-only trade copies, individual window reports and an `OOS_AGGREGATE` summary.
Training trades are never added to these trade counts or P&L. Each source report
retains its full audit/account lineage. The `wf:<experiment_id>` chain records
configuration, training scores, frozen selection, OOS evidence and controller state.

Aggregate nominal P&L/charges sum independent reset-capital window accounts.
They are **not** a continuous trading portfolio. Continuous-equity return and
drawdown, annualized statistics and aggregate curves therefore remain UNAVAILABLE;
per-window recorded metrics remain available. Profitable-window fraction and
candidate-change counts are recorded descriptive stability measures, not forecasts.
Multiple-candidate search counts are retained. An optional owner-specified
`degradation: {"max_net_return_drop": "0.005"}` compares selected training net
return minus OOS net return, in return fractions. This example means a 0.5
percentage-point drop, not a recommended threshold. A strictly larger drop is
flagged; absent policy yields UNAVAILABLE, never a silently invented threshold.
The report exposes candidate changes, absolute risk-fraction drift, profitable
window fraction and maximum within-window drawdown. These are descriptive
diagnostics, not statistical proof of overfitting or LIVE approval.

Completion validates each window's trade count, timestamps and gross/charges/net
accounting. The aggregate, completed status and final audit digest commit in one
transaction. Final audit failure leaves a failed experiment, not a completed
report. Authenticated detail/trade/sample reads check completed parent reports
against that audit chain and digest; tampering returns an integrity error.
Older reports without a completion digest are labelled LEGACY_UNBOUND rather
than retroactively certified. Trade API rows include window and original
source-run/trade identifiers. AUDIT_BOUND means internal consistency only.
An optional `validation` object on the experiment declares research thresholds
before execution: `minimum_trades`, `minimum_windows`, `minimum_expectancy`
(net currency per closed trade), `maximum_within_window_drawdown` (fraction),
and `minimum_profitable_window_fraction` (fraction). All values are explicit;
there are no inferred defaults. They are frozen with the audited experiment.
Equality passes the stated boundary; missing/nonfinite metrics or absent policy
yield UNAVAILABLE. Maximum within-window drawdown does not purport to measure
continuous-portfolio drawdown across reset accounts.

The existing Backtests view displays checks and lets an authenticated owner
record a reasoned review. `POST /api/v1/backtests/{id}/review` accepts only
`reason` and `confirmation: "REVIEW OOS EVIDENCE"`, never replacement thresholds.
It requires an audit-bound completed report and atomically records the policy,
outcome, actor and report digest on the separate `validation:<id>` audit chain.
Audit failure does not return a successful review. The read API exposes the same
deterministic checks without creating an audit event on every polling refresh.

PASSED describes research thresholds only: `live_approved` remains false, and
the response lists outstanding version-binding, verified backtest/external-data,
time-based PAPER and LIVE-arming blockers. An experiment can select different
parameter candidates, so its aggregate is not approval evidence for one fixed
strategy version. Full approval/version binding and real PAPER evidence gates
remain pending; the existing LIVE hard block is unchanged.

New historical runs record the actual registered `StrategySpec` and its
canonical parameter hash before execution. They are included in the finalized
report digest and economic input fingerprint. OOS copies retain that binding;
aggregate diagnostics distinguish FIXED (one identical specification), MIXED
(different parameter hashes), and UNAVAILABLE (missing legacy evidence).
The same version label alone is insufficient: isolated candidate runs may share
it while deliberately using different risk fractions. Missing legacy bindings
are not reconstructed from today's implementation. FIXED is still not approval,
external validation, or a substitute for execution settings/market provenance.

Graceful shutdown cancels/reaps the owned historical child before releasing its
reservation and records INTERRUPTED. Abrupt process death leaves the durable
experiment reservation UNVERIFIED_OWNER_REVIEW_REQUIRED; no automatic replay,
adoption or clearing occurs. Duplicate experiment requests return existing state
instead of rerunning. Operator orphan-recovery controls remain pending. Retain
all child databases and owner input files as evidence.

The two-window integrated fixture selects different training candidates, then
records an OOS stop loss and target exit. The browser displays both OOS trades,
their costed aggregate and the configured degradation flag, not training gains.
These deterministic fixtures exist only in tests
and do not constitute real-market OOS validation or time-based PAPER evidence.
