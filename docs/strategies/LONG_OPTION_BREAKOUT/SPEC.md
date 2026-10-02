# Long-option breakout v1

**Status: STRATEGY NOT APPROVED FOR LIVE TRADING.** This is an unvalidated
deterministic hypothesis. Registration is disabled by default. The production
OMS accepts only independently authorized single-long-option PAPER entries:
a risk-approved candidate alone does not authorize an order.

## Hypothesis and universe

An option premium breaking its previous twenty closed one-minute highs above
SMA20 might continue during an underlying TRENDING_UP regime. This hypothesis
does not establish profitability or likelihood of reaching its target.

The registered universe contains exactly one catalog option, not a guessed symbol
or expiry. The existing worker resolves the instrument and refuses a mismatched
instrument class. Owner-published contract policy selects the minimum calendar
days to expiry; expired, restricted, inactive or unsupported contracts stand down.

## Inputs and entry

Declared strategy inputs are closed candles, indicators and regime. The existing
ingestion/strategy engine supplies twenty-one contiguous validated closed candles,
SMA20, ATR14 and a persisted regime. Entry requires the latest close strictly above
the preceding twenty highs and SMA20, positive ATR and a tick-aligned entry.

Separately, the application requires real provider-interface option observations,
complete source-backed Greeks, an explicit premium tariff and a valid published
contract policy. Origin, timestamp, session, enablement, event and ban gates remain
authoritative. These inputs cannot be supplied by the strategy as broker permission.

## Exit specification

- Initial stop: two ATR below entry, rounded downward to the instrument tick.
- Fixed target: four times entry premium, representing three premiums of gross
  upside. This fixed research parameter is not calibrated to the validation fixture
  and is not an expected return. The gross threshold leaves room for the separate
  minimum reward/risk test after fees; it does not guarantee acceptance.
- Trailing declaration: one initial stop-risk multiple.
- Maximum holding declaration: one hour.
- Invalidation: a closed candle below SMA20.
- Intraday/MIS and EOD controls remain applicable.

The existing exit monitor reconstructs this distinct strategy ID/specification.
Focused tests now exercise actual option positions, protection, stop exits and
emergency close through the same OMS. Broader product validation remains pending.

## Risk and costs

The strategy proposes LONG only and does not size or submit orders. Risk fraction
comes from owner-configured limits. The shared sizer recomputes quantity in whole
lots and reserves at least the conservative tick-rounded full entry premium plus
costs, not merely the ATR stop distance. Insufficient budget for one lot means no
trade. Independent risk retains its veto over loss, margin, exposure, drawdown,
liquidity and reward/risk. Naked option shorts remain disabled.

The source-dated tariff has explicit OPTION_PREMIUM basis. No current fee rate,
starting capital, quote or fill is invented. The confidence value of one denotes
rule completeness only, not a calibrated probability of profit.

## Validation evidence and missing evidence

Tests use clearly synthetic market-provider fixtures through actual ingestion,
analysis, this reference strategy, proposal validation, fee-aware sizing and risk.
They prove risk-approved candidates pass independent preflight/dispatch, fill,
survive restart and close through the real PAPER OMS with net journal/audit/API/UI
state. Policy revocation, missing Greeks and minimum-DTE violations stand down.
These are integration tests, not paper-trading performance evidence.

Backtest: single-session recorded replay through the actual worker is covered by
synthetic costed fixtures; no real-market performance report is available. See
`docs/HISTORICAL_OPTIONS.md`. OOS/walk-forward: synthetic real-child-process tests
verify training-only selection, strategy binding and isolated net OOS results;
real-market validation evidence remains unavailable.
Time-based PAPER results: unavailable. Performance metrics: unavailable. External
market-data verification and Groww LIVE execution: unverified.

Keep these parameters fixed for chronological OOS/walk-forward evaluation, with
point-in-time option history, costs, slippage and explicit missing-history refusal.
Only subsequent validated evidence can support promotion; this specification and
passing fixtures cannot establish M1/M2 or live eligibility.
