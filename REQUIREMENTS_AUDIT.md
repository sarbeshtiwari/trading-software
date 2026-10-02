# REQUIREMENTS_AUDIT.md

**Audit of:** `PROJECT_REQUIREMENTS.md` v0.1 (533 requirements, 51 areas)
**Audited against:** the execution contract (§1–§18) and the owner's category list
**Date:** 2026-09-17
**Purpose:** identify what is missing, ambiguous, conflicting, externally blocked, legally constrained, or unsafe to automate — *before* implementation starts.

---

## 1. Coverage check against the owner's category list

### Implementation audit addendum (2026-09-20)

Checkpoint f4f4825 passed component tests but had no complete OMS, scheduler or
frontend. Historical `[✓]` entries are not blanket end-to-end proof. Going forward
`[✓]` requires implementation, meaningful tests and integration evidence against
acceptance criteria. PAPER-007 is downgraded to partial because UI/report labelling
was not verified. AUTH-007/SEC-002/SEC-005 now have real API/session tests; connected
React views and authenticated risk controls are partial delivery of the broader
application contract. Full PAPER lifecycle, live Groww and M1/M2 remain unverified.

Every category named by the owner maps to at least one requirement area. No category is unaddressed.

| Owner's category | Covered by | Count |
|---|---|---|
| Application architecture | `ARCH` | 16 |
| Frontend/dashboard | `FE` | 21 |
| Backend | `BE` | 14 |
| Database | `DB` | 20 |
| Groww API integration | `GRW` | 27 |
| Authentication and security | `AUTH`, `SEC` | 18 |
| Real-time market data | `MD` | 11 |
| Historical data | `HD` | 11 |
| NSE/BSE | `EXCH` | 8 |
| Equity analysis | `EQ` | 7 |
| Fundamental analysis | `FUND` | 8 |
| Technical analysis | `TA` | 12 |
| News intelligence and verification | `NEWS` | 12 |
| Market sentiment | `SENT` | 5 |
| Market regime detection | `REG` | 6 |
| Intraday trading | `INTRA` | 7 |
| Futures | `FUT` | 6 |
| Options | `OPT` | 11 |
| Options Greeks | `GRK` | 8 |
| Option-chain analysis | `OC` | 8 |
| F&O risk | `FNOR` | 8 |
| Strategy engine | `STRAT` | 13 |
| AI research agents | `AIR` | 10 |
| AI decision engine | `AID` | 10 |
| Deterministic risk engine | `RISK` | 20 |
| Position sizing | `SIZE` | 9 |
| Order execution | `EXEC` | 14 |
| Order management | `OMS` | 8 |
| Portfolio management | `PORT` | 8 |
| P&L | `PNL` | 8 |
| Trade journal | `JRN` | 7 |
| Backtesting | `BT` | 14 |
| Walk-forward testing | `WF` | 5 |
| Paper trading | `PAPER` | 8 |
| Supervised trading | `SUP` | 7 |
| Live trading | `LIVE` | 10 |
| Notifications | `NOTIF` | 7 |
| Monitoring | `MON` | 9 |
| Error handling | `ERR` | 7 |
| State recovery | `REC` | 9 |
| Emergency controls | `EMG` | 7 |
| Self-learning | `LEARN` | 9 |
| Monthly reports | `RPT` | 7 |
| Compliance | `CMP` | 7 |
| Testing | `TEST` | 26 |
| Deployment | `DEPLOY` | 10 |
| Documentation | `DOC` | 15 |
| *(contract §1 also lists)* Logging | `LOG` | 5 |
| *(contract §13)* Audit everything | `AUDIT` | 7 |
| *(contract §14, owner's LLM brief)* LLM provider layer | `LLM` | 13 |

**Result: 0 uncovered categories.**

---

## 2. Requirements I added that the prompt did not explicitly name

These are safety-motivated additions. Each is listed so you can veto any of them rather than discovering them later.

| ID | Addition | Why I added it |
|---|---|---|
| `SEC-010`, `LLM-006` | Prompt-injection containment for news text | News is untrusted third-party text entering an LLM whose output influences trades. Without containment, a crafted headline is an attack surface. |
| `AIR-008` | Numeric grounding check on agent output | LLMs fabricate plausible numbers. Any price/level in agent output must match its input or be rejected. |
| `AID-004` | LLM quantity is advisory; sizer always recomputes | Your brief says the LLM must not increase size beyond limits. Making quantity *advisory by construction* is stronger than checking it afterwards. |
| `AID-010` | Tamper-attempt detection on proposal fields | Detects and logs an LLM attempting to set `max_daily_loss`-style fields rather than silently ignoring them. |
| `OPT-007` | Multi-leg legging-risk policy with automatic unwind | A half-filled spread is a naked position. This is the most common way option strategies blow up operationally. |
| `EXEC-003` | Stop must be placed or the position is exited/escalated | Groww's API exposes `DAY` validity and plain SL orders only — there is no bracket order to lean on. An unprotected position must never be silently tolerated. |
| `PNL-003` | Full Indian cost model (STT/CTT, exchange, SEBI, stamp, GST) | Intraday and F&O edges are frequently smaller than costs. A gross-P&L backtest is worse than no backtest. |
| `GRK-007` | Explicit time-to-expiry convention | Calendar-vs-trading-day conventions change theta materially near expiry. |
| `HD-010`, `BT-014` | Local intraday archive + data-window warnings | Direct consequence of Groww's 3-month intraday history limit. |
| `CMP-001`, `CMP-002` | Order-rate governor and algo-ID tagging | SEBI's retail-algo framework (see §5). Not optional for live trading. |
| `CMP-007` | Self-trade prevention | Two strategies can otherwise cross with each other. |
| `ARCH-011` | Single-writer lock per account | Two processes trading one account is a silent doubling of position size. |
| `REC-005`, `REC-006` | Orphan and unprotected-position detection on startup | Covers the realistic case where the system was down while a position existed. |

---

## 3. Ambiguities requiring your decision

I have written a **recommended default** for each so implementation is never blocked; anything you do not correct will be implemented as the default and recorded in the amendment log.

| # | Ambiguity | Why it matters | Recommended default |
|---|---|---|---|
| A1 | **Account capital** is unspecified | Every absolute risk limit, the minimum viable lot size for F&O, and whether F&O is even feasible depend on it | Configurable `STARTING_CAPITAL`; no default — required at first boot |
| A2 | **Risk appetite numbers** are unspecified (per-trade risk, daily loss, max drawdown, max exposure) | These are the core of the risk engine | Per-trade 0.5% of capital, daily loss 2%, max drawdown 10% (disarm), gross exposure 3× capital, max 5 concurrent positions — all configurable |
| A3 | **Instrument scope** — cash equity, index futures, stock futures, index options, stock options? | Drives margin, Greeks, expiry handling and universe size | Phase 1: NIFTY/BANKNIFTY index futures + index options (defined-risk only) + NIFTY-50 cash equity. Stock F&O later |
| A4 | **Holding horizon** — intraday only, or overnight/positional? | Overnight adds gap risk, different products (NRML/CNC), different margins | Intraday-first (`MIS`), with positional support built but disabled by default |
| A5 | **Which strategies** — none were specified | Contract §15 requires each strategy to be individually justified and validated | The three reference strategies in `STRAT-005`, pending your approval or replacement |
| A6 | **News sources** — none named | Several good Indian sources are paid; free RSS is lower quality and slower | Start with exchange filings (Tier 1) + free RSS from established financial media; paid vendor pluggable later |
| A7 | **Fundamental data source** — Groww's API does not provide fundamentals | `FUND-*` cannot function without a vendor | Interface built; a manual CSV/JSON loader ships as the default source until you choose a vendor |
| A8 | **Notification channels** | Needs your bot token / SMTP credentials | Telegram primary, SMTP secondary; both disabled until configured |
| A9 | **Where it runs** — workstation or 24/7 VPS? | A laptop that sleeps cannot manage an open position | Docker Compose targeting a small always-on VPS; workstation supported for PAPER |
| A10 | **Is `LLM` required for trading, or advisory only?** | Determines whether an Anthropic outage stops trading | Advisory by default: deterministic strategies trade without it; LLM-dependent strategies stand down (`STRAT-009`) |
| A11 | **Naked short options** | Unbounded risk | Disabled by default, explicit flag + hard cap to enable (`OPT-006`) |
| A12 | **News service criticality** — the contract §10 health list includes the news service, but §11 implies graceful degradation | Determines whether a news outage disables all trading | Non-critical by default: news outage degrades, does not disable. **Please confirm** — this is the one place where I have deliberately softened a §10 item |
| A13 | **AI memory scope and retention** | Unbounded memory becomes noise; bounded memory needs a policy | Memory limited to this system's own journal, retained indefinitely, retrieved by structured similarity only |
| A14 | **Monthly report delivery** | Format and destination unspecified | HTML + PDF archived locally, link pushed to the notification channel |
| A15 | **Multiple strategies on the same instrument** | Position netting vs independent tracking | Netted at broker level, tracked per strategy internally, with arbitration (`STRAT-008`) |

---

## 4. Internal conflicts in the specification

These are genuine tensions in the contract itself. Each has a proposed resolution.

### C1 — §16 ("PARTIAL = 0, NOT IMPLEMENTED = 0") vs §15 (strategies need paper-trading results)

**The conflict:** §15 requires every strategy to have *paper-trading results* before it is approved for live trading. Paper-trading results take **calendar time** — weeks of live sessions. §16 says the project is not complete while anything is partial.

**Consequence:** the project cannot be declared "complete" on the day the code is finished, because a required artefact (paper-trading evidence) does not yet exist and cannot be manufactured.

**Proposed resolution — two distinct milestones:**
- **M1 — Implementation Complete:** every requirement implemented and tested; strategies have backtest + out-of-sample evidence; paper mode running.
- **M2 — Validation Complete:** strategies have accumulated the configured minimum paper-trading sessions and pass the approval gate; LIVE arming becomes possible.

`FINAL_IMPLEMENTATION_AUDIT.md` will report M1 status. It will list paper-trading evidence as **PENDING-BY-DESIGN (time-based)**, not as PARTIAL. **Please confirm you accept this distinction** — it is the only honest way to satisfy both sections.

### C2 — §5 ("no fake implementations") vs no Groww credentials

**Resolution (already directed by you):** the adapter is built completely against the documented contract and verified against fixtures and a local mock server. Every unverified-against-live item is listed in §6 below. No test or document will claim live verification.

### C3 — §6 ("no placeholder UI, real data only") vs missing data vendors

**The conflict:** the fundamentals page and parts of the news page cannot show real data until a data source exists (A6, A7).

**Resolution:** those pages render real data from whatever source is configured and display an explicit "no data source configured" state — a *truthful empty state*, not a placeholder or fake numbers. This satisfies §6's intent (never show invented data) while being honest about the missing dependency.

### C4 — §15 ("do not overengineer") vs a 51-area requirement set

**The conflict:** §15 warns against adding components to look sophisticated; the category list nevertheless mandates AI agents, self-learning, AI memory, sentiment, monthly reports and more.

**Resolution:** every component traces to a requirement ID and a named consumer. `TA-012` enforces this mechanically for indicators (no orphan indicators). I will apply the same rule to strategies, agents and models: if nothing consumes it, it is not built.

### C5 — §14 (AI must not override risk) vs "self-learning"

**Resolution:** learning is asymmetric by design. It may **auto-disable** a degrading strategy (a safe direction) and may **propose** parameter changes; it may never auto-enable a strategy, widen a limit, or apply a parameter change (`LEARN-006`, `LEARN-007`).

### C6 — §8 (end-to-end test through order → position → exit) vs not placing real orders

**Resolution:** the e2e chain runs in PAPER mode with the paper broker, plus a mock-server run of the Groww adapter. A live e2e is explicitly deferred to the first SUPERVISED session with real credentials.

### C7 — §10 health-check list includes "News service" as a startup gate vs §11 graceful degradation

**Resolution:** see A12 — configurable criticality, non-critical by default. Needs your confirmation.

### C8 — §9 requires "positions synchronize successfully" before LIVE vs an account with no positions

**Resolution:** synchronisation success means *the call succeeded and local state matches broker state* — an empty match is a pass. Stated explicitly so an empty account does not block arming.

---

## 5. Groww TradeAPI limitations (verified 2026-09-17)

These are properties of the broker's API, not of the design. Each has a stated mitigation.

| # | Limitation | Impact | Mitigation |
|---|---|---|---|
| G1 | **Intraday history limited to 3 months** (daily/weekly is full history) | Intraday strategies cannot be backtested over multiple years or across several market regimes using Groww data alone | Continuous local archiving from day one (`HD-010`); every intraday backtest report states its data window and warns below the configured minimum (`BT-014`); pluggable alternate vendor interface (`HD-011`) |
| G2 | **Validity is `DAY` only** — no IOC, no GTT documented | No good-till-triggered stop that survives overnight; no immediate-or-cancel execution style | Stops are same-day SL/SL-M orders, re-placed each session, plus a monitored synthetic stop as backup (`EXEC-003`). Overnight positions carry explicit gap risk accounting (`FNOR-006`) |
| G3 | **No bracket/cover order product documented** | Entry and stop are separate orders, so there is a window where a position is unprotected | `EXEC-003` places protection immediately after fill confirmation and escalates if it fails; `REC-006` detects unprotected positions on restart |
| G4 | **Order docs list `exchange: NSE`**; SDK constants also mention BSE and MCX | BSE order routing is not confirmed by the order documentation | Build NSE-first; BSE support behind a capability flag (`GRW-025`) verified only once credentials exist. **Commodities (MCX) are out of scope** unless you ask for them |
| G5 | **API-key + secret flow requires daily approval and caps the token endpoint at 150/24h** | A naive re-auth loop can lock you out of authentication for the day | TOTP flow is the default (no expiry); a hard budget guard blocks the 151st daily token call (`AUTH-003`) |
| G6 | **Order rate limits: 10/s, 250/min** | Bursty exits (e.g. flatten-all across many positions) can hit the limit | Throttle with back-pressure (`EXEC-012`), and a separate compliance governor below the SEBI threshold (`CMP-001`) |
| G7 | **LTP/OHLC batch capped at 50 instruments; feed capped at 1000 subscriptions** | Universe size is bounded by data budget | Transparent chunking (`GRW-017`), priority-based subscription eviction (`GRW-022`) |
| G8 | **No fundamentals API** | `FUND-*` has no data source from the broker | External vendor required (A7) — listed as a blocked dependency |
| G9 | **No corporate-actions API documented** | Splits/bonuses can silently corrupt historical analysis | Corporate-action flagging from an external/manual source (`HD-005`); discontinuities flagged rather than silently adjusted |
| G10 | **Margin/SPAN calculation exposure is unconfirmed** | F&O margin may have to be estimated locally | Estimates are labelled `ESTIMATED` and carry a safety multiplier (`FUT-004`); broker-reported margin is always preferred |
| G11 | **Historical candles carry no bid/ask** | Backtest slippage cannot be derived from historical spread | Explicit configurable slippage model, documented as an assumption (`BT-003`) |
| G12 | **Option-chain Greeks: source, model and refresh cadence are undocumented** | Broker Greeks may disagree with locally computed Greeks | Both computed; source labelled; divergence beyond tolerance raises a data-quality event (`GRK-004`) |
| G13 | **API requires an active Groww trading-API subscription** | Everything broker-related is gated on your subscription | Listed as blocked dependency D1 |
| G14 | **Websocket order updates are split (equity vs FNO) and position updates appear FNO-only** | Equity position changes may need polling | Feed + poll merge with deterministic conflict resolution (`OMS-003`) |

---

## 6. External dependencies — what is blocked and what you must do

| # | Dependency | Blocks | Action required from you | Workaround until then |
|---|---|---|---|---|
| D1 | **Groww trading-API subscription + credentials** (API key + secret, or TOTP token + secret) | `GRW-*` live verification, `AUTH-*`, all of `SUPERVISED` and `LIVE`, `REC-*` against a real account | Subscribe to Groww's trading API and generate credentials at `groww.in/trade-api/api-keys` | Full adapter built and fixture-verified; PAPER mode fully functional |
| D2 | **SEBI / Groww retail-algo onboarding** (see §7) | `LIVE` arming, `CMP-002` algo tagging | Ask Groww what registration/tagging their API requires for retail algo orders, and complete it | Order-rate governor and tagging plumbing built and configurable |
| D3 | **Anthropic API key** | `LLM-003` Claude provider (real calls) | Provide `ANTHROPIC_API_KEY` | `DeterministicFallbackProvider` runs the entire system with no key (`LLM-011`) |
| D4 | **News source access** (RSS endpoints or a paid API key) | `NEWS-001` ingestion breadth | Choose sources; provide keys for paid ones | Exchange filings + free RSS |
| D5 | **Fundamental data vendor** | `FUND-001`…`FUND-008` with real data | Choose a vendor or accept manual CSV loading | Manual loader + truthful empty state in the UI |
| D6 | **Notification channel credentials** (Telegram bot token, or SMTP host/user/password) | `NOTIF-001` delivery | Provide credentials | Channels disabled with a startup warning; alerts still logged and shown in the dashboard |
| D7 | **Always-on host (VPS) + network** | Reliable intraday operation, `DEPLOY-008` | Decide the host; I will write the deployment guide for it | Local Docker Compose |
| D8 | **NSE/BSE holiday list for the current and next year** | `EXCH-001` accuracy | None — I will ship a data file; you confirm it annually | Shipped file with a documented update procedure |
| D9 | **Longer intraday history vendor** (optional) | Multi-year intraday backtests (`BT-014`) | Optional purchase decision | Local archive accumulates from day one |

---

## 7. Regulatory constraints (India / SEBI)

Stated as constraints on the project, not as legal advice. You should confirm the current position with Groww directly before enabling LIVE.

| # | Constraint | Effect on this project |
|---|---|---|
| R1 | SEBI's framework for **retail participation in algorithmic trading** (February 2025 circular, implemented through brokers and exchanges during 2025–2026) governs retail algo orders placed through broker APIs | LIVE operation must comply with whatever Groww requires of API-based retail algo users. This is `CMP-003` and blocked dependency D2 |
| R2 | **Algo orders must carry an exchange-assigned unique identifier**, with the requirement that every algo order carries an Algo-ID from 1 April 2026 (already in force as of today's date) | `CMP-002` implements tagging plumbing; the actual identifier must come from Groww. Without it, LIVE trading may be non-compliant — **this is a hard gate, not a formality** |
| R3 | **Order-frequency threshold (initially 10 orders/second)** above which a retail self-developed algo requires registration | `CMP-001` governs order rate below a configurable threshold with a safety margin, and cannot be disabled in LIVE |
| R4 | **The broker is the principal**; algo providers act as agents through the broker, never directly with the exchange | The system only ever routes through Groww's API. No direct exchange connectivity is designed or permitted |
| R5 | **Registration duty sits with the account holder**, not with software | You must complete any registration Groww requires. The system records the registration details you configure and stamps them on orders (`CMP-003`) |
| R6 | Operating an algo for **third-party funds or accounts** carries a materially different regulatory burden (research analyst / investment adviser / portfolio manager regimes) | Explicitly out of scope and blocked in code: single account, single owner (`CMP-004`) |
| R7 | **No investment advice** is produced or distributed | Stated in `docs/COMPLIANCE.md` (`CMP-006`) and in the README |
| R8 | Records of orders and decisions may be requested by the broker or exchange | Retention policy `CMP-005` and the immutable audit trail `AUDIT-*` |

---

## 8. Things that cannot safely be automated

Each of these is deliberately left as a human action. This is a design position, not an omission.

| # | Action | Why it stays human | Requirement |
|---|---|---|---|
| S1 | **Enabling LIVE trading** | Irreversible financial consequence; the whole §9 ceremony exists to make it deliberate | `LIVE-002`, `LIVE-003` |
| S2 | **Raising any risk limit** | An automated system that can widen its own limits has no limits | `RISK-016` |
| S3 | **Clearing a position/order reconciliation discrepancy** | The correct resolution depends on facts only you can check (broker app, contract notes) | `REC-004` |
| S4 | **Re-enabling a strategy that was auto-disabled** | Re-enabling after a loss streak is exactly when judgement is needed | `LEARN-007` |
| S5 | **Applying a learned parameter change** | Auto-tuning on recent P&L is the textbook route to overfitting live capital | `LEARN-006` |
| S6 | **Acting on unverified or conflicting news** | Single-source or contradicted news is how automated systems get picked off | `NEWS-005`, `NEWS-008` |
| S7 | **Trading through a broker/exchange/data outage** | Blind trading during an outage produces unknown state | `MON-004`, `EXEC-008` |
| S8 | **Naked short options** | Theoretically unbounded loss on a retail account | `OPT-006`, `FNOR-003` |
| S9 | **Averaging down / martingale sizing** | Not implemented at all. No requirement supports adding to a losing position beyond a strategy's pre-declared, risk-approved scale-in plan | by omission — flagged here so the absence is intentional and visible |
| S10 | **Resuming after an unknown order state** | Must be resolved before further orders, or duplicates follow | `EXEC-008`, `REC-002` |
| S11 | **Clearing the kill switch** | Requires a human to have understood why it fired | `EMG-007` |
| S12 | **Choosing which strategies exist** | Contract §15 — every strategy needs a stated hypothesis and evidence | `STRAT-013` |

---

## 9. Missing requirements I could not write without your input

These are genuinely absent from the specification and I will not invent them silently:

1. **Your actual risk numbers** (A1, A2) — the risk engine is fully specified structurally, but its configured values are yours to set.
2. **Your actual strategies** (A5) — three reference strategies are proposed; they are placeholders for *your* trading ideas, not a recommendation to trade them.
3. **Target instruments and position scale** (A3) — determines whether index options (₹75+ lot sizes) are even viable at your capital.
4. **Performance expectations / success criteria** — the contract never states what "working well" means. Without it, `LEARN-*` and `RPT-*` can measure but cannot judge. Recommend defining: minimum acceptable expectancy, maximum acceptable drawdown, and the review period after which an underperforming system is switched off.
5. **Operating schedule** — which sessions the system runs, and whether you will be present during them (this materially changes SUPERVISED vs LIVE emphasis).

---

## 10. Audit verdict

| Check | Result |
|---|---|
| Owner categories covered | 51 / 51 |
| Requirements defined | 533 |
| Requirements with an implementation location | 533 |
| Requirements with a named test (or an explicit doc-review justification) | 533 |
| Requirements blocked by external dependency | 9 dependency groups (§6) — none block PAPER mode |
| Internal specification conflicts found | 8 (§4), all with proposed resolutions; **C1 and A12 need your explicit decision** |
| Regulatory hard gates before LIVE | 2 (R2 algo identifier, R3 registration threshold) |
| Actions deliberately left manual | 12 (§8) |

**Blocking your approval:** C1 (two-milestone completion definition) and A12 (news-service criticality). Everything else has a stated default that will be implemented unless you say otherwise.
