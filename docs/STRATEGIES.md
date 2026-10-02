# Strategy contracts and pre-signal gates

Strategies declare a versioned specification and implement entry/exit methods.
The registry revalidates declarations, rejects sentiment/news-only entry paths,
and rejects changed parameters under an existing version. Registrations default
to disabled. PAPER, SUPERVISED and LIVE enablement are stored independently and
exposed read-only at `/api/v1/strategies`. LIVE enablement is unconditionally
blocked until evidence approval and owner arming are implemented.

Signals are advisory: they contain protective stops, profit targets, confidence
and trigger evidence, but no executable quantity or account-control fields.
The engine checks registered specification identity, mode, universe, regime,
input freshness/provenance, optional calendar dependency and LLM availability.
Suppression is logged with the supplied regime identifier. Calendar-dependent
strategies fail closed on missing coverage; unrelated strategies may continue.

Context contains only declared inputs. Fundamentals and news are absent unless
explicitly declared. The engine requires observation/availability timestamps and
data origin for every input and checks closed, ordered, fresh candles. Context
payloads must be supplied by trusted point-in-time builders; envelopes are not
proof of the correctness of arbitrary payloads. No raw external text is executed.

The closed-candle breakout reference hypothesis now drives the actual PAPER
worker and isolated recorded historical runs; see
`strategies/CLOSED_CANDLE_BREAKOUT.md`, `PAPER_WORKER.md` and `HISTORICAL_RUNS.md`.
This is software integration evidence, not profitability or LIVE validation.
All five exit algorithms and priority arbitration exist; see EXITS_AND_ARBITRATION.md.
Signals traverse the shared validation/sizing/risk pipeline, PAPER execution,
position monitoring, journal and runtime scheduler. Broader strategy families,
cross-broker execution and external validation remain incomplete.
Declaration validation cannot prove arbitrary Python strategy code follows its
stated predicates; production strategies require implementation review and tests.
Synthetic helper strategies remain isolated in tests. Pipeline decisions
have a persistent hash-chained audit trail; general strategy logs are separate.
Approval, parameter-change workflows and performance monitoring remain pending.

## Product contract

The mandatory `StrategySpec.product` declaration defines holding mode: `MIS` is
intraday, `CNC` is cash delivery, and `NRML` is carry-forward F&O. A quantitative
signal cannot override this declaration. CNC/FNO and NRML/CASH combinations fail
validation, and positional products require the existing explicit
`ALLOW_POSITIONAL=true` owner setting (default false).

PAPER preflight and final dispatch verify the registered specification hash,
complete recorded strategy context, proposal product and final order product.
Changing a database proposal/order from MIS to CNC cannot evade intraday exit
policy. Turning positional permission off after approval prevents submission.
This permission is frozen with historical execution settings for reproducibility.
Product permission alone never authorizes an order or certifies F&O execution;
the current PAPER executor's remaining instrument/mode/risk gates still apply.
