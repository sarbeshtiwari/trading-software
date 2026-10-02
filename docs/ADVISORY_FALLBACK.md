# Rule-derived PAPER proposal receipts

The initial local-only checkpoint below now has an opt-in Claude review extension;
see [CLAUDE_ADVISORY.md](CLAUDE_ADVISORY.md) for the actual enabling conditions,
durable budget/circuit controls, limitations and current verification scope.
Without explicit enablement, this local-only behavior remains the default.

The reference worker now archives its quantitative signal-to-proposal conversion
through `DeterministicFallbackProvider`. This is a local rule mapping, **not a
Claude call, new strategy, independent research, or order authorization**.

The provider consumes only the existing typed signal, instrument lot size,
evidence references and strategy exit rules. It preserves direction, entry,
stop, first target and quantitative confidence. Its advisory quantity is one
instrument lot; the existing shared sizer recomputes quantity. The shared
proposal validator resolves actual persisted evidence and checks freshness;
the risk engine, safety latches and execution preflight remain mandatory.

The reference strategy remains QUANT. `llm_available` stays false. Strategies
declaring an LLM dependency still stand down. Configuring a Claude key does not
silently enable paid calls in this reference path. Provider selection, remote
Claude review, budget reservations, circuit breaking and bounded remote repair
now have an explicit opt-in path documented separately, not implied by local
fallback receipts.

Each eligible reference signal gets a durable `llm_calls` receipt and hash-chained
`ADVISORY_PROPOSAL` event before downstream validation. The receipt contains a
versioned rule policy hash, redacted request hash and snapshot, schema-valid
redacted response, model identifier, outcome and measured local latency. Local
cost is genuinely zero; token counts and temperature are not applicable and are
NULL, never fabricated remote usage. Call/proposal linkage commits with proposal,
sizing and risk records. Validation rejections remain traceable through their
shared cycle ID even when no proposal exists. A persisted receipt without a
proposal is **not** approval or execution; it can also represent interruption.
Storage failure prevents returning an approved decision.

The authenticated workspace API returns bounded, current-mode, point-in-time
receipt metadata, not raw prompts. Existing Decisions-page polling displays it.
UTC storage is explicit, including under SQLite. Restarted workers retain the
same receipts and existing cycle claims prevent replay.

Evidence: `tests/unit/test_fallback.py`, `tests/integration/test_fallback.py`,
the actual lifecycle/restart cases in `tests/integration/test_reference_worker.py`,
and the actual browser scenario in `tests/integration/test_paper_browser.py`.
Only external market data is a fixture in the worker lifecycle test. Shared
analysis, strategy, validation, sizing, risk, OMS, fills, costs, journal and API
services run. No external LLM, market service, broker or delivery verification is
claimed. LLM-004/008/011/012 and AUDIT-005 remain partial until broader provider
integration and their full acceptance criteria are verified.
