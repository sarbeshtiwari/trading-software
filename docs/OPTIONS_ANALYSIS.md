# Option contract and payoff analysis

OPT-001 through OPT-005 are analysis components, not an order-entry path.

Contract validation preserves lot/tick sizes and rejects inactive or restricted
instrument records. Moneyness is exact against spot: ATM means equal to strike;
the ATM selector instead means the nearest listed strike (lower on ties).
Selection never assumes that a listed contract is liquid. OI, volume and absolute
and relative spread limits are caller-supplied policy; missing inputs fail the
screen. Bid/ask must align to the contract tick. A requested ATM/OTM strike that
fails screening is unavailable, rather than silently replaced by another strike.
Delta selection uses signed, timestamped Greeks; premium selection targets LTP.
These are candidate-selection helpers, not executable fill-price estimates.

Expiry selection uses actual contract metadata with an IST calendar-day minimum
DTE and a 15:30 IST expiry cutoff. Weekly/monthly selection requires explicit
metadata; unknown frequency is unavailable, never inferred from a weekday.
The current instrument loader does not populate expiry-frequency metadata, so
weekly/monthly selection requires an explicitly supplied classification. Historical
callers must supply the instrument master known at their decision time; the current
live resolver is not a point-in-time instrument-master archive.

Payoffs use Decimal arithmetic, premium paid/received, signed lot quantities,
and non-negative terminal underlying prices. Summaries calculate exact extrema
at all strikes and zero, plus the final slope, to distinguish unlimited profit
or loss from finite bounds. Break-even intervals are retained for flat zero
payoff regions. Results are gross: fees, taxes, slippage and physical settlement
obligations belong to subsequent cost, execution and risk components.

Builders support long call/put, covered call, protective put, bull/bear call and
put verticals, long straddle, long strangle and iron condor. Input contracts are
ordered by increasing strike (straddle: call then put at equal strike; condor:
put/put/call/call). Directions are generated, not trusted from user input.
All option legs must share underlying, exchange and expiry, and equal units.
Covered call/protective put require matching underlying share quantities.
There is no naked-short builder. These checks do not prove account coverage:
the independent risk engine must verify actual holdings and concurrent orders.

OPT-008 is only partially implemented: the tested liquidity screen is used by
selection, but the future execution pipeline must enforce it before every order.
The existing `allow_naked_short_options=False` setting is unchanged; risk-engine
enforcement (OPT-006) remains pending. No live execution or profitability is claimed.
