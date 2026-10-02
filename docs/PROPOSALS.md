# Advisory proposal validation

The `TradeProposal` top-level fields exactly match the requirement contract.
Evidence entries are typed database references (EQUITY, NEWS or FUNDAMENTAL).
Prices are finite positive Decimals, quantity is a strict positive integer, and
confidence lies in [0,1]. Unknown fields are rejected as control-tamper attempts;
logs include the reason, never raw model output or external text.

The validator checks the instrument master, active/restricted/expired/index
state, tick/lot alignment, protective stop and target direction, configured entry
deviation, ATR stop bounds and confidence. Decisions use timezone-aware timestamps
and expiry dates in IST. A current instrument row revised after a historical
decision is unavailable, not silently treated as historical membership.

Every referenced source must exist, match the instrument and be available by
decision time. Equity references also enforce origin and payload freshness; manual
fundamental versions enforce known/receipt times. News requires verified status,
matching entities, no conflict and no later revision. Headlines/body/thesis are
inert data, not executable instructions. Citation resolution proves source
availability, not semantic truth of every free-text claim; NEWS grounding and
immutable interpretation history are separate pending work.

This component does not execute orders, call brokers, select quantities or change
risk limits. Import-graph tests check transitive agent dependencies. Quantity is
still advisory: the shared quantitative/LLM pipeline replaces it with independent
sizing before risk. Broker-boundary verification remains pending. Confidence drops
are persisted in the negative-decision ledger and audit chain. No PAPER end-to-end,
LLM connectivity, or live execution verification is claimed.
