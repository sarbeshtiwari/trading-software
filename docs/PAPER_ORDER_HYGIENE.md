# PAPER pending-order hygiene

`PAPER_ENTRY_MAX_AGE_SECONDS` defaults to 300 and must be between 1 and 86400.
The worker measures age from persisted submission time (creation time only when
never submitted). The setting is captured in historical execution configuration
so replay cannot silently change expiry policy.

At the age boundary, remaining ENTRY quantity is cancelled through the existing
PAPER execution service. At cutoff all non-terminal ENTRY orders are considered.
Existing fills remain real simulated fills; cancellation never erases their
positions or fabricates a trade close. Protective EXIT orders are not age-swept.
Cutoff continues attempting position exits even if an entry cancellation fails.

Each attempt records an immutable intent before the broker call and an observed
result afterwards. Unresolved outcomes block entries and require reconciliation;
the worker's persistent error latch still needs owner review. A broker error is
not labelled cancellation success. A cancellation recovered from broker state
does not resubmit an order. On restart an interrupted intent is completed even
when cancellation already made the local order terminal.

Inspect a trade, order or proposal in the existing Audit page for
`ENTRY_CANCEL_INTENT` and `ENTRY_CANCEL_RESULT`. The result and an immutable
`ORDER_ACTION` journal commit together. Filter this kind in Journal; it is not a
closed trade and has no fabricated P&L, exit price or closed timestamp. Seal
failure rolls back both records, leaving the intent recoverable. Owner notes are
allowed; trade/rejection correction fields do not apply to order actions.

Monitoring session summaries expose verified recorded cancellation outcomes,
unresolved order IDs and recorded protection failures. Legacy/missed-session
hygiene evidence is unavailable, not an invented success. No recorded failures
does not prove protection, and the report does not claim full-session coverage.
Generalized limit/live order support remains pending.
Groww LIVE execution remains unverified.
