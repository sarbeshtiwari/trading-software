# Recorded long-option replay

The existing isolated historical runner supports the `long-option-breakout`
hypothesis for one NSE/MIS long-option contract in one Indian trading session.
It uses the same recorded provider, ingestion, analysis, strategy, proposal,
sizing, risk, preflight, PAPER OMS, protection, FIFO, costs and journal services
as the application. There is no parallel derivative simulator.

## Required inputs

Use the existing historical manifest, recording bundle and owner plan/launcher.
In addition to the ordinary index/regime/calendar/risk inputs, provide:

- Point-in-time instrument metadata: FNO/OPTION, NSE, exact symbol, underlying,
  expiry date, positive strike, CE/PE, lot size and tick size.
- `reference_inputs.contract_source` with kind `LONG_OPTION`, REPLAY origin,
  explicit minimum DTE, known/effective dates and fee-risk reserve. Do not supply
  an arbitrary replacement for independently produced contract costs.
- An explicitly dated FNO/MIS fee schedule with `OPTION_PREMIUM` charge basis.
  Tariff rates and capital are owner inputs, never invented defaults.
- `option_ban_report`: the existing `BanAdmission` schema, REPLAY origin,
  original source document, known-at time no later than run start, review reason
  and explicit confirmation. The report must cover that trading date. An empty
  ban list must be supported by the supplied document, not assumed.
- Recorded option candles and quotes plus matching underlying/expiry chain
  snapshots available by run start. Provider Greeks and exact contract evidence
  are independently checked by the worker and execution pipeline at use time.

The initial chain requirement is deliberately conservative: a run beginning
before the first matching chain publication is refused before account writes,
even if that chain appears later in the recording. Missing/future-only/wrong-
expiry history raises `OPTION_CHAIN_HISTORY_UNAVAILABLE`. Later stale or missing
evidence stands down through normal safety gates; nothing is interpolated.

## Evidence

`backend/tests/integration/test_historical_options.py` exercises the real runner.
Its explicitly synthetic recording buys four units at 100 and exits at 96:
gross -16, configured estimated charges 11.93, net -27.93. Ending the replay
before the exit observation leaves the position open and the run INCOMPLETE.
Two independent child-process runs yield equal economic fingerprints. Missing
history, banned underlying, missing Greeks and invalid input metadata cannot
produce unsafe orders. These are software fixtures, not performance evidence.

Results use the existing simulated historical catalog/report path. No additional
UI page is introduced. The real Edge historical-browser scenario logs in, launches
the owner plan, waits for isolated execution and sealed publication, displays the
correct strategy/version and net costs, downloads an AUDIT_BOUND simulated report,
and reloads it. It verifies that publication does not import historical orders or
positions into the active PAPER account. OOS validation remains pending; these
synthetic browser fixtures do not establish historical market performance.

## Walk-forward integration

Owner experiment plans may now reference long-option train/OOS children. Each
child retains the same single-session evidence requirements. Input freezing
requires chain history for every child and rejects mixed equity/option strategy
families. The parent report derives its strategy identity from the validated
manifest instead of hardcoding equity attribution.

Real child-process tests select only from training scores before executing the
selected OOS child. In synthetic loss fixtures, the lower-risk two-unit candidate
is selected; replacing only OOS exit observations with a favorable target outcome
does not change that selection. The parent and OOS strategy binding remain
`long-option-breakout`. Reports exclude training trades and preserve independent
window accounts rather than fabricate a continuous return or drawdown series.
Restart/duplicate requests do not execute the experiment twice. These tests are
software integration evidence, not strategy approval or statistical validation.

## Limitations

Futures, spreads, naked shorts, multi-session option runs, settlement/exercise,
and automatic historical ban-report discovery remain unsupported. Every option
walk-forward child must remain within one session and supply its own dated
evidence; this does not enable overnight settlement or rolling an open position
between window accounts. The actual option OOS browser scenario now verifies
authenticated launch, correct strategy identity, selected OOS-only duration
disclosure, net costs, audit-bound simulated export and reload, in addition to
the existing standalone option-report and equity OOS browser scenarios.

The recorded source, exchange document and tariff are not independently certified
by these fixtures. No profitability, current broker billing, live Groww execution,
time-based PAPER validation, M1 or M2 completion is claimed.
