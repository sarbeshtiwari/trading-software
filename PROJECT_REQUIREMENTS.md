# PROJECT_REQUIREMENTS.md

**Project:** AI-Assisted Indian Equity & F&O Trading System (working name: **ATS**)
**Broker:** Groww TradeAPI (NSE / BSE — CASH and FNO segments)
**Status of this document:** DRAFT — awaiting owner approval. Once approved this file is the single source of truth for the project.
**Derived from:** The execution contract supplied by the project owner (sections 1–18), the category list in §1, the architectural constraints embedded in §7, §9, §10, §11, §12, §13, §14, §15 and §16, and the stack / Groww-access / LLM-provider decisions confirmed by the owner.
**Created:** 2026-09-17
**Version:** 0.1 (pre-approval)

---

## 0. How to read this document

Startup regression evidence: blank `STARTING_CAPITAL` in the distributed example
is treated as unavailable, not invented capital. Configuration regression tests
cover the actual example dotenv, blank values and malformed/non-positive values.
This repair does not promote any trading or end-to-end requirement status.

Protection fault-response integration evidence is in
`backend/tests/integration/test_paper_execution.py`: missing/changed protection,
quantity disagreement, stale emergency quotes and restart restoration. Execution
and recovery requirements remain partial pending heartbeat certification and the
complete provider-driven PAPER vertical slice. Independently checked, expiring
software-watchdog observations now persist through restart and appear in Positions;
`test_protection_observation.py` covers stale quotes, altered audit evidence and
startup refusal/emergency exit for a missing stop. These are not broker-held stops.

Scheduled reference production now has integration coverage in
`backend/tests/integration/test_reference_worker.py`: server-owned context,
provider-fed entry/exit, API state, restart deduplication and interrupted-cycle
fail-closed behavior. Authenticated strategy controls are exercised in
`backend/tests/integration/test_reference_controls.py` and the existing frontend
test suite. Related strategy/intraday/execution requirements remain partial;
cost estimates/FIFO now integrate in the scoped PAPER path, while complete
lifecycle acceptance remains pending.

Provider-fed regime integration now uses closed index candles and the existing
technical/classifier/history services (`backend/app/trading/regime.py`). Explicit
timestamped IV/breadth/calendar evidence remains owner-published; unavailable or
stale inputs stand down. `test_reference_regime.py` exercises actual worker entry,
net-cost journal attribution, source rejection and API state. This does not certify
continuous external source delivery or the full PAPER lifecycle.

### 0.1 Requirement ID scheme

PAPER reconciliation detail evidence: `test_reconciliation_review.py` now checks
exact FIFO lot/source records even when aggregate quantity and price agree, and
identifies local UNKNOWN orders by local/broker/reference IDs. Durable entry
approval still refuses an unresolved record when its aggregate counter is damaged.
Evidence reads fail closed above the configured audit-chain capacity rather than
silently verifying a truncated chain. REC-003/004 remain partial; general order
reconciliation and scalable archival verification are not implied.

PAPER order-recovery evidence now includes terminal-status disagreement, regressed
filled quantity and unavailable trade retrieval. These refuse synchronization
before applying fills and retain discrepancy evidence. Fresh account reconciliation
also rejects terminal-state/quantity regressions; ordinary in-flight updates still
use the OMS transition rules. `test_order_discrepancies.py` verifies persisted
positions/fills remain unchanged after refusals. REC-002 remains partial.

Each requirement has a stable ID of the form `AREA-NNN`. IDs are **never reused or renumbered** once approved. New requirements are appended with the next free number in their area.

| Prefix | Area | Prefix | Area |
|---|---|---|---|
| `ARCH` | Application architecture | `OC` | Option-chain analysis |
| `BE` | Backend services & API | `FNOR` | F&O risk |
| `FE` | Frontend / dashboard | `STRAT` | Strategy engine |
| `DB` | Database & migrations | `LLM` | LLM provider layer |
| `GRW` | Groww API integration | `AIR` | AI research agents |
| `AUTH` | Authentication & session | `AID` | AI decision engine |
| `SEC` | Security | `RISK` | Deterministic risk engine |
| `MD` | Real-time market data | `SIZE` | Position sizing |
| `HD` | Historical data | `EXEC` | Order execution |
| `EXCH` | NSE / BSE / exchange rules | `OMS` | Order management |
| `EQ` | Equity analysis | `PORT` | Portfolio management |
| `FUND` | Fundamental analysis | `PNL` | P&L |
| `TA` | Technical analysis | `JRN` | Trade journal |
| `NEWS` | News intelligence & verification | `BT` | Backtesting |
| `SENT` | Market sentiment | `WF` | Walk-forward validation |
| `REG` | Market regime detection | `PAPER` | Paper trading |
| `INTRA` | Intraday trading | `SUP` | Supervised trading |
| `FUT` | Futures | `LIVE` | Live trading |
| `OPT` | Options | `NOTIF` | Notifications |
| `GRK` | Options Greeks | `MON` | Monitoring & health |
| `ERR` | Error handling | `REC` | State recovery |
| `EMG` | Emergency controls | `AUDIT` | Audit trail |
| `LEARN` | Self-learning | `RPT` | Reporting |
| `CMP` | Compliance | `TEST` | Testing |
| `DEPLOY` | Deployment | `DOC` | Documentation |
| `LOG` | Logging |  |  |

### 0.2 Status markers (execution contract §1)

- `[ ]` Not started
- `[~]` In progress
- `[x]` Implemented (code genuinely exists and runs)
- `[✓]` Tested (automated test exists and passes)

Every requirement starts at `[ ]`. Status is mirrored in `IMPLEMENTATION_STATUS.md`.

### 0.3 Priority

| Priority | Meaning |
|---|---|
| **P0** | Foundational. Nothing else can be built or trusted without it. |
| **P1** | Core trading functionality. The system is not usable without it. |
| **P2** | Required by the specification, not on the critical path to a working PAPER-mode system. |
| **P3** | Required by the specification, lowest build order (reporting, self-learning, polish). |

**All priorities are in scope.** Per the execution contract §1 and §16 nothing is dropped; priority controls *build order only*.

### 0.4 Acceptance criteria convention

Acceptance criteria are written so a reviewer can mechanically decide pass/fail. Where a criterion cannot be verified without live broker credentials it is written to be verifiable against a **recorded/contract-level fixture**, and the residual unverified part is listed in `REQUIREMENTS_AUDIT.md` §6.

---

## 1. System overview (the thing being built)

ATS is a single-owner, self-hosted algorithmic trading system for Indian markets (NSE/BSE, CASH and FNO segments) executing through the Groww TradeAPI. It ingests market data, news and fundamentals; produces quantitative analysis; uses Claude-backed research agents to synthesise structured trade proposals; validates every proposal through a deterministic risk engine the AI cannot bypass or modify; executes and manages orders; tracks positions, P&L and a full trade journal; and exposes all of it through a React dashboard.

It runs in exactly one of three modes at a time:

| Mode | Market data | Order routing | Human gate | Default |
|---|---|---|---|---|
| `PAPER` | Real (or historical replay) | Simulated fill engine | None | **Yes** |
| `SUPERVISED` | Real | Real broker, **only after explicit per-order human approval** | Every order | No |
| `LIVE` | Real | Real broker, autonomous within risk limits | Arming ceremony only | No |

### 1.1 Canonical decision pipeline (execution contract §14)

```
Market Data ──┐
News/Research ─┼─> Quantitative Analysis ─> Claude AI Agents ─> Structured Trade Proposal (JSON)
Fundamentals ─┘                                                            |
                                                                           v
                                             Deterministic Proposal Validation (schema + sanity + bounds)
                                                                           |
                                                                           v
                                     Deterministic Risk Engine ──REJECT──> NO TRADE (logged with reason)
                                                                           | APPROVE
                                                                           v
                                                    Execution Engine ─> Broker (Groww / Paper)
                                                                           |
                                                                           v
                                 Position ─> Monitoring ─> Exit ─> P&L ─> Journal ─> Learning
```

The risk engine is a pure, deterministic, non-LLM component. No LLM output can alter its limits, disable it, or route around it.

### 1.2 Confirmed technology decisions

| Layer | Technology | Rationale |
|---|---|---|
| Backend | Python 3.11, FastAPI, Pydantic v2, asyncio | Owner-confirmed; matches the `backend/...` module paths in the contract |
| Scheduling | APScheduler in-process + Redis distributed lock | Deterministic intraday cadence, single-writer guarantee |
| Relational DB | PostgreSQL 16 + TimescaleDB | Owner-confirmed; hypertables for candles/ticks |
| ORM / migrations | SQLAlchemy 2.x (async) + Alembic | Owner-confirmed |
| Cache / streams / locks | Redis 7 | Owner-confirmed |
| Frontend | React 18 + Vite + TypeScript + Tailwind CSS | Owner-confirmed; matches existing repos |
| Charts | Recharts + lightweight-charts | Time-series and candlestick rendering |
| Containerisation | Docker Compose | Owner-confirmed |
| LLM | Anthropic Claude behind an `LLMProvider` abstraction, plus `DeterministicFallbackProvider` | Owner-confirmed |
| Broker | Groww TradeAPI via a first-party REST client (`growwapi` SDK used for the websocket feed) | Full control over retries, idempotency and timeouts on the order path |

### 1.3 Verified Groww TradeAPI facts (source of truth for the adapter)

Verified against the official Groww documentation on 2026-09-17. These facts constrain many requirements below.

| Fact | Value |
|---|---|
| REST base URL | `https://api.groww.in/v1` |
| Required headers | `Authorization: Bearer <token>`, `Accept: application/json`, `X-API-VERSION: 1.0` |
| Auth endpoint | `POST /token/api/access` |
| Auth flow A | API key + secret → daily access token; **requires daily approval**; token endpoint capped at **150 requests / 24 h** |
| Auth flow B | TOTP token + TOTP secret (via `pyotp`) → access token, **no expiry** |
| Response envelope | Success `{"status":"SUCCESS","payload":{…}}`; failure `{"status":"FAILURE","error":{"code":"…","message":"…"}}` |
| Error codes | `GA000` internal, `GA001` bad request, `GA003` unable to serve, `GA004` not found, `GA005` unauthorised, `GA006` cannot process, `GA007` duplicate order reference id |
| Rate limits | Auth 5/s, 30/min · Orders 10/s, 250/min · Live data 10/s, 300/min · Non-trading 20/s, 500/min |
| Segments | `CASH`, `FNO` |
| Products | `CNC`, `MIS`, `NRML` |
| Order types | `MARKET`, `LIMIT`, `STOP_LOSS`, `STOP_LOSS_MARKET` |
| Validity | `DAY` only |
| Order status values | `OPEN`, `PENDING`, `EXECUTED`, `CANCELLED`, `REJECTED` |
| Idempotency handle | `order_reference_id` — 8–20 alphanumeric chars, max 2 hyphens; duplicates rejected with `GA007` |
| LTP / OHLC batching | Max **50 instruments** per call |
| WebSocket feed | `GrowwFeed`: `subscribe_ltp`, `subscribe_index_value`, `subscribe_market_depth`, `subscribe_equity_order_updates`, `subscribe_fno_order_updates`, `subscribe_fno_position_updates`; **max 1000 instruments** |
| Historical max range per request | 1m→7d · 5m→15d · 10m→30d · 60m→150d · 240m→365d · 1440m→1080d · weekly→unlimited |
| Historical depth | Intraday intervals: **3 months only**. Daily/weekly: full history |
| Option data | `get_option_chain` (strikes, CE/PE, OI, volume, LTP, Greeks), `get_greeks` (delta/gamma/theta/vega/rho/IV) |

> **Consequence recorded here because it shapes the whole project:** intraday history from Groww is limited to 3 months. Intraday strategy backtests therefore cannot be validated on multi-year data from Groww alone — see `REQUIREMENTS_AUDIT.md` §4 and requirements `HD-010`, `BT-014`.

### 1.4 Repository layout (target)

```
ai-trading-system/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI entrypoint
│   │   ├── config.py               # Settings (pydantic-settings)
│   │   ├── modes.py                # TradingMode enum + guards
│   │   ├── api/                    # REST + WS routers
│   │   ├── core/                   # clock, logging, errors, ids, money
│   │   ├── db/                     # models, session, repositories
│   │   ├── brokers/                # BrokerProvider ABC, groww/, paper/
│   │   ├── marketdata/             # MarketDataProvider ABC, live/, historical/, replay/
│   │   ├── instruments/            # instrument master, symbol resolution
│   │   ├── analysis/               # technical/, fundamental/, regime/
│   │   ├── news/                   # ingestion, dedupe, verification, sentiment
│   │   ├── fno/                    # futures, options, greeks, chain, fno_risk
│   │   ├── strategies/             # Strategy ABC + concrete strategies
│   │   ├── llm/                    # LLMProvider ABC, claude/, fallback/, prompts/
│   │   ├── agents/                 # research agents, decision engine
│   │   ├── risk/                   # deterministic risk engine
│   │   ├── sizing/                 # position sizing
│   │   ├── execution/              # order execution, OMS, reconciliation
│   │   ├── portfolio/              # positions, exposure, P&L
│   │   ├── journal/                # trade journal
│   │   ├── backtest/               # engine, walkforward, metrics
│   │   ├── learning/               # attribution, parameter review
│   │   ├── reporting/              # monthly report generation
│   │   ├── notifications/          # channels
│   │   ├── monitoring/             # healthchecks, metrics, watchdogs
│   │   ├── emergency/              # kill switch
│   │   └── audit/                  # decision audit trail
│   ├── tests/{unit,integration,e2e,fixtures}/
│   └── alembic/
├── frontend/
├── docker/
├── docs/
├── PROJECT_REQUIREMENTS.md
├── IMPLEMENTATION_STATUS.md
└── REQUIREMENTS_AUDIT.md
```

---

## 2. Requirements

Columns: **ID · Requirement · Acceptance criteria · Module/File · Test · Pri · Depends · Status**

### 2.1 Application architecture (`ARCH`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| ARCH-001 | Single `TradingMode` enum (`PAPER`, `SUPERVISED`, `LIVE`) is the only mode authority in the system | Enum defined once; `grep` finds no other literal mode strings outside `modes.py` and config parsing; invalid value raises at startup | `backend/app/modes.py` | `test_modes_enum` | P0 | — | [✓] |
| ARCH-002 | Mode is resolved once at startup from `TRADING_MODE` env var and is immutable for the process lifetime | Mutating the resolved mode at runtime raises `ImmutableModeError`; mode appears in startup log line and `/api/system/mode` | `backend/app/modes.py`, `config.py` | `test_mode_immutable` | P0 | ARCH-001 | [✓] |
| ARCH-003 | `TRADING_MODE` defaults to `PAPER` when unset or unparseable | Booting with no env var yields `PAPER` and emits a WARN log; never defaults to `SUPERVISED`/`LIVE` | `backend/app/config.py` | `test_mode_defaults_paper` | P0 | ARCH-001 | [✓] |
| ARCH-004 | `BrokerProvider` abstract interface with `GrowwBrokerProvider` and `PaperBrokerProvider` implementations (contract §7) | ABC defines place/modify/cancel/status/list/trades/positions/holdings/margin/profile; both subclasses implement every method; `abstractmethod` coverage test passes | `backend/app/brokers/base.py` | `test_broker_interface_parity` | P0 | ARCH-001 | [✓] |
| ARCH-005 | `MarketDataProvider` abstract interface with `LiveMarketDataProvider` and `HistoricalMarketDataProvider` implementations (contract §7) | ABC defines ltp/quote/ohlc/candles/option_chain/subscribe; both implementations satisfy it; parity test passes | `backend/app/marketdata/base.py` | `test_marketdata_interface_parity` | P0 | ARCH-001 | [✓] |
| ARCH-006 | Provider selection is driven solely by mode via a factory; no strategy or engine code instantiates a concrete provider | Factory returns `PaperBrokerProvider` for `PAPER`, `GrowwBrokerProvider` for `SUPERVISED`/`LIVE`; static check asserts no direct concrete-class imports outside `brokers/__init__.py` | `backend/app/brokers/factory.py` | `test_provider_factory_by_mode` | P0 | ARCH-004 | [✓] |
| ARCH-007 | The same strategy engine runs unchanged against paper and live providers (contract §7) | An identical strategy instance is executed against both providers in a test and produces identical signals for identical input data | `backend/app/strategies/`, `backend/app/execution/` | `test_engine_provider_agnostic` | P0 | ARCH-004, ARCH-005 | [ ] |
| ARCH-008 | Demo/sample/synthetic data can never reach `LIVE` mode (contract §7) | Synthetic data carries a `DataOrigin.SYNTHETIC` tag; the pipeline raises `SyntheticDataInLiveError` if tagged data reaches an execution path while mode is `LIVE`; test asserts the raise | `backend/app/core/data_origin.py` | `test_synthetic_blocked_in_live` | P0 | ARCH-002 | [✓] |
| ARCH-009 | All configuration is centralised in a typed `Settings` object (pydantic-settings), loaded from env + `.env` | No `os.getenv` outside `config.py`; unknown/missing required settings fail fast with a readable error listing each offending key | `backend/app/config.py` | `test_settings_validation` | P0 | — | [✓] |
| ARCH-010 | Layered dependency rule: `strategies`/`agents` may not import `brokers` or `execution` directly | An import-linter (or custom AST test) enforces the layering and fails CI on violation | `backend/tests/unit/test_layering.py` | `test_layering_rules` | P1 | ARCH-006 | [✓] |
| ARCH-011 | Single-writer guarantee: only one trading loop instance may act on an account at a time | A Redis lock keyed on account id is acquired before the trading loop starts; second instance exits with a clear message; test simulates contention | `backend/app/core/locks.py` | `test_single_writer_lock` | P0 | ARCH-009 | [✓] |
| ARCH-012 | A monotonic, timezone-aware clock abstraction (`Asia/Kolkata`) is the only source of time | No `datetime.now()` without tz outside `core/clock.py`; tests inject a fake clock and control time deterministically | `backend/app/core/clock.py` | `test_clock_tz_and_injection` | P0 | — | [✓] |
| ARCH-013 | Deterministic ID generation for orders, proposals, decisions and journal entries | IDs are ULID/UUIDv7, sortable by time, unique under 10k-concurrency test | `backend/app/core/ids.py` | `test_id_uniqueness_ordering` | P1 | — | [✓] |
| ARCH-014 | All monetary values use `Decimal` with explicit paise-level quantisation; floats are banned in money paths | AST test asserts no `float` annotations in money-carrying models; rounding helper quantises to 0.01 with `ROUND_HALF_UP` | `backend/app/core/money.py` | `test_money_precision` | P0 | — | [✓] |
| ARCH-015 | Graceful shutdown: SIGTERM/SIGINT cancels the trading loop, flushes audit writes and releases locks before exit | Shutdown test asserts no in-flight order is abandoned without a persisted record and the lock is released | `backend/app/main.py`, `core/lifecycle.py` | `test_graceful_shutdown` | P1 | ARCH-011 | [✓] |
| ARCH-016 | Event bus (Redis Streams) decouples producers (data, signals) from consumers (risk, execution, journal, UI) | Publishing an event delivers it to all registered consumers; consumer failure does not drop the event (ack-on-success semantics) | `backend/app/core/events.py`, `backend/app/monitoring/runtime_events.py`, `backend/app/db/models/event_outbox.py`, `backend/app/execution/paper.py`, `backend/app/api/workspace_stream.py`; Redis recovery, transactional PAPER order relay and read-only dashboard invalidation integrated; remaining producer/consumer families pending; `docs/EVENT_DELIVERY.md` | `backend/tests/unit/test_concurrency.py`, `backend/tests/integration/test_redis_events.py`, `test_runtime_event_outbox.py`, `test_fresh_migrations.py`, `test_workspace_stream.py` | P1 | ARCH-009 | [~] |

### 2.2 Backend services & API (`BE`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| BE-001 | FastAPI application exposing a versioned REST API under `/api/v1` with OpenAPI docs | `GET /api/v1/openapi.json` returns a valid schema containing every router; app boots in test client | `backend/app/main.py` | `test_app_boots_openapi` | P0 | ARCH-009 | [✓] |
| BE-002 | Health endpoints: `/api/v1/health/live` (process) and `/api/v1/health/ready` (full dependency check) | `live` returns 200 whenever the process is up; `ready` returns 503 with a per-component breakdown when any critical component is down | `backend/app/api/health.py` | `test_health_endpoints` | P0 | MON-001 | [✓] |
| BE-003 | System endpoints: current mode, trading-enabled flag, armed state, kill-switch state, uptime, version | `GET /api/v1/system/status` returns all fields; values reflect real internal state, not constants | `backend/app/api/system.py` | `test_system_status` | P0 | ARCH-002 | [~] |
| BE-004 | Market-data endpoints: quote, LTP batch, OHLC, candles, index values | Each endpoint proxies the active `MarketDataProvider`; batch endpoints enforce the 50-instrument Groww cap and chunk transparently | `backend/app/api/marketdata.py` | `test_api_marketdata` | P1 | MD-001 | [ ] |
| BE-005 | Order endpoints: submit intent, list, detail, modify, cancel, trades | Endpoints delegate to the OMS; direct broker calls from the API layer are prohibited (layering test) | `backend/app/api/orders.py` | `test_api_orders` | P1 | OMS-001 | [ ] |
| BE-006 | Portfolio endpoints: positions, holdings, exposure, margin, day P&L, equity curve | Values are computed from stored positions/trades, never hard-coded (contract §6) | `backend/app/api/portfolio.py` | `test_api_portfolio` | P1 | PORT-001 | [ ] |
| BE-007 | Proposal/decision endpoints: list proposals with full reasoning, risk verdicts and rejection reasons | Every proposal returned includes its audit chain id and risk verdict; rejected proposals are retrievable (contract §13) | `backend/app/api/decisions.py` | `test_api_decisions` | P1 | AUDIT-001 | [ ] |
| BE-008 | Supervised-approval endpoints: pending approvals, approve, reject, with expiry | Approving a stale (expired) proposal returns 409 and never reaches the broker | `backend/app/api/approvals.py` | `test_api_approvals_expiry` | P1 | SUP-002 | [ ] |
| BE-009 | WebSocket endpoint pushing live ticks, order updates, position updates, alerts and system state to the dashboard | Client receives a message within 1s of an internal event in an integration test; reconnect resumes with a state snapshot | `backend/app/api/market_stream.py`, `workspace_stream.py`, `frontend/src/WorkspaceUpdates.tsx`; authenticated quote snapshots and committed order/fill/position/risk-latch/health refresh hints integrated with current-state HTTP recovery; sub-second all-event delivery and remaining alert/control families pending | `backend/tests/integration/test_market_stream.py`, `test_market_chart_browser.py`, `test_workspace_stream.py`, `test_runtime_event_outbox.py`, `test_control_event_stream.py`, `frontend/tests/workspace-stream-browser.mjs`, `frontend/tests/risk-stream-browser.mjs`, `frontend/src/WorkspaceUpdates.test.tsx` | P1 | ARCH-016 | [~] |
| BE-010 | Request-scoped structured logging with correlation id propagated into every downstream call and audit record | Each response carries `X-Request-Id`; the same id appears in audit rows created by that request | `backend/app/core/logging.py` | `test_correlation_id_propagation` | P1 | LOG-001 | [✓] |
| BE-011 | Backend enforces that mutating trading endpoints are rejected when trading is disabled by health checks or kill switch | Calls return 423 (Locked) with the failing component named | `backend/app/api/deps.py` | `test_trading_disabled_guard` | P0 | MON-004, EMG-001 | [ ] |
| BE-012 | Background scheduler runs the intraday cycle at a configured cadence, only within market hours, only when trading is enabled | Scheduler test with a fake clock verifies the job does not fire outside market hours or while disabled | `backend/app/core/scheduler.py` | `test_scheduler_gating` | P1 | EXCH-002 | [ ] |
| BE-013 | Idempotent API: submitting the same client intent id twice returns the original result rather than creating a second order | Second POST returns 200 with the original order id and creates no new broker call | `backend/app/api/orders.py`, `execution/idempotency.py` | `test_api_idempotency` | P0 | EXEC-004 | [ ] |
| BE-014 | Pagination, filtering and sorting on all list endpoints with hard server-side caps | Requesting >200 items is clamped; cursor pagination returns stable ordering under insertion | `backend/app/api/common.py` | `test_pagination` | P2 | BE-001 | [ ] |

### 2.3 Database (`DB`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| DB-001 | PostgreSQL 16 + TimescaleDB provisioned via Docker Compose with a named volume | `docker compose up db` yields a reachable DB with the `timescaledb` extension created | `docker/docker-compose.yml`, `docker/db/init.sql` | `test_db_connectivity` (integration) | P0 | — | [~] |
| DB-002 | Async SQLAlchemy 2.x engine + session factory with pooling and per-request session lifecycle | Sessions are closed after each request; pool exhaustion under 100 concurrent requests does not deadlock | `backend/app/db/session.py`; configurable driver/pool/session deadlines bound at factory initialization and hard invalidation cleanup; `docs/PAPER_WORKER.md` | `test_session_lifecycle`; `test_database_deadlines.py` timeout/cancellation rollback; `test_network_recovery.py` actual stalled TCP, zero checked-out connections and recovery; `test_database_timeouts.py` configuration; `test_historical_process.py` parent isolation despite changed environment | P0 | DB-001 | [✓] |
| DB-003 | Alembic migrations; schema is created only by migrations (never `create_all` in production paths) | `alembic upgrade head` on an empty DB produces the full schema; `alembic check` reports no drift from models | `backend/alembic/`, explicit candle time index in `app/db/models/market_data.py`; 0018 uses dialect-compatible batch alteration | `test_empty_postgres_upgrade_and_metadata_check`: real empty PostgreSQL upgrade, clean drift check, downgrade/re-upgrade and deliberately introduced drift detection; `test_app_boot.py` SQLite migration roundtrip; `test_orphan_migration.py` preserves values/indexes and refuses lossy downgrade; `test_orphan_postgres.py` same refusal on PostgreSQL | P0 | DB-002 | [✓] |
| DB-004 | `instruments` table: exchange, segment, trading_symbol, groww_symbol, isin, name, lot_size, tick_size, expiry, strike, option_type, instrument_type, active flag | Unique constraint on (exchange, segment, trading_symbol); lookup by any of symbol/isin/token returns one row | `backend/app/db/models/instrument.py` | `test_instrument_model` | P0 | DB-003 | [✓] |
| DB-005 | `candles` TimescaleDB hypertable keyed (instrument_id, interval, ts) with OI and volume columns | Hypertable created; duplicate insert of the same candle upserts rather than duplicating; 1M-row insert completes within the documented budget | `backend/app/db/models/candle.py` | `test_candles_hypertable` | P0 | DB-003 | [x] |
| DB-006 | `ticks` hypertable with retention policy for raw tick capture | Retention policy configured and verifiable via Timescale catalog query | `backend/alembic/versions/0001_initial_schema.py` | `test_candles_hypertable` / `tests/postgres_checks.py::hypertables`: actual Timescale catalog and scheduled 90-day retention | P2 | DB-005 | [✓] |
| DB-007 | `orders` table capturing full lifecycle: client intent id, broker order id, reference id, status, all price/qty fields, timestamps, rejection reason, raw broker payloads | Every state transition is persisted; raw request and response JSON retained for audit | `backend/app/db/models/order.py` | `test_order_model_lifecycle` | P0 | DB-003 | [✓] |
| DB-008 | `trades` (fills) table supporting multiple partial fills per order with exchange trade ids | Sum of fill quantities never exceeds order quantity (DB check constraint); duplicate exchange trade id is rejected | `backend/app/db/models/trading.py`, `alembic/versions/0015_serialized_fill_limits.py` | `test_fill_sum_trigger`: actual deployed PostgreSQL functions/cloned constraints, partial fills, duplicates, overfill/update/reduction and three isolation levels | P0 | DB-007 | [✓] |
| DB-009 | `positions` table with net quantity, average price, realised and unrealised P&L, product, segment, and open/closed state | Position rows reconcile to trades: recomputing from trades reproduces stored values exactly | `backend/app/db/models/position.py` | `test_position_recompute` | P0 | DB-008 | [✓] |
| DB-010 | `proposals` table storing the full structured trade proposal JSON, model/prompt version, confidence, evidence, invalidation conditions | Proposal is retrievable with its evidence list intact; schema-validated on write | `backend/app/db/models/proposal.py` | `test_proposal_model` | P1 | DB-003 | [✓] |
| DB-011 | `risk_decisions` table storing verdict, every rule evaluated, inputs, computed values and the binding reason for rejection | For any decision the exact failing rule and its numeric inputs can be reconstructed | `backend/app/db/models/risk_decision.py` | `test_risk_decision_model` | P0 | DB-003 | [✓] |
| DB-012 | `audit_events` append-only table with the full §13 field set | Rows cannot be updated or deleted (DB trigger/permission); attempt raises | `backend/app/db/models/audit.py`, `alembic/versions/0001_initial_schema.py` | `test_audit_append_only`: raw UPDATE/DELETE rejected by actual deployed PostgreSQL trigger; test row rolled back | P0 | DB-003 | [✓] |
| DB-013 | `journal_entries` table linking proposal → risk decision → orders → trades → exit → outcome | A single query returns the complete lineage for any closed trade | `backend/app/db/models/journal.py` | `test_journal_lineage` | P1 | DB-009 | [✓] |
| DB-014 | `news_items` table with source, url hash, published time, entities, verification status, sentiment and dedupe key | Duplicate article from two sources collapses to one row with both sources recorded | `backend/app/db/models/news.py` | `test_news_dedupe` | P2 | DB-003 | [✓] |
| DB-015 | `fundamentals` table storing per-instrument fundamental snapshots with as-of dates | Point-in-time query returns the value known as of a past date (no look-ahead) | `backend/app/db/models/fundamentals.py` | `test_fundamentals_point_in_time` | P2 | DB-003 | [✓] |
| DB-016 | `llm_calls` table: provider, model, prompt version, tokens in/out, cost, latency, outcome, redacted prompt hash | Cost roll-up per day matches the sum of individual calls | `backend/app/db/models/llm_call.py` | `test_llm_call_accounting` | P1 | DB-003 | [✓] |
| DB-017 | `risk_config` table holding the active, versioned risk limits with change history and author | Updating a limit creates a new version; the previous version remains queryable | `backend/app/db/models/risk_config.py` | `test_risk_config_versioning` | P0 | DB-003 | [✓] |
| DB-018 | `system_state` table persisting mode, armed state, kill-switch state, last reconciliation time and daily counters | State survives a process restart and is read during recovery | `backend/app/db/models/system_state.py` | `test_system_state_persistence` | P0 | DB-003 | [✓] |
| DB-019 | `backtest_runs` and `backtest_results` tables storing parameters, data window, metrics and equity curve | A run is fully reproducible from its stored parameters (same seed → same metrics) | `backend/app/db/models/backtest.py` | `test_backtest_persistence` | P2 | DB-003 | [✓] |
| DB-020 | Daily logical backup job with documented restore procedure | `pg_dump` runs on schedule; a restore test recreates the schema and row counts | `docker/backup/`, `docs/OPERATIONS.md` | `test_backup_restore` (integration) | P2 | DB-001 | [ ] |

### 2.4 Frontend / dashboard (`FE`)

Every page below consumes real backend data. Hard-coded numbers and "Coming Soon" panels are prohibited (contract §6).

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| FE-001 | React 18 + Vite + TypeScript + Tailwind app with typed API client generated from or validated against the OpenAPI schema | `npm run build` succeeds with zero TS errors; API types compile against the live schema in CI | `frontend/` | `test_frontend_build` (CI) | P0 | BE-001 | [~] |
| FE-002 | Global mode banner rendering the live `TRADING_MODE`, colour-coded, always visible | Banner text comes from `/api/v1/system/status`; LIVE renders a distinct high-contrast treatment; test asserts no hard-coded mode string | `frontend/src/components/ModeBanner.tsx` | `ModeBanner.test.tsx` | P0 | BE-003 | [~] |
| FE-003 | Dashboard home: index values (NIFTY/BANKNIFTY/SENSEX), day P&L, open positions count, available margin, exposure, system health | Every tile is bound to a backend field; test mounts with a mocked API and asserts no literal numbers in the component source | `frontend/src/pages/Dashboard.tsx` | `Dashboard.test.tsx` | P0 | BE-004, BE-006 | [~] |
| FE-004 | Live market-data view: watchlist with LTP, change %, volume, updating over WebSocket | Prices update on WS message without a page refresh; stale-data indicator appears when ticks stop for > N seconds | `frontend/src/QuoteWatchlist.tsx`, `backend/app/api/market.py`, `market_stream.py`; audited stored quotes with WebSocket/stale suppression and HTTP fallback integrated; durable preferences/external live-feed acceptance pending; `docs/MARKET_VIEW.md` | `frontend/src/QuoteWatchlist.test.tsx`, `backend/tests/integration/test_market_quotes.py`, `test_market_stream.py`, `test_market_chart_browser.py` | P1 | BE-009, MD-008 | [~] |
| FE-005 | Instrument detail: candlestick chart with selectable interval, indicator overlays, and the instrument's fundamentals | Chart data comes from the candles endpoint; switching interval refetches; indicators match backend-computed values | `frontend/src/MarketPanel.tsx`, `frontend/src/CandleChart.tsx`, `frontend/src/FundamentalPanel.tsx`, `frontend/src/FundamentalSources.tsx`, `backend/app/api/market.py`; stored intraday chart/SMA/manual fundamentals/source discovery integrated, broader acceptance pending; `docs/MARKET_VIEW.md` | `frontend/src/MarketPanel.test.tsx`, `frontend/src/FundamentalPanel.test.tsx`, `backend/tests/integration/test_market_chart.py`, `test_market_chart_browser.py`, `test_market_chart_postgres.py`, `test_fundamental_sources_postgres.py` | P1 | HD-003, TA-001 | [~] |
| FE-006 | Option-chain view: strikes, CE/PE LTP, OI, OI change, IV, Greeks, PCR, max-pain marker | Rendered values equal backend option-chain payload; ATM row is highlighted using the live underlying price | `frontend/src/pages/OptionChain.tsx` | `OptionChain.test.tsx` | P1 | OC-001 | [~] |
| FE-007 | Positions page: open positions with live mark-to-market, unrealised P&L, stop and target levels, and an exit action | P&L recomputes on each tick; exit action posts a real exit intent through the OMS | `frontend/src/pages/Positions.tsx` | `Positions.test.tsx` | P1 | PORT-001 | [~] |
| FE-008 | Orders page: order book with live status, fills, rejection reasons, and modify/cancel actions | Rejected orders display the broker's error code and message verbatim | `frontend/src/pages/Orders.tsx` | `Orders.test.tsx` | P1 | OMS-001 | [~] |
| FE-009 | Proposals & decisions page: every AI proposal with thesis, evidence, confidence, risk verdict, and — for rejections — the exact binding rule | A rejected proposal's page answers "why did you NOT take this trade" without reading logs (contract §13) | `frontend/src/pages/Decisions.tsx` | `Decisions.test.tsx` | P1 | BE-007 | [~] |
| FE-010 | Supervised approval queue: pending proposals with a countdown, approve/reject buttons, and a mandatory confirmation dialog | Approval sends the approval token; expiry disables the button client-side and the server rejects it too | `frontend/src/pages/Approvals.tsx` | `Approvals.test.tsx` | P1 | BE-008 | [ ] |
| FE-011 | Risk page: current limits, live utilisation bars (daily loss, exposure, per-trade risk, drawdown), and limit-edit form | Utilisation values come from the risk engine; edits are rejected when the system is armed in LIVE without re-confirmation | `frontend/src/App.tsx`, `RiskUtilisationPanel.tsx`, `RiskDecisionPanel.tsx`; typed risk API (PAPER owner controls, account-budget polling and receipt-bound historical per-trade/rule inspection; complete live utilisation and broader modes pending) | `App.test.tsx`, `RiskUtilisationPanel.test.tsx`, `RiskDecisionPanel.test.tsx`, `test_risk_controls.py`, `test_risk_inspection.py`, `test_risk_utilisation_browser.py` | P1 | RISK-001 | [~] |
| FE-012 | Trade journal page with filters (date, instrument, strategy, outcome) and a per-trade lineage drill-down | Drill-down shows proposal → risk → orders → fills → exit → P&L for the selected trade | `frontend/src/JournalPanel.tsx`; authenticated `backend/app/api/journal.py` | `JournalPanel.test.tsx`; `test_journal_api.py`; real `paper-browser.mjs` lineage/annotation/download | P2 | JRN-001 | [✓] |
| FE-013 | Backtest page: configure a run, launch it, watch progress, and view metrics plus equity curve | Launching writes a `backtest_runs` row and the UI polls/streams real progress | `frontend/src/HistoricalPanel.tsx`, `HistoricalJobs.tsx`; `backend/app/backtest/jobs.py`, `cancellation.py` (server-owned plan selection, authenticated launch/progress/publication and local-child cancellation; in-browser parameter editing and orphan recovery pending) | `HistoricalPanel.test.tsx`; `HistoricalJobs.test.tsx`; `frontend/tests/historical-browser.mjs`; `historical-cancellation-browser.mjs`; `backend/tests/integration/test_historical_jobs.py`; `test_historical_cancellation.py` | P2 | BT-001 | [~] |
| FE-014 | News page: ingested items with source, verification status, sentiment, linked instruments, and the impact assessment | Unverified items are visually distinct from verified ones | `frontend/src/pages/News.tsx` | `News.test.tsx` | P2 | NEWS-001 | [ ] |
| FE-015 | System health page: per-component health-check results with last-checked timestamps and failure detail | Mirrors `/api/v1/health/ready` exactly; a failing component shows the raw error | `frontend/src/App.tsx`, `RuntimeReadinessPanel.tsx`; `backend/app/api/readiness.py` (typed authenticated runtime blockers, catalog provenance, stale health; full per-component acceptance pending) | `RuntimeReadinessPanel.test.tsx`; `test_runtime_readiness_api.py`; `App.test.tsx` | P1 | MON-001 | [~] |
| FE-016 | Emergency controls: kill switch, flatten-all, disable-new-entries — each behind a typed confirmation | Kill switch requires typing a confirmation phrase; action calls the real emergency endpoint | `frontend/src/App.tsx` (PAPER Risk controls; broader modes pending) | `App.test.tsx` | P0 | EMG-001 | [~] |
| FE-017 | LIVE arming ceremony UI implementing the contract §9 checklist as a gated, ordered flow | Each of the 7 preconditions is shown with pass/fail from the backend; the final confirm button is disabled until all pass | `frontend/src/pages/ArmLive.tsx` | `ArmLive.test.tsx` | P0 | LIVE-002 | [ ] |
| FE-018 | Reports page listing monthly reports with download and in-page rendering | Report content comes from the reporting service, not the client | `frontend/src/pages/Reports.tsx` | `Reports.test.tsx` | P3 | RPT-001 | [ ] |
| FE-019 | Global error/stale-data handling: API failures surface a visible banner; never a silent blank panel | Simulated API 500 renders an error state in every page test | `frontend/src/components/ErrorBoundary.tsx` | `ErrorBoundary.test.tsx` | P1 | FE-001 | [~] |
| FE-020 | Authenticated session for the dashboard with login, token refresh and logout | Unauthenticated requests redirect to login; token expiry triggers refresh, then logout on failure | `frontend/src/auth/` | `auth.test.tsx` | P0 | SEC-002 | [~] |
| FE-021 | Responsive layout usable at 1280px and above, with a dark theme suited to long screen sessions | Layout renders without horizontal overflow at 1280px in a viewport test | `frontend/src/layouts/` | `layout.test.tsx` | P3 | FE-001 | [~] |

### 2.5 Groww API integration (`GRW`)

Built against the verified contract in §1.3. **No fabricated responses.** Until real credentials exist, every requirement here is verified against contract-level fixtures and recorded payload shapes; the residual live verification gap is listed in `REQUIREMENTS_AUDIT.md` §6.

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| GRW-001 | Typed HTTP client for `https://api.groww.in/v1` sending `Authorization`, `Accept` and `X-API-VERSION` headers on every request | Header set asserted on every outbound request in tests; base URL configurable for fixture replay | `backend/app/brokers/groww/client.py` | `test_groww_client_headers` | P0 | ARCH-009 | [✓] |
| GRW-002 | Response-envelope handling: unwrap `status=SUCCESS` → `payload`; map `status=FAILURE` → typed `GrowwApiError(code, message)` | Both envelopes parsed correctly; a malformed envelope raises `InvalidResponseError` rather than returning `None` | `backend/app/brokers/groww/envelope.py` | `test_groww_envelope` | P0 | GRW-001 | [✓] |
| GRW-003 | Error-code taxonomy mapping `GA000/GA001/GA003/GA004/GA005/GA006/GA007` to typed, retryable/non-retryable exception classes | Each code maps to the documented class; `GA007` maps to `DuplicateOrderReferenceError` and is **never** retried | `backend/app/brokers/groww/errors.py` | `test_groww_error_mapping` | P0 | GRW-002 | [✓] |
| GRW-004 | Client-side rate limiter enforcing the documented per-second and per-minute buckets per category (auth/orders/live/non-trading) | Token-bucket limiter blocks the 11th order call within one second; limiter state is shared across the process | `backend/app/brokers/groww/ratelimit.py` | `test_groww_ratelimit_buckets` | P0 | GRW-001 | [✓] |
| GRW-005 | Retry policy with exponential backoff and jitter for transient failures only (`GA000`, `GA003`, 5xx, timeouts) | Non-transient errors are not retried; max attempts and total deadline are configurable and enforced | `backend/app/brokers/groww/retry.py` | `test_groww_retry_policy` | P0 | GRW-003 | [✓] |
| GRW-006 | **Order calls are never blindly retried** (contract §11): a timed-out order triggers a status reconciliation by `order_reference_id` before any resubmission | Timeout test asserts a `get_order_status_by_reference` call precedes any retry, and that a found order suppresses resubmission | `backend/app/brokers/groww/orders.py`, `execution/safe_retry.py` | `test_order_timeout_no_blind_retry` | P0 | GRW-005, EXEC-004 | [✓] |
| GRW-007 | `POST /order/create` implemented with full parameter validation against Groww enums before the call | Invalid product/order type/validity is rejected locally with a field-level error; valid payload matches the documented shape byte-for-byte in a fixture test | `backend/app/brokers/groww/orders.py` | `test_groww_place_order_payload` | P0 | GRW-002 | [✓] |
| GRW-008 | Order modify implemented (quantity, order_type, price, trigger_price, groww_order_id, segment) | Modify payload validated; modifying a non-open order is rejected locally before the call | `backend/app/brokers/groww/orders.py` | `test_groww_modify_order` | P1 | GRW-007 | [✓] |
| GRW-009 | Order cancel implemented (groww_order_id, segment) with idempotent handling of already-cancelled orders | Cancelling an already-cancelled order resolves to success without raising | `backend/app/brokers/groww/orders.py` | `test_groww_cancel_order` | P1 | GRW-007 | [✓] |
| GRW-010 | Order status by id (`GET /order/detail/{groww_order_id}`) and by `order_reference_id`, both segment-aware | Both return a normalised `OrderStatus` model; unknown id maps to `GA004` → `OrderNotFoundError` | `backend/app/brokers/groww/orders.py` | `test_groww_order_status` | P0 | GRW-007 | [✓] |
| GRW-011 | Order list with pagination (max page size 100) and optional segment filter | Pagination loop terminates and aggregates all pages; page size >100 is clamped locally | `backend/app/brokers/groww/orders.py` | `test_groww_order_list_pagination` | P1 | GRW-010 | [✓] |
| GRW-012 | Trade list for an order (max page size 50) mapped to fill records | Multiple partial fills are returned as distinct trades with exchange trade ids | `backend/app/brokers/groww/orders.py` | `test_groww_trade_list` | P1 | GRW-010 | [✓] |
| GRW-013 | Positions fetch normalised into the internal position model (segment, product, net qty, average price) | Normalisation covers CASH and FNO; short positions carry negative quantity | `backend/app/brokers/groww/portfolio.py` | `test_groww_positions_normalisation` | P0 | GRW-002 | [✓] |
| GRW-014 | Holdings fetch normalised (ISIN, quantity, average price, pledged/demat quantities where provided) | Holdings map to instruments via ISIN; unmapped ISIN raises a flagged warning rather than silently dropping | `backend/app/brokers/groww/portfolio.py` | `test_groww_holdings` | P1 | GRW-013 | [✓] |
| GRW-015 | Margin / available-funds fetch, including required-margin calculation where the API exposes it | Available margin is surfaced to the dashboard and to the risk engine as a hard input | `backend/app/brokers/groww/margin.py` | `test_groww_margin` | P0 | GRW-002 | [✓] |
| GRW-016 | Instrument master download, parse, and upsert into `instruments`, scheduled daily pre-open | Full instrument set loads; lot sizes and expiries populated for FNO; re-running is idempotent | `backend/app/instruments/loader.py`, `runtime.py`; `db/models/instrument_snapshot.py`; `scripts/import_instruments.py` (audited public import and opt-in application scheduler; actual pre-open operation blocked by unverified calendar) | `test_instrument_loader`; `test_instrument_source_and_audit_commit_together`; `test_instrument_runtime.py`; `test_instrument_contract_validation.py`; `test_instrument_postgres.py` | P0 | DB-004 | [~] |
| GRW-017 | Live data: LTP batch (≤50), OHLC batch (≤50), full quote with depth, circuit limits, OI and 52-week range | Batch chunking splits a 130-symbol request into 3 calls; quote model exposes every documented field | `backend/app/brokers/groww/marketdata.py` | `test_groww_live_data_batching` | P0 | GRW-004 | [✓] |
| GRW-018 | Historical candles with per-interval range enforcement (1m→7d, 5m→15d, 10m→30d, 60m→150d, 240m→365d, 1440m→1080d) | A request exceeding the interval's max range is automatically split into compliant windows and stitched without gaps or duplicates | `backend/app/brokers/groww/historical.py` | `test_groww_historical_windowing` | P0 | GRW-002 | [✓] |
| GRW-019 | Option chain fetch (all strikes, CE/PE, LTP, OI, volume, Greeks) for an underlying and expiry | Chain parses into a typed model with per-strike CE/PE legs; missing strikes are reported, not silently skipped | `backend/app/brokers/groww/options.py` | `test_groww_option_chain` | P1 | GRW-002 | [✓] |
| GRW-020 | Greeks fetch for an individual contract (delta, gamma, theta, vega, rho, IV) | Values parsed as Decimals; absent Greeks map to `None`, never to `0` | `backend/app/brokers/groww/options.py` | `test_groww_greeks_parsing` | P1 | GRW-019 | [✓] |
| GRW-021 | WebSocket feed wrapper: LTP, index value, market depth, equity order updates, FNO order updates, FNO position updates | Subscribe/unsubscribe lifecycle managed; callback and polling modes both supported | `backend/app/brokers/groww/feed.py` | `test_groww_feed_wrapper` | P1 | GRW-001 | [x] |
| GRW-022 | Feed subscription budget enforced at ≤1000 instruments with a priority-based eviction policy | Subscribing the 1001st instrument evicts the lowest-priority subscription and logs it | `backend/app/brokers/groww/feed.py` | `test_feed_subscription_cap` | P1 | GRW-021 | [✓] |
| GRW-023 | Feed auto-reconnect with exponential backoff, resubscription of the prior set, and a gap warning on the event bus | Simulated disconnect reconnects and restores subscriptions; a `FEED_GAP` event is emitted with the outage window | `backend/app/brokers/groww/feed.py` | `test_feed_reconnect` | P1 | GRW-022 | [✓] |
| GRW-024 | Every Groww request/response is logged with correlation id, latency, rate-limit bucket, and secrets redacted | Log assertions confirm no token appears in any log record | `backend/app/brokers/groww/client.py` | `test_groww_request_logging_redaction` | P0 | SEC-004 | [✓] |
| GRW-025 | Broker-capability declaration so higher layers do not assume unsupported features (e.g. validity is `DAY` only) | A strategy requesting IOC/GTT validity is rejected with a capability error at proposal validation, not at the broker | `backend/app/brokers/groww/capabilities.py` | `test_broker_capabilities` | P1 | ARCH-004 | [✓] |
| GRW-026 | Contract-fixture test suite: recorded/handwritten JSON fixtures for every endpoint, asserting the parser against the documented schema | Every implemented endpoint has at least one success and one failure fixture; fixtures live under `tests/fixtures/groww/` and are clearly marked non-production | `backend/tests/fixtures/groww/` | `test_groww_contract_fixtures` | P0 | GRW-007 | [✓] |
| GRW-027 | A single documented switch (`BROKER_PROVIDER=groww|paper`) moves the engine from paper to real Groww with no code change | Switching the env var and restarting selects the other provider; test asserts factory behaviour for both values | `backend/app/brokers/factory.py` | `test_broker_switch_by_config` | P0 | ARCH-006 | [✓] |

### 2.6 Authentication & session (`AUTH`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| AUTH-001 | Support both documented Groww auth flows: (A) API key + secret, (B) TOTP token + TOTP secret via `pyotp` | Flow selected by config; both construct the documented `POST /token/api/access` request; TOTP code is generated at call time | `backend/app/brokers/groww/auth.py` | `test_groww_auth_flows` | P0 | GRW-001 | [✓] |
| AUTH-002 | Access-token cache with expiry awareness: flow A tokens are treated as daily, flow B tokens as non-expiring but revalidated | Token is reused across calls; a 401/`GA005` triggers exactly one re-auth and a single retry of the original call | `backend/app/brokers/groww/auth.py` | `test_token_cache_and_refresh` | P0 | AUTH-001 | [✓] |
| AUTH-003 | Token-endpoint budget guard respecting the documented 150 requests / 24 h cap on flow A | A counter blocks the 151st daily token request and raises a clear, actionable error instead of hammering the endpoint | `backend/app/brokers/groww/auth.py`, `budget.py`; migration 0014 (durable shared rolling window before exchange; failed/cancelled calls consume reservations; pre-migration/external usage not observable, operational acceptance partial) | `test_token_budget_guard`; `test_groww_failed_auth_budget.py`; `test_token_attempt_budget.py`; `test_token_budget_postgres.py` (actual concurrent connections) | P0 | AUTH-002 | [~] |
| AUTH-004 | Auth failure is a **critical** health-check failure that disables trading rather than degrading silently | With invalid credentials, `/health/ready` reports auth FAIL and every trading endpoint returns 423 | `backend/app/monitoring/broker_checks.py` (simulated/wrong adapters cannot verify Groww; real credentials unavailable) | `test_broker_health_identity.py`, health/watchdog tests; external verification pending | P0 | MON-004 | [x] |
| AUTH-005 | Credentials are loaded only from environment/secret store; never committed, never logged, never returned by any API | Repo scan finds no credential literals; API responses are asserted free of token fields | `backend/app/config.py` | `test_no_credential_leakage` | P0 | SEC-001 | [✓] |
| AUTH-006 | Startup connectivity probe (read-only call such as profile/margin) verifying credentials before any trading is permitted | Probe result recorded in `system_state`; failure blocks arming | `backend/app/monitoring/healthchecks.py` | `test_startup_auth_probe` | P0 | AUTH-002 | [x] |
| AUTH-007 | Dashboard authentication is independent of broker auth: local user login with hashed password and JWT session | Password hashed with Argon2id; JWT expiry and refresh implemented; brute-force attempts rate-limited | `backend/app/api/auth.py`, `app/security/` | `backend/tests/integration/test_auth.py` | P0 | SEC-002 | [✓] |

### 2.7 Security (`SEC`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| SEC-001 | Secrets management: `.env` excluded from git, `.env.example` documents every key, optional support for a secret file mount | `git check-ignore .env` passes; example file lists all required keys with placeholder values | `.gitignore`, `.env.example` | `test_env_example_covers_settings` | P0 | ARCH-009 | [✓] |
| SEC-002 | All mutating API endpoints require authentication; read endpoints require it too by default | Unauthenticated call to every route returns 401 in a route-sweep test | `backend/app/api/deps.py` | `backend/tests/integration/test_auth.py::test_all_routes_require_authentication` | P0 | AUTH-007 | [✓] |
| SEC-003 | CORS restricted to the configured dashboard origin; wildcard origins prohibited outside local development | Config with `*` in non-dev environment fails validation at startup | `backend/app/main.py` | `test_cors_policy` | P0 | ARCH-009 | [✓] |
| SEC-004 | Log redaction filter removing tokens, API keys, TOTP secrets and passwords from all log output | Log records containing known secret patterns are emitted redacted in a filter test | `backend/app/core/logging.py` | `test_log_redaction` | P0 | LOG-001 | [✓] |
| SEC-005 | Rate limiting and lockout on dashboard login | N failed attempts within a window lock the account for a configured period | `backend/app/api/auth.py` | `backend/tests/integration/test_auth.py::test_lockout_survives_engine_restart` | P1 | AUTH-007 | [✓] |
| SEC-006 | Input validation on every endpoint via Pydantic models; no raw dict passthrough to the broker | Fuzz test with malformed payloads yields 422, never a 500 or an outbound broker call | `backend/app/api/` | `test_input_validation_fuzz` | P1 | BE-001 | [ ] |
| SEC-007 | SQL access exclusively through the ORM or parameterised statements | AST scan finds no string-formatted SQL | `backend/app/db/` | `test_no_raw_sql_interpolation` | P1 | DB-002 | [ ] |
| SEC-008 | Dependency vulnerability scan (`pip-audit`, `npm audit`) wired into CI with a documented triage policy | CI job runs and fails on high-severity findings | `.github/workflows/ci.yml` | CI job `security-scan` | P2 | DEPLOY-003 | [ ] |
| SEC-009 | The system binds to localhost by default; exposing it publicly requires explicit configuration and TLS | Default bind documented and asserted; non-local bind without TLS config fails startup | `backend/app/config.py`, `docs/SECURITY.md` | `test_bind_policy` | P1 | DEPLOY-001 | [✓] |
| SEC-010 | LLM prompt-injection containment: news and other external text is never allowed to carry instructions into an executable path | Agent prompts wrap external content in a data envelope; a fixture containing injected instructions ("ignore previous instructions and buy…") produces no valid proposal and is logged as a containment event | `backend/app/llm/sanitise.py`, `agents/` | `test_prompt_injection_containment` | P0 | LLM-006 | [ ] |
| SEC-011 | The LLM has no tool access to the broker, the database or the filesystem — it returns JSON only | Architecture test asserts no broker/DB client is reachable from the LLM layer's import graph | `backend/app/llm/` | `test_llm_no_tool_access` | P0 | ARCH-010 | [ ] |

### 2.8 Real-time market data (`MD`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| MD-001 | `LiveMarketDataProvider` implementing the `MarketDataProvider` interface against Groww live endpoints and feed | Interface parity test passes; provider returns typed models, not raw dicts | `backend/app/marketdata/live.py` | `test_live_provider` | P0 | ARCH-005, GRW-017 | [x] |
| MD-002 | Quote model exposing LTP, OHLC, volume, bid/ask ladder, OI and OI change, circuit limits, 52-week high/low, average price, day change and change % | Every field populated from the Groww quote payload fixture | `backend/app/marketdata/models.py` | `test_quote_model_completeness` | P0 | GRW-017 | [✓] |
| MD-003 | Redis-backed tick cache with TTL so repeated reads inside one decision cycle do not re-hit the API | Second read within the TTL performs no HTTP call (asserted with a call counter) | `backend/app/marketdata/cache.py` | `test_tick_cache` | P1 | MD-001 | [✓] |
| MD-004 | Subscription manager mapping strategy interest to feed subscriptions within the 1000-instrument budget | Adding/removing a strategy adjusts subscriptions; budget never exceeded | `backend/app/marketdata/subscriptions.py` | `test_subscription_manager` | P1 | GRW-022 | [✓] |
| MD-005 | Tick persistence to the `ticks` hypertable for post-trade analysis, at a configurable sampling rate | Ticks appear in the DB; sampling rate honoured; write path is non-blocking | `backend/app/marketdata/persistence.py` | `test_tick_persistence` | P2 | DB-006 | [ ] |
| MD-006 | Real-time candle aggregation from ticks into 1m/5m/15m/60m bars with correct bar-close boundaries in IST | Aggregated bars match reference bars computed from the same tick fixture; boundary ticks land in the correct bar | `backend/app/marketdata/aggregator.py` | `test_candle_aggregation` | P1 | ARCH-012 | [✓] |
| MD-007 | Index value tracking for NIFTY 50, BANK NIFTY, FINNIFTY and SENSEX | Index values available to strategies and the dashboard, sourced from the index feed | `backend/app/marketdata/indices.py` | `test_index_tracking` | P1 | GRW-021 | [x] |
| MD-008 | **Stale-data detection** (contract §11): data older than a configured threshold is marked stale and blocks new entries | With a frozen clock advanced past the threshold, `is_stale` is true, the strategy receives no fresh signal, and an alert is emitted | `backend/app/marketdata/staleness.py` | `test_stale_data_blocks_entry` | P0 | ARCH-012 | [✓] |
| MD-009 | Data-quality validation: reject non-positive prices, crossed bid/ask, zero-volume-with-price-move anomalies and out-of-circuit prints | Each anomaly class is rejected in tests and recorded as a data-quality event | `backend/app/marketdata/validation.py` | `test_data_quality_rules` | P1 | MD-002 | [✓] |
| MD-010 | Market-data failover: if the websocket feed is down, fall back to REST polling at a rate-limit-safe cadence, with degraded status surfaced | Simulated feed outage switches to polling within N seconds and sets health to DEGRADED | `backend/app/marketdata/failover.py` | `test_feed_failover_to_rest` | P1 | GRW-023 | [x] |
| MD-011 | Every market-data record carries a `DataOrigin` tag (`LIVE`, `HISTORICAL`, `REPLAY`, `SYNTHETIC`) | Tag is present on all provider outputs and enforced by ARCH-008 | `backend/app/core/data_origin.py` | `test_data_origin_tagging` | P0 | ARCH-008 | [✓] |

### 2.9 Historical data (`HD`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| HD-001 | `HistoricalMarketDataProvider` implementing the `MarketDataProvider` interface over Groww historical candles plus the local store | Interface parity test passes; reads prefer the local store and fall through to the API for gaps | `backend/app/marketdata/historical.py` | `test_historical_provider` | P0 | ARCH-005, GRW-018 | [✓] |
| HD-002 | Candle ingestion pipeline writing to the `candles` hypertable with upsert semantics | Re-ingesting an overlapping window produces no duplicate rows and no data loss | `backend/app/marketdata/ingest.py` | `test_candle_ingest_idempotent` | P0 | DB-005 | [✓] |
| HD-003 | Gap detection and backfill: identify missing candles across a requested window and fetch only the gaps | A window with a synthetic hole triggers exactly one targeted backfill call | `backend/app/marketdata/backfill.py` | `test_gap_backfill` | P1 | HD-002 | [✓] |
| HD-004 | Trading-calendar-aware gap logic so weekends, holidays and non-session minutes are not treated as gaps | Backfill on a holiday-spanning window issues no calls for non-session periods | `backend/app/marketdata/backfill.py` | `test_gaps_respect_calendar` | P1 | EXCH-001 | [✓] |
| HD-005 | Corporate-action awareness: splits/bonuses flagged so historical price series used for analysis are adjustment-aware | An instrument with a known split shows a flagged discontinuity; unadjusted series are never silently mixed with adjusted ones | `backend/app/marketdata/corporate_actions.py` | `test_corporate_action_flagging` | P2 | HD-002 | [ ] |
| HD-006 | Historical option and futures data captured with OI and volume for F&O instruments | Stored FNO candles carry OI; missing OI is `None`, never `0` | `backend/app/marketdata/ingest.py` | `test_fno_historical_oi` | P2 | DB-005 | [✓] |
| HD-007 | Warm-up loader ensuring every strategy's indicator lookback is satisfied before it is allowed to emit signals | A strategy with a 200-period requirement and 150 available bars is blocked with `InsufficientHistoryError` | `backend/app/marketdata/warmup.py` | `test_warmup_blocks_insufficient_history` | P0 | HD-002 | [✓] |
| HD-008 | Replay provider that feeds stored candles to the engine in chronological order with no look-ahead | Replay test asserts the engine cannot access any bar with `ts > current_ts` | `backend/app/marketdata/replay.py` (nominal bar-close availability; not a full backtest engine) | `test_replay_no_lookahead`, `test_replay_safety.py`: closure, original timestamps, simultaneous callbacks, malformed/contradictory data and rewind refusal | P0 | HD-002 | [✓] |
| HD-009 | Scheduled daily historical sync after market close for all tracked instruments | Sync job completes, writes candles for the session, and records a run summary | `backend/app/marketdata/jobs.py` | `test_daily_sync_job` | P2 | BE-012 | [ ] |
| HD-010 | **Documented constraint:** Groww intraday history is limited to 3 months; the system continuously archives intraday candles locally to build a longer private history | Local archive grows daily; a coverage report shows per-instrument earliest available intraday bar; the 3-month API limit is surfaced in the backtest UI | `backend/app/marketdata/archive.py`, `docs/DATA.md` | `test_archive_coverage_report` | P1 | HD-002 | [✓] |
| HD-011 | Pluggable alternate historical source interface so a longer-history vendor can be added later without engine changes | A second provider can be registered and selected by config; interface test passes for a stub implementation | `backend/app/marketdata/base.py` | `test_alternate_history_source` | P3 | ARCH-005 | [~] |

### 2.10 Exchange rules & calendar (`EXCH`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| EXCH-001 | Trading calendar with NSE/BSE holidays, maintained in a data file and overridable, covering the current and next year | Known holidays return `is_trading_day=False`; the file's provenance and update procedure are documented | `backend/app/core/calendar.py`, `data/holidays.json` (year completeness requires available timestamp; NSE 2026 circulars/publication cutoffs recorded; BSE, remaining amendments/special times and 2027 coverage unverified) | `test_trading_calendar`; `test_calendar_publication.py` | P0 | ARCH-012 | [~] |
| EXCH-002 | Session-phase detection: pre-open (09:00–09:15), regular (09:15–15:30), closing, post-market, closed — in IST | Phase is correct at each boundary minute with an injected clock | `backend/app/core/sessions.py` | `test_session_phases` | P0 | EXCH-001 | [✓] |
| EXCH-003 | Segment-specific session awareness (CASH vs FNO) and configurable special sessions (e.g. Muhurat trading) | A configured special session is recognised; the default is closed | `backend/app/core/sessions.py`, `calendar.py`, `entry_windows.py` (explicit unavailable hours block entries and degrade market health) | `test_special_sessions`, `test_unknown_session.py`, `test_paper_worker.py` | P2 | EXCH-002 | [✓] |
| EXCH-004 | Tick-size and lot-size compliance: every order price is rounded to the instrument tick size and every quantity is a lot multiple for FNO | Non-compliant price/quantity is corrected or rejected before reaching the broker; test covers both | `backend/app/execution/compliance.py` | `test_tick_lot_compliance` | P0 | DB-004 | [ ] |
| EXCH-005 | Circuit-limit awareness: orders priced outside the instrument's upper/lower circuit are rejected locally | An order beyond the circuit limit is rejected with a clear reason and never sent | `backend/app/marketdata/circuits.py`, `stored_quotes.py`, `execution/paper.py`, `brokers/paper/engine.py` (PAPER preflight/dispatch refresh, LIVE-origin missing-band refusal, fill bounds and audit/replay recovery; broader modes and historical missing-band coverage pending) | `test_paper_circuits.py`, `test_circuit_fills.py`, `test_stored_circuit_quotes.py` | P1 | MD-002 | [~] |
| EXCH-006 | Expiry-day handling for F&O: expiring contracts are identified, new entries blocked past a configured cutoff, and open positions flagged for mandatory action | On expiry day after cutoff, new entries in the expiring contract are refused and an alert lists positions requiring action | `backend/app/execution/expiry.py`, `freshness.py`, `hygiene.py`, `paper.py`, `trading/worker.py` (configured entry veto, durable position alerts and recoverable pending-entry cancellation before settlement; broader-mode/external acceptance pending) | `test_expiry_admission.py`, `test_expiry_monitor.py`, `test_expiry_cancellation.py`, `test_order_hygiene.py` | P1 | EXCH-001 | [~] |
| EXCH-007 | Intraday square-off cutoff for MIS products enforced ahead of the broker's own auto-square-off | At the configured cutoff, remaining MIS positions generate exit intents; test uses an injected clock | `backend/app/execution/squareoff.py` | `test_mis_squareoff_cutoff` | P0 | EXCH-002 | [ ] |
| EXCH-008 | Ban-period / restricted-instrument awareness for F&O, with a maintained list and a hard block on new entries | An instrument on the ban list cannot receive a new-entry order; exits remain permitted | `backend/app/fno/restrictions.py`: owner-admitted dated NSE CSV, exact document/hash/history, daily/origin selection and shared entry veto; authenticated Risk-page controls. Scheduled acquisition, other-exchange sourcing and positive F&O OMS lifecycle remain pending | `backend/tests/unit/test_fno_ban_report.py`, `backend/tests/integration/test_fno_bans.py`, `test_fno_ban_browser.py`: parsing, shared rejection, restart, dated revisions, scope, authentication and actual browser admission; no claim of enabled F&O execution | P2 | EXCH-006 | [~] |

### 2.11 Equity analysis (`EQ`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| EQ-001 | Configurable trading universe (index constituents, liquidity floor, price band, F&O eligibility) resolved daily | Universe resolution produces a deterministic instrument list from stored data; each exclusion carries a reason | `backend/app/analysis/universe.py` | `backend/tests/unit/test_equity.py::test_universe_resolution` | P1 | GRW-016 | [~] |
| EQ-002 | Liquidity screen using average traded value and spread, excluding instruments the account cannot exit cleanly | Instruments below the configured ADV threshold are excluded; threshold is expressed relative to intended position size | `backend/app/analysis/liquidity.py` | `backend/tests/unit/test_equity.py::test_liquidity_screen` | P1 | HD-002 | [✓] |
| EQ-003 | Relative-strength ranking of universe members against their index over configurable lookbacks | Ranking on a fixture reproduces hand-computed order exactly | `backend/app/analysis/relative_strength.py` | `backend/tests/unit/test_equity.py::test_relative_strength` | P2 | TA-001 | [✓] |
| EQ-004 | Sector/industry classification attached to instruments, enabling sector exposure limits and sector rotation views | Every active universe member has a sector; unmapped instruments are reported | `backend/app/analysis/sectors.py` | `backend/tests/unit/test_equity.py::test_sector_mapping` | P2 | DB-004 | [✓] |
| EQ-005 | Volatility profile per instrument (ATR%, realised volatility, beta to index) refreshed daily | Computed values match reference calculations on a fixture within tolerance | `backend/app/analysis/volatility.py` | `backend/tests/unit/test_equity.py::test_volatility_profile` | P1 | TA-001 | [~] |
| EQ-006 | Gap and pre-open analysis producing a ranked list of gap-up/gap-down candidates with context | Gap % computed against the prior close; pre-open data used when available, with a documented fallback | `backend/app/analysis/preopen.py` | `backend/tests/unit/test_equity.py::test_gap_analysis` | P2 | EXCH-002 | [✓] |
| EQ-007 | Event-window blackout: instruments with results/board meetings inside a configured window are flagged for restricted strategies | Flagged instruments are visible to the risk engine and to the AI as context | `backend/app/analysis/events.py`, `backend/app/risk/event_controls.py`, `backend/app/llm/service.py`: owner-published corporate/event windows reach shared PAPER vetoes, archived decisions and server-resolved advisory requests. Automatic convergence/refresh of all corporate and reference calendar sources remains partial; unconfigured is not clear | `backend/tests/unit/test_equity.py::test_event_blackout_flagging`, `backend/tests/integration/test_event_controls.py`, `test_event_advisory.py`, `test_llm_service.py`: corporate publication/scope, stale blackout veto, real worker-to-HTTP fixture context, immutable receipts and preserved exits | P2 | FUND-006 | [~] |

### 2.12 Fundamental analysis (`FUND`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| FUND-001 | Fundamental data ingestion interface with a pluggable source, storing point-in-time snapshots | Snapshot carries an `as_of` date; the same query at an earlier date returns the older value (no look-ahead) | `backend/app/analysis/fundamental/source.py` | `backend/tests/integration/test_fundamentals.py::test_fundamental_point_in_time` | P2 | DB-015 | [✓] |
| FUND-002 | Core valuation metrics: P/E, P/B, EV/EBITDA, dividend yield, market cap, with per-metric freshness | Metrics computed or ingested and exposed via API; missing metrics are `None`, never `0` | `backend/app/analysis/fundamental/valuation.py` | `backend/tests/unit/test_fundamentals.py::test_valuation_metrics` | P2 | FUND-001 | [✓] |
| FUND-003 | Growth and profitability metrics: revenue growth, EPS growth, ROE, ROCE, operating and net margins | Values match a hand-computed fixture from stored statements | `backend/app/analysis/fundamental/quality.py` | `backend/tests/unit/test_fundamentals.py::test_growth_profitability` | P2 | FUND-001 | [✓] |
| FUND-004 | Balance-sheet health: debt/equity, interest coverage, current ratio, promoter-pledge flag where available | Health score composed from documented components; each component individually inspectable | `backend/app/analysis/fundamental/health.py` | `backend/tests/unit/test_fundamentals.py::test_balance_sheet_health` | P2 | FUND-001 | [✓] |
| FUND-005 | Composite fundamental score with a documented, versioned formula and per-component contributions | Score reproducible from components; formula version stored alongside the score | `backend/app/analysis/fundamental/score.py` | `backend/tests/unit/test_fundamentals.py::test_fundamental_score` | P2 | FUND-004 | [✓] |
| FUND-006 | Corporate calendar: results dates, dividends, splits, bonuses, board meetings, ingested and queryable | Upcoming events for an instrument returned for a date range; feeds EQ-007 | `backend/app/analysis/fundamental/calendar.py` | `backend/tests/integration/test_corporate_calendar.py::test_corporate_calendar` | P2 | FUND-001 | [✓] |
| FUND-007 | Fundamental data is explicitly excluded from intraday entry signals unless a strategy declares it as an input | A strategy not declaring fundamentals receives no fundamental fields (asserted by the strategy context builder) | `backend/app/strategies/context.py` | `backend/tests/unit/test_strategies.py::test_fundamentals_opt_in` | P2 | STRAT-002 | [✓] |
| FUND-008 | Stale-fundamentals guard: data older than a configured age is marked stale and excluded from scoring | A stale snapshot is excluded and the exclusion is logged | `backend/app/analysis/fundamental/source.py` | `backend/tests/integration/test_fundamentals.py::test_stale_fundamentals` | P2 | FUND-001 | [✓] |

### 2.13 Technical analysis (`TA`)

All indicators are pure functions over an OHLCV series, unit-tested against hand-computed reference values.

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| TA-001 | Indicator framework: vectorised, deterministic, NaN-safe, with explicit minimum-lookback declaration per indicator | Calling an indicator with fewer bars than its lookback raises rather than returning garbage | `backend/app/analysis/technical/base.py` | `test_indicator_framework` | P0 | — | [✓] |
| TA-002 | Moving averages: SMA, EMA, WMA, VWAP (session-anchored) | Each matches a reference fixture to 1e-8; VWAP resets at session open | `backend/app/analysis/technical/ma.py` | `test_moving_averages` | P0 | TA-001 | [✓] |
| TA-003 | Momentum indicators: RSI (Wilder), MACD with signal and histogram, Stochastic, Rate of Change | Values match reference fixtures; Wilder smoothing verified explicitly | `backend/app/analysis/technical/momentum.py` | `test_momentum_indicators` | P0 | TA-001 | [✓] |
| TA-004 | Volatility indicators: ATR (Wilder), Bollinger Bands, Keltner Channels, historical volatility | ATR matches reference; band width correct at known inputs | `backend/app/analysis/technical/volatility.py` | `test_volatility_indicators` | P0 | TA-001 | [✓] |
| TA-005 | Volume indicators: OBV, volume moving average, relative volume, VWAP deviation | Reference-matched; relative volume handles partial sessions correctly | `backend/app/analysis/technical/volume.py` | `test_volume_indicators` | P1 | TA-001 | [✓] |
| TA-006 | Trend indicators: ADX/DI (Wilder), Supertrend, Donchian channels | Reference-matched on fixtures including flat and gapping series | `backend/app/analysis/technical/trend.py` | `test_trend_indicators` | P1 | TA-001 | [✓] |
| TA-007 | Support/resistance and pivot levels: classic and Fibonacci pivots, prior-day high/low/close, swing highs/lows | Levels computed from prior session; swing detection parameters documented | `backend/app/analysis/technical/levels.py` | `test_levels` | P1 | TA-001 | [✓] |
| TA-008 | Multi-timeframe analysis helper aligning higher-timeframe context to the trading timeframe without look-ahead | A 60m indicator value used at 09:20 on a 5m chart reflects only completed 60m bars | `backend/app/analysis/technical/mtf.py` | `test_mtf_no_lookahead` | P0 | TA-001 | [✓] |
| TA-009 | Candlestick/price-action features: body/wick ratios, inside/outside bars, range expansion, consolidation detection | Feature extraction matches labelled fixtures | `backend/app/analysis/technical/price_action.py` | `test_price_action_features` | P2 | TA-001 | [✓] |
| TA-010 | Indicator results are cached per (instrument, interval, indicator, params, last_bar_ts) and invalidated on new bars | Cache hit avoids recomputation; a new bar invalidates | `backend/app/analysis/technical/cache.py` | `test_indicator_cache` | P2 | TA-001 | [✓] |
| TA-011 | A technical-snapshot builder assembling all indicator values for an instrument into one typed object for strategies and the AI | Snapshot contains every declared indicator with its parameters and the bar timestamp it was computed at | `backend/app/analysis/technical/snapshot.py` | `test_technical_snapshot` | P1 | TA-002…TA-009 | [✓] |
| TA-012 | No indicator is added unless a strategy uses it (contract §15 — no ornamental complexity) | Every indicator module is referenced by at least one strategy or analysis consumer; an unused-indicator test fails the build | `backend/tests/unit/test_no_orphan_indicators.py` | `test_no_orphan_indicators` | P1 | STRAT-001 | [✓] |

### 2.14 News intelligence & verification (`NEWS`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| NEWS-001 | News ingestion service with pluggable sources (RSS/API), scheduled polling, and per-source rate limiting | Configured sources are polled on schedule; items persist with source attribution and fetch timestamps | `backend/app/news/ingest.py; backend/app/news/polling.py; backend/app/news/providers.py; backend/app/news/runtime.py` | `backend/tests/integration/test_news_ingest.py; backend/tests/integration/test_news_polling.py; backend/tests/integration/test_news_runtime.py; backend/tests/unit/test_news_fetch.py` | P2 | DB-014 | [~] |
| NEWS-002 | Deduplication across sources using URL canonicalisation plus content similarity | The same story from three sources collapses to one item listing three sources | `backend/app/news/dedupe.py; backend/app/news/ingest.py; backend/app/api/news.py` | `backend/tests/integration/test_news_dedupe.py; frontend/src/NewsArticleImport.test.tsx; frontend/tests/news-sources-browser.mjs` | P2 | NEWS-001 | [~] |
| NEWS-003 | Entity extraction mapping articles to instruments (company name, symbol, ISIN aliases) with a confidence score | Known article fixtures map to the correct instruments; ambiguous names below threshold are left unmapped rather than guessed | `backend/app/news/entities.py; backend/app/news/ingest.py; backend/app/api/news.py` | `backend/tests/integration/test_news_entities.py; frontend/src/NewsArticleImport.test.tsx; frontend/tests/news-sources-browser.mjs` | P2 | GRW-016 | [~] |
| NEWS-004 | Source credibility tiering (exchange filings/regulator > established financial media > aggregators > social) with configurable weights | Each item carries a credibility tier; tier influences downstream weighting deterministically | `backend/app/news/sources.py; backend/app/news/verification.py` | `backend/tests/integration/test_news_controls.py; backend/tests/integration/test_news_verification.py` | P2 | NEWS-001 | [~] |
| NEWS-005 | **Verification rule:** a news item may not be used as a trade justification unless corroborated by ≥2 independent sources or one Tier-1 primary source (exchange filing/regulator) | An uncorroborated item yields `verification_status=UNVERIFIED` and the proposal validator rejects proposals citing it as primary evidence | `backend/app/news/verification.py; backend/app/news/readiness.py; backend/app/news/research.py; backend/app/agents/validation.py` | `backend/tests/integration/test_news_verification.py; backend/tests/integration/test_news_consumer_readiness.py; backend/tests/integration/test_news_research_admission.py` | P1 | NEWS-004 | [~] |
| NEWS-006 | Recency and staleness handling: news older than a configurable window is not treated as actionable | A 3-day-old item cannot be cited as an intraday catalyst | `backend/app/news/verification.py; backend/app/news/readiness.py` | `backend/tests/integration/test_news_verification.py; backend/tests/integration/test_news_consumer_readiness.py` | P2 | NEWS-005 | [~] |
| NEWS-007 | Claude-assisted interpretation producing structured output (event type, affected entities, direction, magnitude estimate, confidence, time horizon) | Output validates against a strict JSON schema; free text is confined to a `summary` field that cannot drive execution | `backend/app/news/interpret.py; backend/app/news/production.py; backend/app/news/runtime.py; backend/app/api/news.py; frontend/src/NewsInterpretation.tsx` | `backend/tests/integration/test_news_interpretation.py; backend/tests/integration/test_news_production.py; frontend/src/NewsInterpretation.test.tsx; frontend/tests/news-sources-browser.mjs` | P2 | LLM-003 | [~] |
| NEWS-008 | Contradiction detection: conflicting reports on the same event are surfaced rather than averaged away | Two fixtures with opposite claims produce a `CONFLICTING` status and suppress actionability | `backend/app/news/conflicts.py; backend/app/news/ingest.py; backend/app/news/interpret.py; backend/app/api/news.py` | `backend/tests/integration/test_news_conflicts.py; frontend/src/NewsInterpretation.test.tsx; frontend/tests/news-sources-browser.mjs` | P2 | NEWS-002 | [~] |
| NEWS-009 | **Hallucination guard:** any factual claim the LLM makes about a news item must be traceable to ingested source text | Claims not substring/semantically anchored to stored source text are dropped and logged as `UNSOURCED_CLAIM` | `backend/app/news/interpret.py` | `backend/tests/integration/test_news_interpretation.py` | P1 | NEWS-007 | [~] |
| NEWS-010 | Corporate-announcement channel (exchange filings) treated as a distinct, highest-trust source type | Filing-sourced items are Tier 1 and exempt from the two-source rule | `backend/app/news/sources/filings.py` | `test_filings_tier1` | P2 | NEWS-004 | [ ] |
| NEWS-011 | News-driven halts: a high-severity negative event on a held instrument raises an alert and blocks new entries in it | Fixture event triggers the block and the alert | `backend/app/news/reactions.py`, `backend/app/risk/news_halts.py`: owner-enabled admitted-evidence CASH/PAPER halt; shared decision/entry/final safety gates, audited release, Risk UI and durable alert outbox. Cross-process/PostgreSQL concurrency and derivative-family integration pending; external delivery unverified | `backend/tests/integration/test_news_halts.py`, `test_news_halt_recovery.py`: real admission/holding/worker, negative decision, restart, protected exit, rollback, stale/future/origin/severity/policy gates, review races, release dedupe/new-evidence re-latch, corruption, API and actual browser | P2 | RISK-014 | [~] |
| NEWS-012 | News service failure degrades gracefully: strategies that require news enter `NO TRADE`; strategies that do not, continue | With the news service down, a news-dependent strategy produces no signal and logs the reason | `backend/app/news/availability.py; backend/app/strategies/engine.py; backend/app/agents/pipeline.py` | `backend/tests/integration/test_news_availability.py; backend/tests/integration/test_reference_worker.py` | P1 | STRAT-009 | [~] |

### 2.15 Market sentiment (`SENT`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| SENT-001 | Per-instrument news sentiment score aggregated from verified items, weighted by credibility and recency | Score is reproducible from the stored item set; weighting formula versioned | `backend/app/news/sentiment.py`, `backend/app/news/research_sentiment.py`, shared strategy/decision gates and existing Market view; explicit owner opt-in for uncalibrated advisory labels, not fact verification; bounded automatic production integrated, external data/semantic validation pending | `backend/tests/unit/test_sentiment.py::test_news_sentiment_aggregation`, `backend/tests/integration/test_research_sentiment.py`, admission browser | P2 | NEWS-005 | [~] |
| SENT-002 | Market-wide sentiment from index behaviour, advance/decline, and volatility measures | Composite computed from stored data; components individually inspectable | `backend/app/analysis/sentiment/market.py` | `backend/tests/unit/test_sentiment.py::test_market_sentiment` | P2 | MD-007 | [~] |
| SENT-003 | Derivatives-based sentiment: put/call ratio, OI build-up interpretation (long build-up, short covering, etc.) | Build-up classification matches the standard price/OI matrix on fixtures | `backend/app/fno/oi_analysis.py` | `backend/tests/unit/test_sentiment.py::test_oi_buildup_classification` | P1 | OC-003 | [✓] |
| SENT-004 | India VIX tracking with regime thresholds | VIX value retrieved and classified into configured bands | `backend/app/analysis/sentiment/vix.py` | `backend/tests/unit/test_sentiment.py::test_vix_bands` | P1 | MD-007 | [✓] |
| SENT-005 | Sentiment is an input, never a standalone trigger: no strategy may enter on sentiment alone | Strategy validation rejects any strategy whose entry conditions reference only sentiment | `backend/app/strategies/validation.py` | `backend/tests/unit/test_strategies.py::test_sentiment_only_contract_rejected` | P2 | STRAT-002 | [✓] |

### 2.16 Market regime detection (`REG`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| REG-001 | Regime classifier producing a typed regime label: `TRENDING_UP`, `TRENDING_DOWN`, `RANGING`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `EVENT_RISK` | Classifier is deterministic; labelled fixtures reproduce expected labels | `backend/app/analysis/regime/classifier.py` | `backend/tests/unit/test_regime.py::test_regime_classifier` | P1 | TA-006, SENT-004 | [✓] |
| REG-002 | Regime inputs are documented and bounded: index trend (ADX/MA structure), realised and implied volatility, breadth, and event calendar | Each input contributes a recorded numeric value in the classification output | `backend/app/analysis/regime/inputs.py`; `backend/app/trading/regime_sources.py`; `docs/PAPER_REGIME_SOURCES.md` | `backend/tests/unit/test_regime.py::test_regime_inputs_recorded`; `backend/tests/integration/test_regime_sources.py` | P1 | REG-001 | [✓] |
| REG-003 | Regime hysteresis preventing rapid flip-flopping between labels | A borderline series does not oscillate more than the configured maximum per session | `backend/app/analysis/regime/classifier.py` | `backend/tests/unit/test_regime.py::test_regime_hysteresis` | P1 | REG-001 | [✓] |
| REG-004 | Strategies declare which regimes they are permitted to trade; the engine suppresses them in other regimes | A trend strategy produces no signal in `RANGING` and the suppression is logged with the regime id | `backend/app/strategies/base.py` | `backend/tests/integration/test_strategies.py::test_regime_gating` | P1 | STRAT-002 | [✓] |
| REG-005 | Regime history persisted with timestamps so trades can be attributed to the regime they were taken in | Journal entries carry the regime id active at entry | `backend/app/analysis/regime/history.py`, `backend/app/execution/paper.py`, `backend/app/api/workspace.py` | `backend/tests/integration/test_regime_history.py::test_regime_history_attribution`, `backend/tests/integration/test_reference_regime.py`, `backend/tests/integration/test_regime_sources.py` (provider-driven open-position restart) | P2 | DB-013 | [✓] |
| REG-006 | `EVENT_RISK` regime automatically engaged around scheduled high-impact events (policy decisions, major results, expiry) | On a configured event date the regime is `EVENT_RISK` and affected strategies are suppressed | `backend/app/analysis/regime/events.py` | `backend/tests/integration/test_strategies.py::test_event_risk_suppression` | P2 | FUND-006 | [✓] |

### 2.17 Intraday trading (`INTRA`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| INTRA-001 | Intraday trading loop running on a fixed cadence within the regular session only, producing at most one decision cycle at a time | Overlapping cycles are prevented by a lock; cycles do not run pre-open or post-close | `backend/app/execution/intraday_loop.py` | `test_intraday_loop_cadence` | P1 | BE-012, EXCH-002 | [~] |
| INTRA-002 | No-entry windows: configurable blackout at the open and before the square-off cutoff | Entries attempted inside a blackout are refused with a logged reason | `backend/app/core/entry_windows.py`, shared pipeline, PAPER preflight/dispatch and worker | `test_entry_windows.py` (unit/integration); boundaries, special sessions, incomplete calendar, direct-entry rejection, replay/API evidence and exits during blackout | P1 | EXCH-007 | [✓] |
| INTRA-003 | Per-day intraday trade-count cap enforced across all strategies | The (N+1)th entry of the day is refused by the risk engine | `backend/app/risk/entry_history.py`, `entry_policy.py`, shared pipeline and PAPER preflight/dispatch | `test_entry_history.py`, `test_entry_policy.py`; durable intent/fill history, restart, cancellations, replay and API evidence | P1 | RISK-006 | [✓] |
| INTRA-004 | Cooling-off period after a losing trade in the same instrument | A new entry in that instrument inside the window is refused | `backend/app/risk/entry_history.py`, `entry_policy.py`, shared pipeline and PAPER preflight/dispatch | `test_entry_history.py`, `test_entry_policy.py`; recorded net losses, unknown economics fail closed, exact boundary, restart and missing journal | P2 | RISK-006 | [✓] |
| INTRA-005 | Intraday product mapping: intraday strategies use `MIS`; positional strategies use `CNC`/`NRML`, declared per strategy | A strategy declaring intraday cannot emit a `CNC` order (validation error) | `backend/app/strategies/products.py`, `agents/pipeline.py`, `execution/paper.py`; immutable declared product, owner positional permission and preflight/dispatch contract checks | `test_entry_contract.py` (signal/proposal/context/registration/order tampering, permission withdrawal, actual CNC PAPER fill); `test_strategy_products.py` (segment matrix) | P1 | STRAT-002 | [✓] |
| INTRA-006 | Mandatory intraday exit: all open MIS positions receive exit intents at the configured cutoff even without a strategy signal | Cutoff test with open positions generates exit intents for each | `backend/app/trading/worker.py` (PAPER integrated; broader real-broker scope pending) | `backend/tests/integration/test_paper_worker.py`; `backend/tests/integration/test_provider_session.py` (provider-driven cutoff, stale exit and restart/EOD) | P0 | EXCH-007 | [~] |
| INTRA-007 | Session P&L tracking updated on every fill and mark-to-market tick, feeding the daily loss limit in real time | Daily loss limit trips within one cycle of the threshold being crossed | `backend/app/portfolio/session_pnl.py` | `test_session_pnl_realtime` | P0 | PNL-002 | [~] |

### 2.18 Futures (`FUT`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| FUT-001 | Futures instrument handling: contract identification by underlying, expiry and lot size; near/next/far month resolution | Resolving "NIFTY current month future" on a fixture date returns the correct contract | `backend/app/fno/futures.py` | `backend/tests/unit/test_futures.py::test_futures_contract_resolution` | P1 | GRW-016 | [✓] |
| FUT-002 | Rollover logic: detect approaching expiry and produce a rollover plan (exit near, enter next) with cost estimate | Within the configured window, a held future generates a rollover recommendation containing both legs | `backend/app/fno/rollover.py` | `backend/tests/unit/test_futures.py::test_futures_rollover_plan` | P2 | EXCH-006 | [✓] |
| FUT-003 | Basis and spread analysis: futures-vs-spot basis, annualised carry, and calendar spread between months | Values match hand-computed fixtures | `backend/app/fno/basis.py` | `backend/tests/unit/test_futures.py::test_futures_basis` | P2 | FUT-001 | [✓] |
| FUT-004 | Futures margin awareness: span+exposure requirement obtained from the broker where available, with a conservative local estimate as fallback, clearly labelled as an estimate | Estimated margins are tagged `ESTIMATED` and the risk engine applies a configurable safety multiplier to them | `backend/app/fno/margin.py` | `test_futures_margin_estimate_labelling` | P1 | GRW-015 | [ ] |
| FUT-005 | Futures P&L computed on lot-size multiples with correct sign for short positions | P&L for a short future matches the hand-computed value including lot multiplication | `backend/app/portfolio/pnl_fno.py` | `test_futures_pnl` | P1 | PNL-001 | [ ] |
| FUT-006 | Mark-to-market monitoring for futures positions with margin-shortfall alerting | A simulated adverse move below the configured margin buffer raises an alert and blocks new entries | `backend/app/fno/margin_monitor.py` | `test_futures_margin_shortfall_alert` | P1 | FUT-004 | [ ] |

### 2.19 Options (`OPT`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| OPT-001 | Option instrument handling: strike, expiry, CE/PE, lot size, and ATM/ITM/OTM classification against the live underlying | Classification correct across a strike ladder fixture at a given spot | `backend/app/fno/options.py` | `backend/tests/unit/test_options.py::test_option_classification` | P1 | GRW-016 | [✓] |
| OPT-002 | Strike selection helpers: ATM, N-strikes-OTM, delta-targeted, and premium-targeted selection | Each selector returns the expected strike on a fixture chain | `backend/app/fno/strike_selection.py` | `backend/tests/unit/test_options.py::test_strike_selection` | P1 | OC-001 | [✓] |
| OPT-003 | Expiry selection: weekly vs monthly, with a minimum-days-to-expiry rule per strategy | A strategy requiring ≥2 DTE cannot select an expiring-today contract | `backend/app/fno/expiry.py` | `backend/tests/unit/test_options.py::test_expiry_selection_rules` | P1 | EXCH-006 | [✓] |
| OPT-004 | Option premium and payoff mathematics: intrinsic value, time value, breakeven, and payoff at expiry for single legs and defined multi-leg structures | Payoff curves match hand-computed values at sampled underlying prices | `backend/app/fno/payoff.py` | `backend/tests/unit/test_options.py::test_option_payoff` | P1 | OPT-001 | [✓] |
| OPT-005 | Supported multi-leg structures defined explicitly: long call/put, covered call, protective put, bull/bear vertical spreads, straddle, strangle, iron condor | Each structure has a builder producing correctly signed legs with matching expiries and lot multiples | `backend/app/fno/structures.py` | `backend/tests/unit/test_options.py::test_option_structures` | P2 | OPT-004 | [✓] |
| OPT-006 | **Naked short options are disabled by default** and require an explicit configuration flag plus a hard per-structure risk cap | With the flag off, a naked-short proposal is rejected by the risk engine with reason `NAKED_SHORT_DISABLED` | `backend/app/risk/rules.py`; `docs/PAPER_OPTIONS.md` (independent default veto implemented; explicit enablement/capped structures remain unsupported) | `test_risk.py::test_option_premium_bounds_and_naked_short_veto_are_independent`; `test_paper_option_account.py` | P0 | RISK-001 | [~] |
| OPT-007 | Multi-leg orders are executed leg-by-leg with an explicit legging-risk policy: defined entry order, partial-leg handling, and automatic unwind on failure | If leg 2 fails after leg 1 fills, the system unwinds leg 1 or raises a `NAKED_LEG` critical alert and blocks further entries | `backend/app/execution/multileg.py` | `test_multileg_partial_failure_unwind` | P0 | EXEC-001 | [ ] |
| OPT-008 | Option liquidity screen: minimum OI, minimum volume and maximum bid-ask spread enforced before any option order | An illiquid strike is rejected pre-order with the failing metric named | `backend/app/fno/liquidity.py` | `backend/tests/unit/test_options.py::test_liquidity_named_failures` | P1 | OC-001 | [~] |
| OPT-009 | Option order pricing uses limit orders by default with a configurable slippage cap; market orders on options require an explicit override | A market order on an option without the override is rejected | `backend/app/execution/pricing.py` | `test_option_order_pricing_policy` | P1 | EXEC-002 | [ ] |
| OPT-010 | Assignment and exercise awareness for held short options near expiry, with a mandatory-action alert | Short ITM option on expiry day triggers a critical alert and an exit intent | `backend/app/fno/assignment.py` | `test_assignment_risk_alert` | P1 | EXCH-006 | [ ] |
| OPT-011 | Option P&L on lot multiples with correct treatment of premium paid vs received | Long and short option P&L both match hand-computed fixtures | `backend/app/portfolio/pnl_fno.py` | `test_option_pnl` | P1 | PNL-001 | [ ] |

### 2.20 Options Greeks (`GRK`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| GRK-001 | Black-Scholes pricing implementation for European options (Indian index/stock options are European-style) | Prices match published reference values to 1e-6 on a standard test matrix | `backend/app/fno/greeks/black_scholes.py` | `test_black_scholes_pricing` | P1 | — | [✓] |
| GRK-002 | Analytic Greeks: delta, gamma, theta, vega, rho for calls and puts | Each Greek matches reference values to 1e-6; put-call parity relationships verified | `backend/app/fno/greeks/analytic.py` | `test_greeks_reference_values` | P1 | GRK-001 | [✓] |
| GRK-003 | Implied volatility solver (bisection/Newton with bracketing) that converges or reports failure explicitly | Round-trip test: price → IV → price reproduces the input to tolerance; non-convergence returns `None` with a reason, never a silent 0 | `backend/app/fno/greeks/iv.py` | `test_iv_solver_roundtrip` | P1 | GRK-001 | [✓] |
| GRK-004 | Broker-supplied Greeks are preferred when available; locally computed Greeks are used as fallback and are labelled by source | Every Greek value carries `source=BROKER|COMPUTED`; mismatch beyond a tolerance raises a data-quality event | `backend/app/fno/greeks/resolver.py` | `test_greeks_source_labelling` | P1 | GRW-020 | [✓] |
| GRK-005 | Risk-free rate and dividend assumptions are configurable and recorded with every computed Greek | Changing the rate changes results; the rate used is stored in the audit record | `backend/app/fno/greeks/params.py` | `test_greeks_assumption_recording` | P2 | GRK-002 | [✓] |
| GRK-006 | Portfolio-level Greek aggregation across all option positions (net delta, gamma, theta, vega) with lot and sign handling | Aggregate for a mixed long/short book matches hand-computed totals | `backend/app/fno/greeks/portfolio.py` | `test_portfolio_greeks_aggregation` | P1 | GRK-002 | [✓] |
| GRK-007 | Time-to-expiry computed in trading-calendar terms with an explicit convention documented and tested | T for an expiry two sessions away matches the documented convention exactly | `backend/app/fno/greeks/time.py` | `test_time_to_expiry_convention` | P1 | EXCH-001 | [✓] |
| GRK-008 | Greeks refresh cadence during the session, with staleness marking | Greeks older than the threshold are marked stale and cannot justify a new entry | `backend/app/fno/greeks/resolver.py`; `backend/app/trading/options.py`; `backend/app/execution/freshness.py` (provider evidence collected by reference worker and source-revalidated through long-option PAPER OMS) | `test_stale_greeks_block_entry`; `test_option_evidence.py`; `test_option_execution.py` | P1 | MD-008 | [✓] |

### 2.21 Option-chain analysis (`OC`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| OC-001 | Option-chain model: full strike ladder with CE/PE LTP, OI, OI change, volume, IV and Greeks, plus the underlying spot and timestamp | Model built from the Groww chain fixture with every field populated or explicitly `None` | `backend/app/fno/chain/model.py` | `backend/tests/unit/test_option_chain.py::test_option_chain_model` | P1 | GRW-019 | [✓] |
| OC-002 | Max-pain computation across the strike ladder | Max pain on a fixture matches the hand-computed strike | `backend/app/fno/chain/maxpain.py` | `backend/tests/unit/test_option_chain.py::test_max_pain` | P2 | OC-001 | [✓] |
| OC-003 | Put/call ratio by OI and by volume, at chain level and per strike band | PCR values match hand-computed fixtures | `backend/app/fno/chain/pcr.py` | `backend/tests/unit/test_option_chain.py::test_pcr` | P1 | OC-001 | [✓] |
| OC-004 | OI build-up classification per strike using the price/OI matrix (long build-up, short build-up, long unwinding, short covering) | Classification matches the standard matrix on all four fixture cases | `backend/app/fno/chain/buildup.py` | `backend/tests/unit/test_option_chain.py::test_oi_buildup_matrix` | P1 | OC-001 | [✓] |
| OC-005 | Support/resistance inference from OI concentration (highest CE OI as resistance, highest PE OI as support) with a documented caveat that this is heuristic | Inferred levels match the fixture; output carries a `heuristic=True` marker | `backend/app/fno/chain/levels.py` | `backend/tests/unit/test_option_chain.py::test_oi_levels` | P2 | OC-001 | [✓] |
| OC-006 | IV skew and term-structure summary across strikes and expiries | Skew computed and exposed; missing IVs handled without distorting the curve | `backend/app/fno/chain/skew.py` | `backend/tests/unit/test_option_chain.py::test_iv_skew` | P2 | OC-001 | [✓] |
| OC-007 | Chain snapshot persistence at a configurable cadence for post-trade analysis and backtesting | Snapshots stored and retrievable by (underlying, expiry, timestamp) | `backend/app/fno/chain/snapshots.py`; `backend/app/trading/options.py` | `backend/tests/integration/test_chain_snapshots.py::test_chain_snapshot_persistence`; `test_option_evidence.py` | P2 | DB-005 | [✓] |
| OC-008 | Chain-derived analytics are exposed to strategies and to the AI as structured context, never as free text | Context object is typed and schema-validated before entering any prompt | `backend/app/strategies/context.py` | `backend/tests/unit/test_option_chain.py::test_chain_context_typed` | P1 | AIR-004 | [✓] |

### 2.22 F&O risk (`FNOR`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| FNOR-001 | Derivatives exposure is measured in notional terms (lot size × price × lots), not premium, for exposure limits | A single NIFTY lot's notional is computed correctly and counted against exposure limits | `backend/app/risk/exposure_fno.py` | `test_fno_notional_exposure` | P0 | GRK-006 | [ ] |
| FNOR-002 | Portfolio Greek limits: maximum absolute net delta, gamma, vega and minimum theta, configurable and enforced | A proposal breaching any Greek limit is rejected with the breached Greek named | `backend/app/risk/rules/greeks.py` | `test_portfolio_greek_limits` | P1 | GRK-006 | [ ] |
| FNOR-003 | Defined-risk requirement: every option structure must have a computable maximum loss before approval | A structure whose max loss cannot be computed (or is unbounded) is rejected unless the naked-short override is enabled | `backend/app/risk/rules/options.py` | `test_defined_risk_requirement` | P0 | OPT-004 | [ ] |
| FNOR-004 | Margin-utilisation limit: total margin used may not exceed a configured fraction of available margin | A proposal pushing utilisation past the limit is rejected; utilisation uses broker margin when available | `backend/app/risk/rules/margin.py` | `test_margin_utilisation_limit` | P0 | GRW-015 | [ ] |
| FNOR-005 | Expiry-day risk rules: reduced position sizing and blocked new entries after the configured cutoff | On expiry day the sizing multiplier and entry block are both applied | `backend/app/risk/rules/expiry.py` | `test_expiry_day_risk_rules` | P1 | EXCH-006 | [ ] |
| FNOR-006 | Gap-risk accounting for overnight F&O positions: worst-case gap scenario evaluated against the daily loss limit before carrying a position | A position whose modelled gap loss exceeds the remaining daily budget is refused or flagged for reduction | `backend/app/risk/rules/gap.py` | `test_overnight_gap_risk` | P1 | RISK-005 | [ ] |
| FNOR-007 | Correlated-exposure limit preventing multiple simultaneous positions that are effectively the same directional bet on the index | Three correlated index-direction positions trip the correlation limit | `backend/app/risk/rules/correlation.py` | `test_correlated_exposure_limit` | P1 | RISK-007 | [ ] |
| FNOR-008 | Stress test: portfolio revalued under configured underlying-move and IV-shift scenarios, with the worst case compared to limits | Stress output produces a P&L per scenario; breaching the configured worst-case threshold blocks new entries | `backend/app/risk/stress.py` | `test_portfolio_stress_scenarios` | P2 | GRK-006 | [ ] |

### 2.23 Strategy engine (`STRAT`)

Per contract §15 every strategy must carry a definition, entry conditions, exit conditions, risk rules, a backtest, out-of-sample validation, paper-trading results and performance metrics. Strategies lacking evidence are marked **STRATEGY NOT APPROVED FOR LIVE TRADING**.

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| STRAT-001 | `Strategy` abstract base class with a declared contract: id, version, timeframe, universe filter, permitted regimes, required inputs, product type, entry, exit, and risk parameters | Any subclass missing a required declaration fails to register | `backend/app/strategies/base.py` | `backend/tests/unit/test_strategies.py::test_strategy_contract_enforced` | P0 | ARCH-010 | [✓] |
| STRAT-002 | Strategy registry with explicit enable/disable per mode; a strategy can be enabled for PAPER but disabled for LIVE | Registry state is persisted and surfaced in the API; a LIVE-disabled strategy cannot produce a live order | `backend/app/strategies/registry.py` | `backend/tests/integration/test_strategies.py::test_strategy_registry_mode_gating` | P0 | STRAT-001 | [~] |
| STRAT-003 | Signal model: typed `Signal` with direction, instrument, entry reference price, stop loss, target(s), timeframe, confidence and the exact conditions that fired | A signal without a stop loss is invalid and cannot be constructed | `backend/app/strategies/signal.py` | `backend/tests/unit/test_strategies.py::test_signal_requires_stop` | P0 | STRAT-001 | [✓] |
| STRAT-004 | Strategy context builder assembling only the declared inputs (candles, indicators, regime, chain, news, fundamentals) for each strategy | A strategy that did not declare news receives no news field (attribute access raises) | `backend/app/strategies/context.py` (declared inputs plus immutable decision-time metadata; no wall-clock drift in reference signals) | `backend/tests/unit/test_strategies.py::test_context_only_declared_inputs`; `test_context_decision_time_is_explicit_and_read_only`; `backend/tests/integration/test_advancing_clock.py` | P1 | TA-011 | [✓] |
| STRAT-005 | At least three fully specified reference strategies covering distinct regimes — e.g. (a) index-futures trend-following, (b) equity intraday mean-reversion, (c) defined-risk option spread — each with complete §15 documentation | Each strategy has a spec document, entry/exit code, risk rules and a backtest report | `backend/app/strategies/reference.py`, `docs/strategies/CLOSED_CANDLE_BREAKOUT.md`, `docs/strategies/LONG_OPTION_BREAKOUT/SPEC.md` (equity and option momentum PAPER lifecycles; distinct-regime families and option backtest reports pending) | `backend/tests/integration/test_reference_lifecycle.py`; `test_option_evidence.py`; `test_option_browser.py` | P1 | STRAT-001 | [~] |
| STRAT-006 | Exit logic is a first-class part of every strategy: stop loss, target, trailing rule, time-based exit and invalidation-condition exit | A strategy missing any of the five exit paths fails validation | `backend/app/strategies/exits.py` | `backend/tests/unit/test_exits_arbitration.py::test_exit_completeness` | P0 | STRAT-003 | [✓] |
| STRAT-007 | Deterministic evaluation: the same inputs always produce the same signal (no wall-clock or random dependence outside the injected clock and seeded RNG) | Running a strategy twice over identical fixtures yields byte-identical signals | `backend/app/strategies/base.py` | `backend/tests/integration/test_strategies.py::test_strategy_determinism` | P0 | ARCH-012 | [~] |
| STRAT-008 | Signal conflict resolution when multiple strategies target the same instrument in opposite directions | Conflicting signals resolve by a documented, deterministic policy; both the winner and the suppressed signal are logged | `backend/app/strategies/arbitration.py` | `backend/tests/unit/test_exits_arbitration.py::test_signal_conflict_resolution` | P1 | STRAT-003 | [✓] |
| STRAT-009 | Each strategy declares whether it requires the LLM; if the LLM is unavailable, LLM-requiring strategies enter `NO TRADE` while others continue deterministically | With the LLM disabled, the LLM-dependent strategy emits no signal with reason `LLM_UNAVAILABLE`; the deterministic strategy still emits | `backend/app/strategies/base.py` | `backend/tests/integration/test_strategies.py::test_llm_dependency_degradation` | P0 | LLM-009 | [✓] |
| STRAT-010 | Strategy approval gate: a strategy may be enabled for LIVE only when its backtest, out-of-sample and paper-trading evidence meet configured minimum thresholds | Attempting to enable an unvalidated strategy for LIVE returns `STRATEGY NOT APPROVED FOR LIVE TRADING` | `backend/app/strategies/approval.py`, `evidence.py`, `review.py`, `registry.py` (audited registration/report linkage and PAPER eligibility/API/UI; mixed hashes, future reports and insufficient evidence blocked; actual approval/arming and external verification pending, LIVE remains hard-blocked) | `backend/tests/unit/test_oos_approval.py`; `tests/integration/test_walkforward_reports.py`; `test_paper_evidence.py`; `test_strategies.py`; real browser review | P0 | WF-004, PAPER-006 | [~] |
| STRAT-011 | Strategy parameters are versioned; changing a parameter creates a new version and invalidates prior live approval | Editing a parameter of a LIVE-approved strategy reverts it to unapproved until re-validated | `backend/app/strategies/versioning.py` | `test_parameter_change_revokes_approval` | P1 | STRAT-010 | [ ] |
| STRAT-012 | Per-strategy performance tracking (live and paper) with rolling metrics and automatic disablement on breach of a degradation threshold | A strategy exceeding its configured rolling drawdown is auto-disabled and an alert is raised | `backend/app/strategies/monitor.py`, `portfolio/metrics.py`, worker/preflight; authenticated strategy APIs and `frontend/src/StrategyMonitorPanel.tsx`; `docs/PAPER_STRATEGY_MONITOR.md` (PAPER closed-net window, atomic latch/audit/outbox and reviewed reset; broader-mode integration pending) | `backend/tests/integration/test_strategy_monitor.py`; `test_paper_browser.py::test_real_worker_degradation_reaches_browser`; `tests/unit/test_performance_metrics.py`; `frontend/src/StrategyMonitorPanel.test.tsx` | P1 | LEARN-002 | [~] |
| STRAT-013 | No strategy, indicator or model is added without a stated hypothesis and validation plan (contract §15) | Every strategy directory contains `SPEC.md` with hypothesis, conditions, risk rules and validation results; a test asserts the file exists and has all sections | `docs/strategies/`, `backend/tests/unit/test_strategy_docs.py` | `test_strategy_spec_completeness` | P1 | STRAT-005 | [ ] |

### 2.24 LLM provider layer (`LLM`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| LLM-001 | `LLMProvider` abstract interface with `ClaudeProvider`, a stub `OpenAIProvider` slot, and `DeterministicFallbackProvider` | Interface parity test passes for all registered providers | `backend/app/llm/base.py; backend/app/llm/factory.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P0 | ARCH-009 | [~] |
| LLM-002 | Provider selected by `LLM_PROVIDER` env var; switching providers requires no engine code change | Setting the var to each supported value yields the corresponding provider from the factory | `backend/app/llm/factory.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P0 | LLM-001 | [~] |
| LLM-003 | Claude provider using the Anthropic Messages API with the configured model id, timeouts and max tokens | A recorded-fixture test asserts request shape and response parsing; model id is configurable and logged | `backend/app/llm/claude.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P0 | LLM-001 | [~] |
| LLM-004 | **Structured output only:** every LLM call declares a JSON schema and the response is validated against it before use | A response failing schema validation is never passed downstream | `backend/app/llm/schema.py; backend/app/llm/service.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P0 | LLM-003 | [~] |
| LLM-005 | Invalid-JSON recovery: bounded repair attempts (extraction, one re-ask with the validation error), then failure | After the configured attempts the call fails cleanly with `LLMSchemaError`; no partial object is used | `backend/app/llm/schema.py; backend/app/llm/service.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P0 | LLM-004 | [~] |
| LLM-006 | Prompt construction separates system instructions from untrusted data, with external content wrapped in a delimited data block marked as non-instructional | Prompt builder test asserts the envelope; injected instructions in the data block do not alter the schema-constrained output | `backend/app/llm/prompts.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P0 | SEC-010 | [~] |
| LLM-007 | Prompt versioning: every prompt template has a semantic version stored with each call and each resulting proposal | Changing a template requires a version bump (test fails on unchanged version with changed content hash) | `backend/app/llm/prompts.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P1 | DB-016 | [~] |
| LLM-008 | Full LLM call logging: provider, model, prompt version, input/output token counts, cost, latency, outcome, redacted prompt hash | Every call produces exactly one `llm_calls` row; cost roll-up matches | `backend/app/llm/telemetry.py; backend/app/llm/service.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P1 | DB-016 | [~] |
| LLM-009 | Timeout, retry and rate-limit handling with a circuit breaker that opens after repeated failures | Simulated 429/timeout storms open the breaker; while open, calls fail fast and the fallback path engages | `backend/app/llm/claude.py; backend/app/llm/budget.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P0 | LLM-003 | [~] |
| LLM-010 | Cost and volume budget: configurable daily token/cost cap that disables LLM calls (not trading) when exhausted | Exceeding the cap switches to the fallback provider and raises an alert | `backend/app/llm/budget.py; backend/app/llm/service.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P1 | LLM-008 | [~] |
| LLM-011 | `DeterministicFallbackProvider` returns schema-valid, rule-derived output so the whole system runs with no Anthropic key | Full end-to-end test passes with `LLM_PROVIDER=fallback` and no API key present | `backend/app/llm/fallback.py; backend/app/trading/reference.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P0 | LLM-004 | [~] |
| LLM-012 | LLM responses are stored verbatim (redacted where needed) for audit and later review | Stored response can be replayed against the validator in a test | `backend/app/llm/telemetry.py; backend/app/llm/service.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P1 | AUDIT-001 | [~] |
| LLM-013 | Temperature/determinism policy: decision-path calls use the lowest-variance settings supported and record the settings used | Settings recorded per call and asserted in tests | `backend/app/llm/claude.py; backend/app/llm/budget.py` | `backend/tests/unit/test_llm.py; backend/tests/integration/test_llm_service.py` | P2 | LLM-003 | [~] |

### 2.25 AI research agents (`AIR`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| AIR-001 | Agent framework: each agent declares its inputs, output schema, prompt version and failure policy | An agent missing any declaration cannot be registered | `backend/app/agents/tasks.py; backend/app/llm/service.py` | `backend/tests/unit/test_agent_tasks.py; backend/tests/integration/test_llm_service.py` | P1 | LLM-004 | [~] |
| AIR-002 | News-analysis agent producing structured event interpretation grounded in stored source text | Output schema-valid; claims without source anchoring are dropped (NEWS-009) | `backend/app/news/interpret.py` registered NEWS_AGENT task; `backend/app/news/production.py`, `production_controls.py` and existing runtime; opt-in source-bound advisory extraction with audited bounded recovery, semantic completeness/external validation pending | `backend/tests/integration/test_news_interpretation.py; backend/tests/integration/test_news_production.py; backend/tests/integration/test_news_production_controls.py` | P2 | NEWS-007 | [~] |
| AIR-003 | Technical-context agent that *explains* quantitative signals but never invents indicator values | Agent input contains computed indicator values; output referencing a value not in its input is rejected by a numeric-grounding check | `backend/app/agents/technical_agent.py` | `test_technical_agent_grounding` | P1 | TA-011 | [ ] |
| AIR-004 | Research-synthesis agent combining technical, fundamental, regime, chain and news context into a trade thesis | Output contains thesis, evidence list with source ids, invalidation conditions and confidence; schema-validated | `backend/app/agents/research_agent.py` | `test_research_agent_schema` | P1 | AIR-002, AIR-003 | [ ] |
| AIR-005 | Scenario-analysis agent producing bull/base/bear outcomes with rough probabilities for a proposed trade | Probabilities sum to 1 within tolerance; output schema-validated | `backend/app/agents/scenario_agent.py` | `test_scenario_agent` | P2 | AIR-004 | [ ] |
| AIR-006 | Conflicting-information agent that surfaces contradictions between sources rather than resolving them silently | Given contradictory fixtures the agent flags the conflict and lowers confidence | `backend/app/agents/conflict_agent.py` | `test_conflict_agent` | P2 | NEWS-008 | [ ] |
| AIR-007 | Agents are stateless per call; all context is passed explicitly, making every call reproducible from stored inputs | Replaying a stored input set reproduces an equivalent call (same prompt hash) | `backend/app/agents/tasks.py; backend/app/llm/budget.py` | `backend/tests/unit/test_agent_tasks.py; backend/tests/integration/test_proposal_agent.py` | P1 | AUDIT-001 | [~] |
| AIR-008 | Agent numeric-grounding validator: any number in agent output that is meant to be market data must match the provided input within tolerance | A fabricated price in agent output is rejected with `UNGROUNDED_NUMBER` | `backend/app/agents/tasks.py; backend/app/llm/service.py` | `backend/tests/unit/test_agent_tasks.py; backend/tests/integration/test_llm_service.py` | P0 | AIR-003 | [~] |
| AIR-009 | Agent orchestration with per-agent timeouts, parallel execution where independent, and partial-result policy | One failing agent does not abort the cycle; its absence is recorded and confidence is reduced | `backend/app/agents/orchestrator.py` | `test_agent_orchestration_partial` | P1 | LLM-009 | [ ] |
| AIR-010 | Agents have no write access to anything: no broker, no DB writes, no filesystem, no network beyond the LLM provider | Import-graph and capability test asserts the restriction (SEC-011) | `backend/app/agents/` | `test_agents_read_only` | P0 | SEC-011 | [ ] |

### 2.26 AI decision engine (`AID`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| AID-001 | `TradeProposal` schema exactly as specified by the owner: instrument, direction, strategy, entry, stop_loss, target, quantity, thesis, evidence[], invalidation_conditions[], confidence | Pydantic model matches the specified field set; extra fields are rejected | `backend/app/agents/proposal.py` | `backend/tests/unit/test_proposal.py::test_proposal_schema` | P0 | LLM-004 | [✓] |
| AID-002 | Deterministic proposal validation before the risk engine: instrument exists and is tradable, direction valid, prices positive and tick-aligned, stop on the correct side of entry, target consistent with direction, quantity a positive lot multiple, confidence in [0,1] | Each violation has a dedicated test and produces a specific rejection code | `backend/app/agents/validation.py` | `backend/tests/integration/test_proposal.py::test_proposal_validation_rules` | P0 | AID-001 | [✓] |
| AID-003 | Sanity bounds: entry must be within a configured band of the live market price; stop distance must be within configured min/max ATR multiples | A proposal with an entry 20% away from market is rejected as `UNREALISTIC_ENTRY` | `backend/app/agents/validation.py` | `backend/tests/integration/test_proposal.py::test_proposal_sanity_bounds` | P0 | AID-002 | [✓] |
| AID-004 | **Quantity from the LLM is advisory only** — final quantity is always recomputed by the deterministic position sizer | Test asserts the executed quantity equals the sizer output even when the proposal requests more | `backend/app/agents/pipeline.py` | `backend/tests/integration/test_pipeline.py::test_quant_path_parity` | P0 | SIZE-001 | [~] |
| AID-005 | Every proposal must cite at least one verifiable evidence item with a source id resolvable in the database | A proposal with empty or unresolvable evidence is rejected | `backend/app/agents/validation.py` | `backend/tests/integration/test_proposal.py::test_evidence_resolvable` | P1 | NEWS-005 | [✓] |
| AID-006 | Confidence thresholding: proposals below a configured confidence floor are dropped before the risk engine, with the drop recorded | Sub-threshold proposal never reaches risk; drop is auditable | `backend/app/agents/pipeline.py` | `backend/tests/integration/test_pipeline.py::test_confidence_floor_and_negative_decision` | P1 | AID-001 | [✓] |
| AID-007 | The decision engine cannot emit an order directly; its only output is a validated proposal handed to the risk engine | Import-graph test asserts the decision engine has no path to the execution layer | `backend/app/agents/pipeline.py` | `backend/tests/unit/test_proposal.py::test_decision_engine_no_execution_path` | P0 | ARCH-010 | [✓] |
| AID-008 | A strategy signal may exist without an LLM proposal (pure quantitative path), and both paths converge on the same validation + risk pipeline | A deterministic signal traverses validation and risk identically in a test | `backend/app/agents/pipeline.py` | `backend/tests/integration/test_pipeline.py::test_quant_path_parity` | P0 | STRAT-003 | [✓] |
| AID-009 | The engine records, for every cycle, both the proposals produced and the instruments considered and rejected pre-proposal, with reasons | The audit answers "why did you NOT take this trade" for a considered-but-skipped instrument (contract §13) | `backend/app/agents/pipeline.py` | `backend/tests/integration/test_pipeline.py::test_confidence_floor_and_negative_decision` | P0 | AUDIT-002 | [✓] |
| AID-010 | The LLM is structurally prohibited from altering risk limits, sizing rules, mode, kill-switch state or account configuration | Fuzz test feeds proposals containing fields like `max_daily_loss`/`override_risk`; all are rejected as unknown fields and logged as tamper attempts | `backend/app/agents/validation.py` | `backend/tests/unit/test_proposal.py::test_llm_cannot_alter_controls` | P0 | RISK-016 | [✓] |

### 2.27 Deterministic risk engine (`RISK`)

The risk engine is pure, deterministic, LLM-free and mandatory. Every order intent passes through it. It is the only component permitted to approve a trade.

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| RISK-001 | Risk engine entry point `evaluate(proposal, portfolio_state, market_state, config) -> RiskDecision` with no I/O and no randomness | Function is pure: same inputs produce the same decision; no network/DB access inside (asserted by a no-I/O test harness) | `backend/app/risk/engine.py` | `backend/tests/unit/test_risk.py::test_risk_engine_purity` | P0 | DB-017 | [✓] |
| RISK-002 | Every rule is a separate, individually testable unit returning pass/fail plus the numeric inputs it used | Rule registry lists all rules; each has its own unit test | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_rule_registry_coverage` | P0 | RISK-001 | [✓] |
| RISK-003 | **Per-trade risk limit:** risk (entry−stop) × quantity may not exceed a configured fraction of capital | A proposal breaching it is rejected with `PER_TRADE_RISK_EXCEEDED` and the computed values | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_per_trade_risk_limit` | P0 | RISK-002 | [✓] |
| RISK-004 | **Maximum open exposure limit** across all positions, measured in notional terms including derivatives | Adding a position beyond the limit is rejected with the computed exposure | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_max_exposure_limit` | P0 | FNOR-001 | [✓] |
| RISK-005 | **Maximum daily loss limit:** when realised+unrealised day loss reaches the limit, all new entries are blocked for the session | Crossing the limit blocks entries within one cycle and raises a critical alert; exits remain allowed | `backend/app/risk/safety.py`, `backend/app/agents/pipeline.py` | `backend/tests/integration/test_risk_safety.py`, `backend/tests/integration/test_notification_runtime.py` | P0 | INTRA-007 | [~] |
| RISK-006 | **Maximum drawdown limit** measured from peak equity, blocking new entries and requiring manual re-arming | Simulated drawdown past the threshold disarms the system and requires explicit re-arm | `backend/app/risk/safety.py`, `backend/app/agents/pipeline.py` | `backend/tests/integration/test_risk_safety.py`, `backend/tests/integration/test_notification_runtime.py` | P0 | PNL-004 | [~] |
| RISK-007 | Concentration limits: per-instrument, per-sector and per-underlying caps | Each cap has its own rejection code and test | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_concentration_limits` | P0 | EQ-004 | [✓] |
| RISK-008 | Maximum simultaneous open positions, globally and per strategy | The (N+1)th concurrent position is rejected | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_max_open_positions` | P1 | RISK-002 | [✓] |
| RISK-009 | **Mandatory stop loss:** no entry is approved without a valid stop loss on the correct side of entry, within configured distance bounds | A proposal with no stop, or a stop on the wrong side, is rejected | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_mandatory_stop_loss` | P0 | STRAT-003 | [✓] |
| RISK-010 | Minimum reward:risk ratio enforced using entry, stop and first target | A trade below the configured R:R is rejected with the computed ratio | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_min_reward_risk` | P1 | RISK-009 | [✓] |
| RISK-011 | Margin sufficiency check against broker-reported available margin with a configured buffer | A proposal exceeding available margin minus buffer is rejected | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_margin_sufficiency` | P0 | GRW-015 | [✓] |
| RISK-012 | Liquidity and slippage check: expected slippage from the depth book may not exceed a configured fraction of the trade's risk | An illiquid instrument is rejected with the estimated slippage | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_slippage_limit` | P1 | MD-002 | [✓] |
| RISK-013 | Data-freshness precondition: the engine rejects any proposal built on stale market data, stale Greeks or a stale portfolio snapshot | Each staleness source has its own rejection test | `backend/app/risk/rules.py` | `backend/tests/unit/test_risk.py::test_risk_rejects_stale_inputs` | P0 | MD-008 | [✓] |
| RISK-014 | Instrument-level blocks: ban list, news halt, event blackout and manual blocklist all prevent new entries | Each block source is independently testable and named in the rejection | `backend/app/risk/rules.py`, `news_halts.py`, `instrument_blocks.py`, `event_controls.py`, `backend/app/fno/restrictions.py`: server-resolved PAPER news/manual/calendar and owner-admitted daily NSE ban vetoes; current catalog restrictions; authenticated review and Risk UI. F&O positive OMS lifecycle, scheduled/exchange-wide source maintenance and PostgreSQL cross-process verification remain pending | `backend/tests/unit/test_risk.py::test_instrument_blocks`; `backend/tests/integration/test_news_halts.py`, `test_news_halt_recovery.py`, `test_instrument_blocks.py`, `test_event_controls.py`, `test_event_controls_browser.py`, `test_fno_bans.py`: actual PAPER/API/browser checks, restart, dated scope, preserved exits, rollback and late-dispatch veto; all-source enforcement not complete | P1 | EXCH-008 | [~] |
| RISK-015 | Every decision — approval or rejection — is persisted with all rules evaluated, their inputs and the binding reason | The stored decision reproduces the verdict when replayed through the engine | `backend/app/risk/audit.py` | `backend/tests/integration/test_risk.py::test_risk_decision_replay` | P0 | DB-011 | [✓] |
| RISK-016 | Risk limits are changeable only through an authenticated, audited configuration path — never by the LLM, a strategy or an agent | Any attempt to mutate limits from the agent/strategy layer raises; config changes create a versioned, attributed record | `backend/app/risk/config.py` | `backend/tests/unit/test_risk.py::test_invalid_provenance_identity_and_immutable_limits` | P0 | DB-017 | [~] |
| RISK-017 | Fail-closed behaviour: if the risk engine raises, is misconfigured, or its config cannot be loaded, trading is disabled rather than permitted | Injected exception results in no order and a critical alert | `backend/app/risk/safety.py`, `backend/app/agents/pipeline.py` | `backend/tests/integration/test_risk_safety.py`, `backend/tests/integration/test_notification_runtime.py` | P0 | RISK-001 | [~] |
| RISK-018 | Pre-execution re-check: risk is re-evaluated immediately before order submission using the latest state | A state change between proposal and submission that breaches a limit aborts the submission | `backend/app/execution/preflight.py` | `test_preexecution_recheck` | P0 | RISK-001 | [ ] |
| RISK-019 | Risk limits are expressed both as absolute currency amounts and as percentages of capital, with the stricter binding | Both forms configured; the stricter one binds in a test | `backend/app/risk/config.py` | `backend/tests/unit/test_risk.py::test_limit_forms_stricter_binds` | P1 | RISK-003 | [✓] |
| RISK-020 | Risk-state snapshot API exposing live utilisation of every limit for the dashboard | Utilisation values equal the engine's internal computation | `backend/app/risk/state.py`, `inspection.py`, `evidence.py`, `api/risk.py`; Risk UI (audited account metrics and receipt-bound historical rule inspection; complete current per-candidate/concentration/market-dependent metrics remain pending) | `test_risk_utilisation.py`, `test_risk_inspection.py`, `test_risk_evidence_postgres.py`, `test_risk_utilisation_browser.py`, risk-panel frontend tests | P1 | FE-011 | [~] |

### 2.28 Position sizing (`SIZE`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| SIZE-001 | Risk-based position sizing: quantity = floor(risk_budget / (entry − stop)), adjusted to lot size, never exceeding any risk limit | Hand-computed fixtures match exactly, including rounding direction | `backend/app/sizing/risk_based.py`: stop risk floored by supplied defined maximum loss, plus costs; shared pipeline passes the same bound as independent risk | `backend/tests/unit/test_sizing.py::test_risk_based_sizing`, `test_defined_loss_basis_sizes_against_full_budget_not_only_stop_distance`; `backend/tests/integration/test_pipeline.py::test_shared_sizing_and_risk_use_same_defined_loss_basis` | P0 | RISK-003 | [✓] |
| SIZE-002 | Lot-size and tick-size adjustment for F&O: quantity is always an integer multiple of lot size; rounding is always downward | A budget allowing 1.9 lots yields 1 lot | `backend/app/sizing/lots.py` | `backend/tests/unit/test_sizing.py::test_lot_rounding_down` | P0 | DB-004 | [✓] |
| SIZE-003 | Zero-size outcome is a valid, explicit result meaning NO TRADE, never silently upsized to the minimum | A budget below one lot returns quantity 0 with reason `BUDGET_BELOW_MIN_LOT` and no order is created | `backend/app/sizing/risk_based.py` | `backend/tests/unit/test_sizing.py::test_zero_size_no_trade` | P0 | SIZE-001 | [✓] |
| SIZE-004 | Margin-constrained sizing: quantity is capped by available margin including the configured buffer | A margin-limited case reduces quantity to the affordable amount | `backend/app/sizing/risk_based.py` | `backend/tests/unit/test_sizing.py::test_margin_constrained_sizing` | P0 | GRW-015 | [✓] |
| SIZE-005 | Volatility-adjusted sizing option (ATR-scaled risk budget) selectable per strategy | With the option enabled, a higher-ATR instrument receives proportionally smaller size | `backend/app/sizing/risk_based.py` | `backend/tests/unit/test_sizing.py::test_volatility_adjusted_sizing` | P1 | TA-004 | [✓] |
| SIZE-006 | Exposure-aware sizing: the sizer reduces quantity so that the resulting position does not breach concentration or exposure limits | Sizing near the exposure cap returns a reduced quantity rather than a rejected trade | `backend/app/sizing/risk_based.py` | `backend/tests/unit/test_sizing.py::test_exposure_aware_sizing` | P1 | RISK-004 | [✓] |
| SIZE-007 | Daily-budget-aware sizing: remaining daily loss budget caps the risk allocated to a new trade | With 20% of the daily budget left, the sizer allocates at most that amount of risk | `backend/app/sizing/risk_based.py` | `backend/tests/unit/test_sizing.py::test_daily_budget_sizing` | P0 | RISK-005 | [✓] |
| SIZE-008 | Sizing is deterministic and fully logged: inputs, formula version and the resulting quantity are recorded per proposal | Stored sizing record reproduces the quantity when replayed | `backend/app/sizing/audit.py`; `backend/app/agents/pipeline.py` (quantity-dependent fee reserve, defined-loss source snapshot, formula-version verification; unchanged legacy stop-only replay) | `backend/tests/integration/test_sizing.py::test_sizing_audit_replay`, `test_defined_loss_replay_survives_restart_and_cannot_claim_legacy_formula`; `backend/tests/integration/test_contract_production.py` | P1 | AUDIT-001 | [✓] |
| SIZE-009 | Optional Kelly-fraction sizing is available but capped at a configured fraction and disabled by default, with documented rationale | Enabling it applies the cap; default configuration uses risk-based sizing | `backend/app/sizing/risk_based.py` | `backend/tests/unit/test_sizing.py::test_kelly_capped_and_off_by_default` | P3 | SIZE-001 | [✓] |

### 2.29 Order execution (`EXEC`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| EXEC-001 | Execution service converting an approved proposal into broker orders through the active `BrokerProvider` | Approved proposal produces exactly one entry order in paper mode with matching parameters | `backend/app/execution/executor.py` | `test_execution_from_approval` | P0 | ARCH-006 | [~] |
| EXEC-002 | Order-type policy: limit orders by default with a configurable slippage cap; market orders permitted only where explicitly allowed by the strategy and instrument liquidity | A market order on a low-liquidity instrument is refused | `backend/app/execution/pricing.py` | `test_order_type_policy` | P1 | OPT-009 | [~] |
| EXEC-003 | Stop-loss placement: a protective stop order is placed (or a monitored synthetic stop is armed) immediately after entry confirmation, with failure escalated | If the stop cannot be placed, the position is exited or a critical alert is raised; never left unprotected silently | `backend/app/execution/protection.py` | `test_stop_placement_or_escalate` | P0 | EXEC-001 | [~] |
| EXEC-004 | **Idempotent submission:** every order carries a deterministic `order_reference_id` derived from the intent id; duplicate submission is detected locally and by `GA007` | Submitting the same intent twice yields one broker order; `GA007` is treated as "already exists" and resolved by status lookup | `backend/app/execution/idempotency.py` | `test_idempotent_order_submission` | P0 | GRW-007 | [~] |
| EXEC-005 | Timeout handling per contract §11: on timeout, reconcile by reference id before deciding; never resubmit blindly | Timeout test with a broker that actually accepted the order results in zero duplicate orders | `backend/app/execution/safe_retry.py` | `test_timeout_reconciliation` | P0 | GRW-006 | [~] |
| EXEC-006 | Partial-fill handling: remaining quantity is tracked, protective stops are sized to filled quantity, and a policy decides whether to chase, wait or cancel | A 40%-filled order results in a stop for 40% and a documented decision for the remainder | `backend/app/execution/partial_fills.py` | `test_partial_fill_handling` | P0 | DB-008 | [~] |
| EXEC-007 | Rejection handling: broker rejections are classified (margin, price band, instrument, risk, unknown) and never auto-retried without addressing the cause | Each class has a test; unknown rejections stop further attempts for that intent | `backend/app/execution/rejections.py` | `test_rejection_classification` | P0 | GRW-003 | [~] |
| EXEC-008 | Unknown-order-status handling: an order whose status cannot be determined puts the system into a `RECONCILE_REQUIRED` state that blocks new entries | Simulated unknown status blocks new entries until resolved | `backend/app/execution/unknown_state.py` | `test_unknown_status_blocks_trading` | P0 | REC-004 | [~] |
| EXEC-009 | Exit execution: stop-loss, target, trailing, time-based and invalidation exits all route through the same execution path with the same guarantees | Each exit type produces a correctly signed exit order in tests | `backend/app/execution/paper.py`, `backend/app/trading/exits.py` | `backend/tests/integration/test_reference_exits.py`, `backend/tests/integration/test_paper_execution.py` | P0 | STRAT-006 | [~] |
| EXEC-010 | Emergency exit path that bypasses strategy logic but never bypasses order-safety mechanics (idempotency, reconciliation) | Kill-switch flatten produces exit orders for all positions with idempotency preserved | `backend/app/emergency/flatten.py` | `test_emergency_flatten` | P0 | EMG-002 | [~] |
| EXEC-011 | Execution-quality tracking: intended price vs fill price slippage recorded per order and aggregated per strategy | Slippage statistics available per strategy and surfaced in reports | `backend/app/execution/quality.py` | `test_slippage_tracking` | P2 | DB-008 | [ ] |
| EXEC-012 | Order throttle respecting Groww's 10/s and 250/min order limits, with queuing and back-pressure | A burst of 30 orders is paced within limits; queue depth is observable | `backend/app/execution/throttle.py` | `test_order_throttle` | P0 | GRW-004 | [ ] |
| EXEC-013 | `SUPERVISED` mode gate: no order reaches the broker without a recorded human approval token bound to that specific proposal | An unapproved proposal in SUPERVISED mode never calls the broker (call counter asserts zero) | `backend/app/execution/supervision.py` | `test_supervised_gate` | P0 | SUP-002 | [ ] |
| EXEC-014 | Paper execution engine simulating fills with configurable slippage, latency, partial fills and rejection probability — clearly labelled as simulated | Paper fills are marked `simulated=True` throughout the system and can never be reported as broker fills | `backend/app/brokers/paper/engine.py`, `provider.py`, `liquidity.py`, `constraints.py`, `stops.py`; shared historical/OMS runner, simulation disclosures and durable stop activation; no exchange queue or live execution certification | `backend/tests/integration/test_paper_broker.py`; `test_paper_latency.py`; `test_paper_liquidity.py`; `test_paper_constraints.py`; `test_paper_stops.py`; `test_recorded_worker.py`; `test_historical_acceptance.py`; `test_paper_browser.py` | P0 | ARCH-008 | [✓] |

### 2.30 Order management (`OMS`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| OMS-001 | Order state machine with explicit legal transitions (`CREATED→SUBMITTED→OPEN/PENDING→PARTIAL→EXECUTED/CANCELLED/REJECTED/UNKNOWN`) | Illegal transitions raise; every transition is persisted with a timestamp and source | `backend/app/execution/oms/state.py` | `test_order_state_machine` | P0 | DB-007 | [~] |
| OMS-002 | Order registry as the single source of truth for in-flight orders, surviving restart | After a simulated crash, in-flight orders are recovered from the DB | `backend/app/execution/oms/registry.py` | `test_order_registry_recovery` | P0 | DB-007 | [~] |
| OMS-003 | Order updates consumed from the websocket feed and reconciled with polled status, with the newer authoritative source winning deterministically | Conflicting feed/poll updates resolve by a documented rule and never regress a terminal state | `backend/app/execution/oms/updates.py` | `test_order_update_merge` | P0 | GRW-021 | [ ] |
| OMS-004 | Order modification with the same safety guarantees as placement (validation, idempotency, reconciliation) | Modify on a filled order is refused locally | `backend/app/execution/replacement.py` | `test_owner_replace.py`, `test_real_browser_replaces_unfilled_entry` | P1 | GRW-008 | [~] |
| OMS-005 | Order cancellation including bulk cancel of all open orders | Bulk cancel cancels every open order and reports per-order outcomes | `backend/app/execution/oms/cancel.py` | `test_bulk_cancel` | P1 | GRW-009 | [~] |
| OMS-006 | Stale-order sweeper cancelling orders that remain unfilled beyond a configured age | A stale limit order is cancelled and the decision is journalled | `backend/app/execution/hygiene.py`, `journal/order_actions.py`, `trading/worker.py` (PAPER entries, atomic cancellation result/journal; generalized limit/live-order scope pending) | `test_order_hygiene.py`, `test_order_action_journal.py`, `test_paper_browser.py` (partial fill, failure, restart, seal rollback, no fictional P&L, API/UI) | P1 | OMS-001 | [~] |
| OMS-007 | End-of-day order hygiene: all open day orders are cancelled or reconciled before session close | After the EOD job, no untracked open orders remain | `backend/app/execution/hygiene.py`, `trading/worker.py`, `notifications/order_hygiene.py`; Monitoring summary exposes verified recorded actions/unresolved IDs/protection failures, not complete historical coverage or broader-mode verification | `test_order_hygiene.py`, `test_paper_worker.py`, `test_provider_session.py`, `test_order_action_journal.py`, `test_paper_browser.py` | P1 | EXCH-002 | [~] |
| OMS-008 | Every order links to its originating proposal, risk decision and strategy for full traceability | The lineage query returns a complete chain for any order | `backend/app/execution/oms/registry.py` | `test_order_lineage` | P1 | DB-013 | [~] |

### 2.31 Portfolio management (`PORT`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| PORT-001 | Position tracker maintaining net quantity, average price and open/closed state from fills, for CASH and FNO | Recomputing positions from the trade log reproduces stored state exactly | `backend/app/portfolio/positions.py` | `test_position_tracking` | P0 | DB-009 | [~] |
| PORT-002 | Average-price arithmetic correct for scale-ins, partial exits, reversals and short positions | Each case matches a hand-computed fixture | `backend/app/portfolio/fifo.py`; `docs/PAPER_FIFO.md`; `docs/PAPER_OPTIONS.md` (CASH and single-long-option PAPER OMS integrated; broader derivatives unsupported) | `test_fifo.py`; `test_paper_fifo.py`; `test_paper_broker.py`; `test_paper_option_account.py`; `test_option_evidence.py` | P0 | PORT-001 | [~] |
| PORT-003 | Mark-to-market valuation on every tick with unrealised P&L per position and portfolio total | MTM matches hand-computed values for long and short, CASH and FNO | `backend/app/portfolio/mtm.py` | `test_mark_to_market` | P0 | MD-001 | [~] |
| PORT-004 | Exposure computation: gross, net, per-instrument, per-sector and per-underlying, including derivative notional | Values match hand-computed fixtures and feed the risk engine | `backend/app/portfolio/exposure.py` | `test_exposure_computation` | P0 | FNOR-001 | [ ] |
| PORT-005 | Capital and equity tracking: starting capital, current equity, peak equity, and available deployable capital | Equity curve updates on every realised P&L change; peak is monotonic | `backend/app/portfolio/equity.py` | `test_equity_tracking` | P0 | PNL-001 | [~] |
| PORT-006 | Holdings (delivery) tracked separately from intraday positions, with correct product attribution | A CNC holding and an MIS position in the same instrument remain distinct | `backend/app/portfolio/holdings.py` | `test_holdings_vs_positions` | P1 | GRW-014 | [ ] |
| PORT-007 | Portfolio snapshot persisted at a configurable cadence and at session end for historical analysis | Snapshots retrievable by timestamp and used by reporting | `backend/app/portfolio/snapshots.py` | `test_portfolio_snapshots` | P2 | DB-009 | [~] |
| PORT-008 | Broker positions are the authority: local positions are reconciled against broker positions and discrepancies are flagged, never silently overwritten | A deliberate mismatch produces a `DISCREPANCY` record and blocks trading until resolved | `backend/app/portfolio/reconcile.py` | `test_position_reconciliation` | P0 | REC-003 | [~] |

### 2.32 P&L (`PNL`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| PNL-001 | Realised P&L computed per closed trade using FIFO matching, with lot multiples for F&O | FIFO matching over a scale-in/scale-out fixture matches hand computation | `backend/app/portfolio/pnl.py` | `test_realised_pnl_fifo` | P0 | DB-008 | [~] |
| PNL-002 | Unrealised P&L per open position, updated on every mark | Values match hand computation for long/short, CASH/FNO | `backend/app/portfolio/pnl.py` | `test_unrealised_pnl` | P0 | PORT-003 | [~] |
| PNL-003 | Cost model including brokerage, STT/CTT, exchange transaction charges, SEBI turnover fee, stamp duty and GST, configurable per segment and product | Net P&L for a fixture trade matches a hand-computed cost breakdown; each component is individually inspectable | `backend/app/portfolio/costs.py`; `docs/PAPER_COSTS.md`; `docs/PAPER_OPTIONS.md` (CASH/MIS and single-long-option PAPER OMS estimates integrated; other products and external billing verification pending) | `test_costs.py`; `test_paper_costs.py`; `test_paper_option_account.py`; `test_option_browser.py` | P0 | — | [~] |
| PNL-004 | Daily, weekly, monthly and since-inception P&L aggregates with drawdown series | Aggregates reconcile to the sum of underlying trades exactly | `backend/app/portfolio/aggregates.py` | `test_pnl_aggregates` | P1 | PNL-001 | [ ] |
| PNL-005 | Per-strategy and per-instrument P&L attribution | Attribution sums to total P&L | `backend/app/portfolio/attribution.py` | `test_pnl_attribution` | P1 | PNL-004 | [ ] |
| PNL-006 | Performance metrics: win rate, average win/loss, profit factor, expectancy, Sharpe, Sortino, max drawdown, recovery factor, average holding period | Each metric matches a reference implementation on a fixture equity/trade series | `backend/app/portfolio/metrics.py` (descriptive net metrics integrated; Sharpe/Sortino pending adequate return/benchmark inputs) | `backend/tests/unit/test_performance_metrics.py`; `test_historical_engine.py` | P1 | PNL-004 | [~] |
| PNL-007 | Paper and live P&L are stored and reported separately and can never be aggregated into a single figure | An attempt to aggregate across modes raises; the UI labels each figure with its mode | `backend/app/portfolio/pnl.py` | `test_mode_separated_pnl` | P0 | ARCH-008 | [~] |
| PNL-008 | P&L figures shown in the dashboard come from computed positions and trades, never from constants or client-side maths | Frontend test asserts values originate from the API payload | `frontend/src/pages/Dashboard.tsx` | `Dashboard.pnl.test.tsx` | P0 | BE-006 | [~] |

### 2.33 Trade journal (`JRN`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| JRN-001 | Journal entry created for every trade, linking proposal, risk decision, orders, fills, exits, costs and net P&L | The lineage query returns a complete, gap-free chain for every closed trade in a full e2e run | `backend/app/execution/paper.py`, `portfolio/journal_integrity.py`, `journal/service.py` (costed PAPER lineage verified; general cross-mode/correction workflow incomplete) | `test_reference_worker.py`; `test_paper_evidence.py` (audit rollback/exactly-once recovery); `test_journal_api.py`; real browser lineage | P1 | DB-013 | [~] |
| JRN-002 | Journal captures the market context at entry: regime, indicator snapshot, chain summary, news used and the AI thesis | Stored context is sufficient to reconstruct the rationale without external data | `backend/app/journal/context.py` | `test_journal_context_capture` | P1 | TA-011 | [~] |
| JRN-003 | Journal captures exit context: exit reason code, price, slippage, holding period and whether an invalidation condition fired | Each exit reason is represented as an enum, not free text | `backend/app/execution/paper.py` (actual PAPER exit enum, duration, slippage and invalidation flag; general cross-mode coverage pending) | `test_journal_api.py`; existing PAPER exit/browser tests | P1 | EXEC-009 | [~] |
| JRN-004 | Rejected proposals are journalled too, with the binding rejection reason, so the "why not" question is answerable | A rejected proposal appears in the journal view with its rule and inputs | `backend/app/journal/rejections.py`, `agents/pipeline.py`; authenticated journal API/view | `test_journal_api.py` (validation/sizing rejection, context, no fictional fills/P&L, audit rollback); `test_pipeline.py` (risk rejection) | P1 | AID-009 | [✓] |
| JRN-005 | Manual annotation: the owner can attach notes and tags to any journal entry | Notes persist and are returned by the API | `backend/app/journal/service.py`; authenticated annotation API; existing Journal view | `test_journal_api.py` (persistence, actor, rollback, tamper/deletion); real browser owner annotation | P2 | DB-013 | [✓] |
| JRN-006 | Journal export to CSV and JSON for offline analysis | Export contains every field and round-trips through a parser in a test | `backend/app/api/journal.py`; authenticated bounded CSV/JSON export; `docs/JOURNAL.md` JSON-cell CSV encoding | `test_journal_api.py` (every column, exact CSV/JSON roundtrip, limits); real browser downloaded artifact | P2 | JRN-001 | [✓] |
| JRN-007 | Journal entries are immutable except for annotations; corrections create a new versioned entry | An update attempt on a core field raises | `backend/app/db/models/journal.py`, `journal/corrections.py`, `alembic/versions/0010_journal_revisions.py`; authenticated contextual revisions, sealed originals; no economic rewrite permission | `test_journal_revisions.py` (ORM/bulk/SQLite SQL refusal, concurrent versions, rollback, tamper, evidence exclusion); real browser version/original inspection; PostgreSQL trigger execution unverified | P1 | DB-012 | [✓] |

### 2.34 Backtesting (`BT`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| BT-001 | Event-driven backtest engine replaying stored candles bar-by-bar through the *same* strategy, sizing and risk code used live | A strategy produces identical signals in backtest and in replayed paper mode for the same data | `backend/app/backtest/engine.py`, `launcher.py` (isolated bounded shared-worker execution; full sampled-session PAPER parity) | `backend/tests/integration/test_full_session_parity.py`; `test_historical_engine.py`; `test_historical_process.py` | P1 | HD-008 | [✓] |
| BT-002 | **No look-ahead:** at bar *t* the engine exposes only data up to and including *t*, and entries execute at *t+1* open (or the configured fill convention) | A deliberately cheating strategy (reading the next bar) fails with a look-ahead assertion | `backend/app/backtest/engine.py`; `backend/app/marketdata/replay.py`; `backend/app/strategies/engine.py` (bounded closed candles; configured quote-fill convention; next-bar access rejected/audited and research INCOMPLETE, not arbitrary Python sandboxing) | `backend/tests/integration/test_historical_lookahead.py`; `test_historical_acceptance.py`; `test_historical_engine.py`; `backend/tests/unit/test_replay_safety.py` | P0 | HD-008 | [✓] |
| BT-003 | Realistic fill model: slippage, spread, partial fills and rejection probability, all configurable and documented | Changing the slippage parameter changes results monotonically in the expected direction | `backend/app/brokers/paper/engine.py`; existing shared historical runner; `docs/HISTORICAL_FILL_MODEL.md` (actual delay, adverse tick rounding, whole lots, conserved depth and stop activation verified; no duplicate engine or profitability claim) | `backend/tests/integration/test_historical_acceptance.py`; `test_paper_broker.py`; `test_paper_latency.py`; `test_paper_liquidity.py`; `test_paper_constraints.py`; `test_paper_stops.py` | P1 | EXEC-014 | [✓] |
| BT-004 | Full transaction-cost model applied (PNL-003) so backtest P&L is net, never gross | Backtest net P&L for a fixture equals hand-computed net including all charges | `backend/app/backtest/engine.py`; `backend/app/portfolio/costs.py` (actual CASH/MIS costs integrated; broader product costs pending) | `backend/tests/integration/test_historical_acceptance.py`; `test_historical_engine.py` | P0 | PNL-003 | [~] |
| BT-005 | Risk engine active during backtests so historical results reflect the same constraints as live trading | A backtest with tight limits produces fewer trades than one with loose limits, and rejections are recorded | `backend/app/backtest/engine.py`; shared decision/risk pipeline (actual isolated runner, no risk bypass) | `backend/tests/integration/test_historical_acceptance.py` (tight concentration veto persists risk decision and produces no orders versus baseline trade) | P0 | RISK-001 | [✓] |
| BT-006 | Metrics output: total return, CAGR, Sharpe, Sortino, max drawdown, win rate, profit factor, expectancy, trade count, exposure time, average holding period | All metrics computed by the shared `metrics` module (no duplicate implementations) | `backend/app/backtest/report.py` (shared descriptive metrics integrated; annualized/exposure-duration metrics pending) | `backend/tests/integration/test_historical_engine.py`; `backend/tests/unit/test_performance_metrics.py` | P1 | PNL-006 | [~] |
| BT-007 | Equity curve, drawdown curve and trade list persisted per run and rendered in the dashboard | Stored run reproduces the same curve on reload | `backend/app/backtest/storage.py`, `report.py`, `publication.py`; `backend/app/api/backtests.py`; `frontend/src/HistoricalPanel.tsx` (audit-bound catalog publication and API/UI reload verified; simulated sampled evidence only) | `backend/tests/integration/test_historical_engine.py`; `test_historical_publication.py`; `test_historical_api.py`; `test_paper_browser.py`; `test_historical_process.py` | P2 | DB-019 | [✓] |
| BT-008 | Deterministic reproducibility: the same run configuration and seed reproduce identical results | Two runs produce byte-identical metrics and trade lists | `backend/app/backtest/reproducibility.py`; `engine.py`; historical API/dashboard (versioned economic fingerprints; run-local IDs excluded, original lineage retained) | `backend/tests/integration/test_historical_acceptance.py`; `test_historical_api.py`; `frontend/tests/historical-browser.mjs` | P1 | ARCH-013 | [✓] |
| BT-009 | Parameter sweep with explicit overfitting warnings: the number of combinations tested is recorded and reported alongside results | A sweep report shows combinations tested and flags results selected from a large search space | `backend/app/backtest/sweep.py` | `test_parameter_sweep_reporting` | P2 | BT-006 | [ ] |
| BT-010 | Survivorship-bias handling: the universe used for a historical period reflects instruments listed and eligible at that time, or the limitation is explicitly recorded in the report | Report includes a survivorship-bias statement with the actual universe construction method used | `backend/app/backtest/universe.py`, `bootstrap.py`, `walkforward.py`; historical API/dashboard/export (owner-declared static lists and selected/context roles; explicit bias limitation, legacy unavailability without sealed-history mutation; not externally certified eligibility) | `backend/tests/integration/test_historical_api.py`; `test_walkforward_reports.py`; `frontend/tests/historical-browser.mjs`; `HistoricalPanel.test.tsx` | P2 | EQ-001 | [✓] |
| BT-011 | Backtest results are clearly marked as simulated and can never be presented as live performance | Every backtest payload carries `simulated=True`; the UI labels it | `backend/app/api/backtests.py`, `historical_jobs.py`; `backend/app/backtest/child.py`, `launcher.py`, `publish.py`, `jobs.py`, `wf_integrity.py`; `frontend/src/HistoricalPanel.tsx` (discovery/results/trades/samples/export/jobs/plans/OOS review/CLI labeled; stored false labels refused, no LIVE evidence claim) | `backend/tests/integration/test_historical_api.py`; `test_historical_process.py`; `test_historical_jobs.py`; `test_historical_publication.py`; `test_walkforward_reports.py`; real historical browser download | P1 | ARCH-008 | [✓] |
| BT-012 | Option/futures backtesting supported using stored chain snapshots where available, with an explicit limitation notice where they are not | An F&O backtest without chain history fails loudly rather than silently approximating | `backend/app/backtest/fno.py`, `bootstrap.py`, `walkforward.py`; `docs/HISTORICAL_OPTIONS.md` (single-session NSE long-option replay, attributed OOS children and sealed API/dashboard/export; futures and multi-session position carry unsupported) | `backend/tests/integration/test_historical_options.py`; `test_option_walkforward.py`; `test_paper_browser.py` (costed lifecycle, future-exit exclusion, missing chain refusal, entry gates, OOS selection, reproducibility and real historical browser) | P2 | OC-007 | [~] |
| BT-013 | Backtest run configuration is versioned and stored with the strategy version and data window used | Changing the strategy version creates a distinct run record | `backend/app/backtest/bootstrap.py` (manifest and execution settings persisted; version-change acceptance pending) | `backend/tests/integration/test_historical_bootstrap.py`, `test_historical_engine.py` | P2 | STRAT-011 | [~] |
| BT-014 | **Documented limitation:** intraday backtests are limited by available intraday history (Groww: 3 months, plus locally archived data); the report states the exact data window and its length | Every intraday backtest report shows data coverage and warns when the window is below the configured minimum for statistical confidence | `backend/app/backtest/adequacy.py`, `bootstrap.py`, `wf_report.py`; typed detail/export API and `frontend/src/HistoricalPanel.tsx`; `docs/BACKTEST_DISCLOSURES.md` (captured minimum, exact UTC duration, bounded recorded publications, explicit coverage/confidence unverified; legacy metadata unavailable without rewriting sealed reports) | `test_historical_adequacy.py`; `test_historical_api.py`; `test_option_walkforward.py`; real equity/option historical and OOS browser exports | P1 | HD-010 | [✓] |

### 2.35 Walk-forward validation (`WF`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| WF-001 | Walk-forward framework splitting history into rolling in-sample/out-of-sample windows with configurable sizes and step | Window generation matches hand-computed splits on a fixture date range | `backend/app/backtest/windows.py`, `walkforward.py` (UTC half-open windows, explicit embargo, bounded owner configuration and actual isolated jobs) | `backend/tests/integration/test_walkforward.py`; `test_paper_browser.py` | P1 | BT-001 | [✓] |
| WF-002 | Parameters are optimised only on in-sample data and evaluated only on the following out-of-sample window | A test asserts out-of-sample data is never visible to the optimiser | `backend/app/backtest/wf_inputs.py`, `walkforward.py` (typed training-only scores, frozen risk-fraction candidates, audit before selected OOS execution; broader strategy-family optimisation not claimed) | `backend/tests/integration/test_walkforward.py`; `test_walkforward_recovery.py`; `frontend/tests/walkforward-browser.mjs` | P0 | WF-001 | [✓] |
| WF-003 | Aggregate out-of-sample performance report with per-window metrics and stability measures | Report shows per-window and aggregate metrics and parameter drift across windows | `backend/app/backtest/wf_report.py`, `wf_integrity.py`; typed historical API/dashboard (audit-bound OOS-only reset-account totals, parameter drift and descriptive stability; not continuous portfolio metrics) | `backend/tests/integration/test_walkforward_reports.py`; `frontend/tests/walkforward-browser.mjs` (actual two-window jobs, independently calculated costs/net totals, API lineage and tamper rejection) | P1 | WF-002 | [✓] |
| WF-004 | Validation thresholds configured (minimum out-of-sample trades, minimum expectancy, maximum drawdown, minimum window consistency) gate a strategy's LIVE approval | A strategy failing any threshold cannot be enabled for LIVE (STRAT-010) | `backend/app/strategies/approval.py`; frozen experiment policy and authenticated historical API/UI review (threshold failures integrated; LIVE approval/version evidence gate remains pending; drawdown explicitly within-window) | `backend/tests/unit/test_oos_approval.py`; `tests/integration/test_walkforward_reports.py`; `frontend/tests/walkforward-browser.mjs` | P0 | STRAT-010 | [~] |
| WF-005 | Degradation detection: out-of-sample performance materially worse than in-sample is flagged as likely overfitting | A fixture with strong IS and weak OOS is flagged | `backend/app/backtest/wf_report.py`, `windows.py` (explicit return-fraction drop threshold; absent policy UNAVAILABLE; diagnostic not statistical proof) | `backend/tests/integration/test_walkforward_reports.py`; `frontend/tests/walkforward-browser.mjs` (strong IS/losing OOS flag and improving OOS non-flag through real jobs/API/UI) | P1 | WF-003 | [✓] |

### 2.36 Paper trading (`PAPER`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| PAPER-001 | `PaperBrokerProvider` implementing the full `BrokerProvider` interface with a simulated order book and account | Interface parity test passes; every method behaves plausibly and deterministically | `backend/app/brokers/paper/provider.py` | `test_paper_provider_parity` | P0 | ARCH-004 | [✓] |
| PAPER-002 | Paper trading runs against **real live market data** so behaviour matches live conditions apart from execution | In PAPER mode the market-data provider is the live one (unless replay is explicitly selected) | `backend/app/brokers/factory.py` | `test_paper_uses_live_data` | P0 | MD-001 | [✓] |
| PAPER-003 | Simulated account with configurable starting capital, margin model and per-segment buying power | Margin is consumed and released correctly across a trade lifecycle | `backend/app/brokers/paper/account.py`; `docs/PAPER_OPTIONS.md` (typed long-option premium reservation; generic margin is not live verification) | `test_paper_account_margin`; `test_paper_option_account.py` | P0 | PAPER-001 | [✓] |
| PAPER-004 | Fill simulation using the live depth book where available, with configurable latency and slippage | A large order against a thin book fills partially and at worse prices, deterministically | `backend/app/brokers/paper/engine.py`, `provider.py`, `liquidity.py`, `constraints.py`, `stops.py` (existing provider interface, conserved depth, deterministic priority, lot/tick constraints and durable activation; external feed/live fills unverified) | `backend/tests/integration/test_paper_broker.py`; `test_paper_latency.py`; `test_paper_liquidity.py`; `test_paper_constraints.py`; `test_paper_stops.py`; `test_historical_acceptance.py`; `docs/HISTORICAL_FILL_MODEL.md` | P1 | MD-002 | [✓] |
| PAPER-005 | Paper state persists across restarts (positions, orders, equity) | After a restart, paper positions and equity are unchanged | `backend/app/brokers/paper/state.py` | `test_paper_state_persistence` | P1 | DB-018 | [✓] |
| PAPER-006 | Paper performance is tracked with the same metrics as live and is a precondition for LIVE strategy approval (minimum sessions and trades configured) | A strategy with insufficient paper history cannot be LIVE-approved | `backend/app/strategies/paper_policy.py`, `paper_evidence.py`, `review.py`; `trading/paper_coverage.py`; authenticated API/Strategies view (explicit policy, observed coverage, costed audited LIVE-origin eligibility, shared trade metrics; real time-based/external validation and actual LIVE approval remain pending) | `backend/tests/integration/test_paper_evidence.py`; `frontend/tests/paper-browser.mjs` (restart gaps, stale/future/synthetic inputs, altered evidence, policy replacement, net P&L and review) | P0 | STRAT-010 | [~] |
| PAPER-007 | Every paper artefact (order, trade, position, P&L) is labelled `mode=PAPER` end-to-end, including in the UI and reports | A sweep test asserts the label on every persisted artefact created in PAPER mode | `backend/app/core/data_origin.py` | `test_paper_labelling_everywhere` | P0 | PNL-007 | [~] |
| PAPER-008 | A full trading day can be replayed from stored data through the paper engine for regression testing | A recorded session replays end-to-end and produces a deterministic result | `backend/app/marketdata/replay.py`; `backend/app/marketdata/recordings.py`; `backend/app/backtest/engine.py` (stored deterministic sampled session, shared worker; not external market verification) | `backend/tests/integration/test_historical_session.py`; `test_recorded_worker.py`; `backend/tests/unit/test_recorded_replay.py`; `test_recording_bundles.py` | P1 | HD-008 | [✓] |

### 2.37 Supervised trading (`SUP`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| SUP-001 | In SUPERVISED mode every risk-approved proposal is placed into an approval queue instead of being executed | No broker order is created without approval (call counter asserts zero) | `backend/app/execution/supervision.py` | `test_supervised_queue` | P0 | EXEC-013 | [ ] |
| SUP-002 | Approval tokens are single-use, bound to a proposal id, and expire after a configurable window | A reused or expired token is rejected with 409 | `backend/app/execution/supervision.py` | `test_approval_token_lifecycle` | P0 | SUP-001 | [ ] |
| SUP-003 | Approval requests are pushed to the notification channels with the full rationale and risk verdict | Notification payload includes instrument, direction, size, risk, thesis and expiry | `backend/app/notifications/approvals.py` | `test_approval_notification` | P1 | NOTIF-001 | [ ] |
| SUP-004 | On approval, the order is re-validated against current market state and re-checked by the risk engine before submission | A price move that breaches limits between proposal and approval aborts the order | `backend/app/execution/preflight.py` | `test_approval_revalidation` | P0 | RISK-018 | [ ] |
| SUP-005 | Rejection by the owner is recorded with an optional reason and feeds the learning module | Rejected proposals appear in the journal and in learning inputs | `backend/app/journal/service.py` | `test_supervised_rejection_recorded` | P2 | JRN-004 | [ ] |
| SUP-006 | Exits are configurable to be either supervised or automatic, defaulting to **automatic** so a position is never stranded awaiting approval | With the default configuration, a stop-loss exit executes without human approval in SUPERVISED mode | `backend/app/execution/supervision.py` | `test_supervised_exits_automatic` | P0 | EXEC-009 | [ ] |
| SUP-007 | Unapproved proposals expire cleanly with no side effects and are journalled as `EXPIRED_UNAPPROVED` | Expiry test asserts no order, no position and a journal record | `backend/app/execution/supervision.py` | `test_proposal_expiry` | P1 | SUP-002 | [ ] |

### 2.38 Live trading (`LIVE`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| LIVE-001 | `LIVE` mode requires `TRADING_MODE=LIVE` **and** a separate explicit arming action; configuration alone never enables live order flow | With `TRADING_MODE=LIVE` but unarmed, every order attempt is refused with `NOT_ARMED` | `backend/app/modes.py`, `execution/arming.py` | `test_live_requires_arming` | P0 | ARCH-002 | [ ] |
| LIVE-002 | Arming implements the contract §9 checklist in order: explicit LIVE configuration, risk limits configured, valid Groww credentials, verified connectivity, successful account/position sync, passing health checks, explicit owner confirmation | Arming fails at the first unmet precondition and names it; all seven are individually tested | `backend/app/execution/arming.py` | `test_arming_checklist` | P0 | MON-004, REC-003 | [ ] |
| LIVE-003 | Arming confirmation requires a typed confirmation phrase and is recorded with timestamp and user | Confirmation record persisted; a wrong phrase does not arm | `backend/app/execution/arming.py` | `test_arming_confirmation_phrase` | P0 | LIVE-002 | [ ] |
| LIVE-004 | Arming expires at session end and after any restart; the system never comes up armed | After a restart the system is unarmed regardless of prior state | `backend/app/execution/arming.py` | `test_no_auto_rearm` | P0 | DB-018 | [ ] |
| LIVE-005 | Live capital limits: a maximum deployable capital figure distinct from total account capital, configured before arming | Orders exceeding the configured deployable capital are rejected | `backend/app/risk/rules/capital.py` | `test_deployable_capital_limit` | P0 | RISK-004 | [ ] |
| LIVE-006 | A graduated rollout control (maximum trades/day and maximum capital for the first N live sessions) configurable and enforced | With rollout limits active, the (N+1)th live trade is refused | `backend/app/risk/rules/rollout.py` | `test_graduated_rollout_limits` | P1 | LIVE-005 | [ ] |
| LIVE-007 | Only LIVE-approved strategies (STRAT-010) may trade in LIVE mode | An unapproved strategy produces no live order even if enabled in PAPER | `backend/app/strategies/registry.py` | `test_only_approved_strategies_live` | P0 | STRAT-010 | [ ] |
| LIVE-008 | Any critical health-check failure during a live session immediately disables new entries and alerts | Simulated DB/broker/feed failure disables entries within one cycle | `backend/app/monitoring/watchdog.py` | `test_live_health_failure_disables` | P0 | MON-005 | [ ] |
| LIVE-009 | Disarming is always available, takes effect immediately, and optionally flattens positions | Disarm stops new entries instantly; flatten option produces exit orders | `backend/app/execution/arming.py` | `test_disarm` | P0 | EXEC-010 | [ ] |
| LIVE-010 | Live mode cannot start when any component is running with synthetic/demo data or when the paper broker is selected | Misconfiguration (e.g. `TRADING_MODE=LIVE` with `BROKER_PROVIDER=paper`) fails startup with an explicit error | `backend/app/config.py` | `test_live_config_consistency` | P0 | ARCH-008 | [ ] |

### 2.39 Notifications (`NOTIF`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| NOTIF-001 | Notification service with a channel abstraction and at least two working channels (e.g. Telegram bot and email/SMTP), selected by configuration | Both channels send a real message when configured; missing configuration disables that channel with a warning, not a crash | `backend/app/notifications/channels.py` | `backend/tests/unit/test_notifications.py::test_missing_channel_configuration_warns_without_network` | P1 | ARCH-009 | [~] |
| NOTIF-002 | Severity levels (`INFO`, `WARNING`, `CRITICAL`) with per-level channel routing and quiet hours for non-critical messages | Routing matrix honoured; CRITICAL always bypasses quiet hours | `backend/app/notifications/routing.py` | `backend/tests/unit/test_notifications.py::test_quiet_hours_critical_bypass_and_invalid_notice` | P1 | NOTIF-001 | [✓] |
| NOTIF-003 | Mandatory notifications: order rejection, stop-loss hit, daily-loss-limit breach, drawdown breach, kill-switch activation, health-check failure, reconciliation discrepancy, auth failure, feed outage, LLM budget exhaustion | Each event type has a test asserting a notification is emitted with the right severity | `backend/app/notifications/runtime.py`, `outbox.py`, `incidents.py`, `risk/safety.py`, `monitoring/watchdog.py` (PAPER lifecycle, risk, incidents and typed provider-health failures integrated; LLM budget producer and full runtime provider binding pending) | `test_notification_runtime.py`, `test_notification_outbox.py`, `test_paper_incidents.py`, `test_worker_review.py`, `test_health_watchdog.py` (restart-aware auth/feed failure and recovery) | P1 | NOTIF-002 | [~] |
| NOTIF-004 | Approval requests in SUPERVISED mode delivered with actionable detail (SUP-003) | Covered by `test_approval_notification` | `backend/app/notifications/approvals.py` | `test_approval_notification` | P1 | SUP-003 | [ ] |
| NOTIF-005 | Daily summary notification at session end: trades, P&L, limit utilisation, errors and open positions | Summary content matches computed values for a fixture session | `backend/app/notifications/summary.py`, `notifications/recovery.py`, `notifications/valuation.py`, `api/reports.py`, `frontend/src/PaperSummaryPanel.tsx` (PAPER close/recovery and sealed-fill catch-up, shared limits, typed API/UI; historical marks/order state and other modes partial) | `test_daily_summary.py`, `test_summary_recovery.py`, `test_paper_browser.py`, `PaperSummaryPanel.test.tsx`; costs, restart/catch-up, cutoff FIFO, legacy gaps, audit rollback/integrity, real dashboard polling | P2 | PNL-004 | [~] |
| NOTIF-006 | Deduplication and rate limiting so a repeated condition does not flood channels | The same alert repeated 100 times sends once plus a suppression count | `backend/app/notifications/dedupe.py`, `backend/app/notifications/service.py` | `backend/tests/unit/test_notification_dedupe.py` | P2 | NOTIF-001 | [✓] |
| NOTIF-007 | Notification delivery failures are logged and retried, and never block the trading loop | A failing channel does not raise into the trading path (asserted) | `backend/app/notifications/service.py` | `backend/tests/unit/test_notifications.py::test_retry_failure_isolation_redaction_and_routing` | P1 | ERR-005 | [✓] |

### 2.40 Logging (`LOG`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| LOG-001 | Structured JSON logging with level, timestamp (IST + UTC), logger, correlation id, and contextual fields | Log output parses as JSON; required fields present on every record | `backend/app/core/logging.py` | `test_structured_logging` | P0 | ARCH-012 | [✓] |
| LOG-002 | Separate log streams/files for application, trading decisions, broker I/O and errors, with rotation and retention | Files created and rotated per configuration; retention enforced | `backend/app/core/logging.py`, `docker/` | `test_log_rotation` | P1 | LOG-001 | [✓] |
| LOG-003 | Secret redaction applied at the handler level so no code path can leak credentials to logs (SEC-004) | Covered by `test_log_redaction` | `backend/app/core/logging.py` | `test_log_redaction` | P0 | SEC-004 | [✓] |
| LOG-004 | Every broker request and response logged with latency, status, correlation id and rate-limit bucket | Log record exists for each call in an integration test | `backend/app/brokers/groww/client.py` | `test_broker_io_logging` | P1 | GRW-024 | [ ] |
| LOG-005 | Log level configurable per module; DEBUG never enabled by default in LIVE | Starting in LIVE with DEBUG configured emits a warning and is recorded | `backend/app/config.py` | `test_log_level_policy` | P2 | ARCH-009 | [✓] |

### 2.41 Monitoring & health (`MON`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| MON-001 | Health-check framework with named checks, criticality flags, timeouts and last-result caching | Each check returns `{name, status, detail, checked_at, critical}`; a hanging check times out rather than blocking | `backend/app/monitoring/healthchecks.py` | `test_healthcheck_framework` | P0 | ARCH-009 | [✓] |
| MON-002 | Startup health checks covering exactly the contract §10 list: database, Redis, market data, Groww authentication, account information, margin information, positions, instruments, news service, risk engine, order service, system clock, market status | All thirteen checks exist and are individually testable; the list is asserted by a test against the contract | `backend/app/monitoring/startup.py` | `test_startup_check_coverage` | P0 | MON-001 | [~] |
| MON-003 | System-clock check comparing local time to a reference and failing beyond a configured skew | Injected skew beyond the threshold fails the check | `backend/app/monitoring/checks/clock.py` | `test_clock_skew_check` | P0 | ARCH-012 | [x] |
| MON-004 | **Critical failure disables trading** (contract §10): if any critical check fails, trading is disabled and the reason is surfaced everywhere (API, UI, notifications) | With a failing critical check, every trading endpoint returns 423 and the dashboard shows `TRADING DISABLED` with the component named | `backend/app/monitoring/gate.py` | `test_critical_failure_disables_trading` | P0 | MON-002 | [✓] |
| MON-005 | Continuous watchdog re-running health checks during the session at a configured interval | A component failing mid-session disables new entries within one interval | `backend/app/monitoring/watchdog.py`, `main.py`, `trading/health.py` (selected PAPER provider and audited quote refresh integrated; complete external/runtime-check coverage pending) | `test_health_watchdog.py`, `test_app_boot.py`, `test_market_bootstrap.py`; bounded checks, fail-closed audit, read-only bootstrap, recovery preserves latches | P0 | MON-004 | [~] |
| MON-006 | Runtime metrics: cycle latency, broker call latency and error rates, feed lag, queue depths, LLM latency and cost, DB pool usage — exposed in a machine-readable endpoint | `/metrics` returns all documented metrics with correct types | `backend/app/monitoring/metrics.py` | `test_metrics_endpoint` | P2 | BE-001 | [ ] |
| MON-007 | Heartbeat record written at a fixed cadence so an external watcher can detect a stalled process | Heartbeat timestamp advances; a frozen loop is detectable | `backend/app/trading/worker.py`, `trading/health.py` (persisted PAPER cycle heartbeat and runtime freshness checks; independent external watcher pending) | `test_paper_worker.py`, `test_runtime_health.py` | P1 | DB-018 | [~] |
| MON-008 | Data-quality monitoring: counts of stale ticks, rejected candles, feed gaps and reconciliation discrepancies, with thresholds that raise alerts | Exceeding a threshold emits an alert and marks the system DEGRADED | `backend/app/monitoring/data_quality.py` | `test_data_quality_monitoring` | P2 | MD-009 | [ ] |
| MON-009 | Health status is persisted so post-incident review can show what was failing at any past moment | Historical health records queryable by time range | `backend/app/monitoring/watchdog.py`, `monitoring/history.py`, `api/health.py`, `frontend/src/HealthHistoryPanel.tsx`; authenticated bounded audited interval inspection integrated; observation gaps are explicit, archive/checkpoint verification above 10,000 chain records pending | `test_health_watchdog.py`, `tests/integration/test_health_history.py` (actual PostgreSQL and Edge), `frontend/src/HealthHistoryPanel.test.tsx`, `frontend/tests/health-history-browser.mjs` | P2 | DB-018 | [~] |

### 2.42 Error handling (`ERR`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| ERR-001 | Typed exception hierarchy separating transient, permanent, configuration, data-quality and safety errors | Every raised exception in the codebase derives from the base class (AST test) | `backend/app/core/errors.py` | `test_exception_hierarchy` | P0 | — | [✓] |
| ERR-002 | Every external operation handles the contract §11 list explicitly: timeout, connection failure, authentication failure, rate limit, invalid response, duplicate order, partial fill, rejected order, unknown order status, stale market data | A matrix test drives each failure mode against the broker adapter and asserts the documented behaviour | `backend/app/brokers/groww/`, `execution/` | `test_failure_mode_matrix` | P0 | GRW-003 | [ ] |
| ERR-003 | Fail-closed default for anything touching order flow: unexpected errors stop trading rather than continuing | An unexpected exception in the cycle disables new entries and alerts | `backend/app/execution/intraday_loop.py` | `test_fail_closed_on_unexpected_error` | P0 | RISK-017 | [ ] |
| ERR-004 | Circuit breakers per external dependency (broker, LLM, news) that open on repeated failures and recover on probe success | Breaker opens after N failures, half-opens after the cooldown, closes on success | `backend/app/core/circuit_breaker.py` | `test_circuit_breaker_states` | P1 | ERR-001 | [ ] |
| ERR-005 | Non-critical subsystem failures (news, notifications, metrics) degrade without stopping trading | Each subsystem is killed in a test while the trading loop continues in a defined degraded state | `backend/app/core/degradation.py` | `test_graceful_degradation` | P1 | ERR-001 | [ ] |
| ERR-006 | Dead-letter handling for events that repeatedly fail processing, with operator visibility | Poison events land in a dead-letter stream and raise an alert instead of looping forever | `backend/app/core/events.py`, `backend/app/api/event_failures.py`, `backend/app/monitoring/event_delivery.py`, `frontend/src/EventFailurePanel.tsx`; Redis retention, inspection and atomic audit/outbox publication integrated; runtime producer/consumer and full operational acceptance pending; `docs/EVENT_DELIVERY.md` | `backend/tests/integration/test_redis_events.py`, `test_event_failure_api.py`, `test_event_failure_publication.py`, `test_event_publication_postgres.py`, `frontend/src/EventFailurePanel.test.tsx`, `frontend/tests/event-failures-browser.mjs` | P2 | ARCH-016 | [~] |
| ERR-007 | All errors carry a stable machine-readable code plus human detail, surfaced in the API and UI | Error taxonomy documented; API error responses validated against a schema | `backend/app/core/errors.py`, `docs/ERRORS.md` | `test_error_response_schema` | P1 | ERR-001 | [~] |

### 2.43 State recovery (`REC`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| REC-001 | Startup recovery sequence implementing contract §12 exactly: connect to DB, authenticate, fetch broker positions, fetch broker orders, reconcile local state, detect discrepancies, resolve or flag, and only then allow trading | The sequence is implemented as ordered, individually testable steps; skipping a step is impossible by construction | `backend/app/recovery/startup.py` | `test_recovery_sequence_order` | P0 | MON-002 | [ ] |
| REC-002 | Order reconciliation: every local in-flight order is matched to a broker order by reference id; unmatched orders in either direction are flagged | PAPER local-only, broker-only and changed request terms persist exact evidence; complete status/fill recovery and external broker acceptance pending | `backend/app/execution/paper.py`, `backend/app/execution/discrepancies.py` | `tests/integration/test_order_discrepancies.py`, `tests/integration/test_paper_execution.py` | P0 | OMS-002 | [~] |
| REC-003 | Position reconciliation: broker positions are authoritative; local differences produce a `DISCREPANCY` record with quantity and price deltas | A seeded mismatch is detected with exact deltas; PAPER evidence verified, external broker reconciliation pending | `backend/app/execution/discrepancies.py`, `backend/app/execution/paper.py` | `tests/integration/test_reconciliation_review.py`, `tests/integration/test_reconciliation_postgres.py` | P0 | PORT-008 | [~] |
| REC-004 | Unresolved discrepancies block trading until explicitly resolved by the owner, with an audited resolution action | PAPER entries stay blocked across executor restart; authenticated fresh-state review records owner/time without clearing unrelated gates; other modes pending | `backend/app/api/reconciliation.py`, `backend/app/execution/discrepancies.py`, `frontend/src/ReconciliationPanel.tsx` | `tests/integration/test_reconciliation_review.py`, `frontend/tests/reconciliation-browser.mjs` | P0 | REC-003 | [~] |
| REC-005 | Orphan-position detection: a broker position with no local record is adopted into local state with a flagged origin, never ignored | PAPER adoption, owner acknowledgment, audit-backed FIFO/protection plans and explicit original-projection restoration integrated with fresh persisted broker checks; archived orphan provenance retained, no invented trades or re-arm; genuinely absent history, broader recovery and other modes pending | `backend/app/execution/orphans.py`, `backend/app/execution/orphan_review.py`, `backend/app/execution/position_recovery.py`, `backend/app/execution/recovery_protection.py`, `backend/app/execution/orphan_restore.py`, `backend/app/api/reconciliation.py`, `frontend/src/OrphanReviewPanel.tsx`; `docs/PAPER_EXECUTION.md` | `tests/integration/test_orphan_restore.py`, `tests/integration/test_orphan_review.py`, `tests/integration/test_orphan_recovery.py`, `tests/integration/test_orphan_postgres.py`, `tests/integration/test_position_recovery_plan.py`, `tests/integration/test_recovery_plan_postgres.py`, `frontend/tests/orphan-browser.mjs` | P0 | REC-003 | [~] |
| REC-006 | Unprotected-position detection: an open position with no active stop is detected at startup and immediately protected or flagged critical | Startup with an unprotected position raises a critical alert and arms protection | `backend/app/execution/paper.py`, `backend/app/trading/worker.py` | `tests/integration/test_protection_observation.py` | P0 | EXEC-003 | [~] |
| REC-007 | Recovery is idempotent and safe to run repeatedly | Running recovery twice produces the same final state and no duplicate records; PAPER entry identity/fills survive actual child termination; repeated owner recovery finalizes interrupted replacement evidence without replaying orders; complete recovery scope pending | `backend/app/execution/paper.py`, `backend/app/trading/worker.py`, `backend/app/trading/recover.py` | `tests/integration/test_process_recovery.py` (SQLite and disposable PostgreSQL; open/pending order; no duplicate entry or fill), `test_orphan_restore.py`, `test_owner_replace.py` | P1 | REC-001 | [~] |
| REC-008 | Survives the four failure classes in contract §12 — application restart, server restart, network failure, database restart — each with an automated test | Actual application subprocess restart, isolated PostgreSQL connection termination and actual loopback TCP outage/recovery tested; entries stay blocked after owner recovery with accounting/order identity preserved; broader broker-network and server/database restart classes pending | `backend/app/execution/paper.py`, `backend/app/trading/worker.py`, `backend/app/trading/recover.py`; isolated subprocess and TCP test helpers | `tests/integration/test_process_recovery.py`, `test_database_recovery_control.py`, `test_network_recovery.py`, `test_worker_recovery_control.py`; disposable PostgreSQL databases, no owner service restart | P0 | REC-001 | [~] |
| REC-009 | Redis loss is tolerated: caches rebuild, locks are re-acquired, and no trading decision depends on cache-only state | Flushing Redis mid-session does not corrupt positions or orders | `backend/app/core/locks.py` | `test_redis_loss_tolerance` | P1 | ARCH-011 | [ ] |

### 2.44 Emergency controls (`EMG`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| EMG-001 | Kill switch that immediately stops all new order generation, persists across restarts, and can only be cleared explicitly | Activating the switch blocks all entries within one cycle; restart does not clear it | `backend/app/emergency/controls.py` (PAPER integrated; broader modes pending) | `test_emergency.py` | P0 | DB-018 | [~] |
| EMG-002 | Flatten-all: cancel every open order and exit every open position through the safe execution path | Flatten produces cancels plus correctly signed exits for all positions and reports per-item outcomes | `backend/app/emergency/flatten.py` (PAPER entries cancelled; existing exits retained; replacement/recovery pending) | `test_emergency.py`, `test_paper_browser.py`: actual owner UI confirmation, idempotent close, stale quote leaves pending exit without fabricated fill | P0 | EXEC-010 | [~] |
| EMG-003 | Disable-new-entries (softer control) that allows exits and management of existing positions | With it active, entries are refused and exits succeed | `backend/app/emergency/controls.py` (PAPER integrated) | `test_emergency.py` | P0 | EMG-001 | [~] |
| EMG-004 | Automatic triggers: daily-loss breach, drawdown breach, reconciliation discrepancy, repeated order rejections, critical health failure and feed loss beyond a threshold each engage the appropriate control automatically | Each trigger has a test asserting the control engaged and the alert sent | `backend/app/emergency/rejections.py`, existing risk safety, reconciliation incidents and health watchdog (PAPER integration; remote delivery and cross-mode coverage unverified) | `test_rejection_trigger.py`, `test_risk_safety.py`, `test_notification_runtime.py`, `test_paper_incidents.py`, `test_health_watchdog.py` — durable blocks, restart, transactional outbox, safe exits; isolated transport acknowledgements only | P0 | RISK-005 | [~] |
| EMG-005 | The kill switch is reachable from the dashboard, the API and a standalone CLI that works even if the web app is down | CLI invocation sets the persisted state and the running process honours it within one cycle | `backend/app/emergency/cli.py`; `docs/EMERGENCY_CLI.md` (authenticated PAPER-only command, shared durable state; broader-mode/deployment verification pending) | `test_emergency_cli.py` (actual separate process without HTTP, worker cycle/restart, bad credentials/confirmation, revocation, unavailable schema); existing API/browser controls | P0 | EMG-001 | [~] |
| EMG-006 | Every emergency action is audited with actor, timestamp, reason and the resulting state | Audit record exists for each action and is immutable | `backend/app/emergency/controls.py`, `flatten.py`, `audit.py`, `api/emergency.py` (PAPER results and authenticated typed request refusals audited; malformed/unauthenticated requests and broader mode parity remain separate) | `test_emergency.py` (confirmation, mode, health, missing worker, unexpected failure, storage failure and queryable evidence) | P0 | AUDIT-001 | [~] |
| EMG-007 | Clearing an emergency state requires explicit confirmation and re-running health checks before trading resumes | Clearing without passing health checks leaves trading disabled | `backend/app/emergency/controls.py` (PAPER health/reconciliation integrated; worker-error review separate) | `test_emergency.py` | P0 | MON-004 | [~] |

### 2.45 Audit trail (`AUDIT`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| AUDIT-001 | Append-only audit log recording the full contract §13 field set: timestamp, market state, data used, news used, indicators, strategy, signal, risk calculation, position size, decision, risk approval/rejection, order, broker response, exit and result | A single e2e trade produces a complete audit chain containing every listed field | `backend/app/audit/service.py`, `portfolio/fill_evidence.py` (PAPER fill economic snapshot committed atomically with accounting; broader field/mode parity partial) | `backend/tests/integration/test_audit.py::test_pipeline_positive_audit_and_failure_atomicity`, `test_summary_recovery.py` | P0 | DB-012 | [~] |
| AUDIT-002 | Negative decisions are audited with the same rigour as positive ones | For an instrument considered and skipped, the audit contains the evaluated inputs and the binding reason | `backend/app/audit/service.py` | `backend/tests/integration/test_audit.py::test_audit_negative_decisions` | P0 | AID-009 | [✓] |
| AUDIT-003 | Audit chains are queryable by trade, by instrument, by day and by decision id, and are exposed in the API and UI | The "why this trade / why not that trade" questions are answerable from the UI alone | `backend/app/api/audit.py`, `backend/app/audit/read.py`, `frontend/src/AuditPanel.tsx` | `backend/tests/integration/test_audit_read.py`, `frontend/src/AuditPanel.test.tsx`, `frontend/tests/summary-browser.mjs` — PAPER lifecycle and negative decision drill-down; completeness anchors, legacy linkage and all external/LLM paths remain unverified | P0 | FE-009 | [~] |
| AUDIT-004 | Audit records store the exact data snapshots used (prices, indicator values, Greeks, news ids), not references that may later change | Replaying a stored audit record reproduces the same decision inputs even after the underlying data changes | `backend/app/audit/snapshots.py` | `backend/tests/integration/test_audit.py::test_source_change_does_not_change_audit_snapshot` | P0 | AUDIT-001 | [✓] |
| AUDIT-005 | LLM interactions in the decision path are audited: prompt version, model, inputs, raw output and validation result | Every proposal links to its LLM call record | `backend/app/llm/telemetry.py; backend/app/agents/pipeline.py` | `backend/tests/integration/test_reference_worker.py` | P1 | LLM-012 | [~] |
| AUDIT-006 | Configuration changes (risk limits, strategy enablement, mode, arming) are audited with actor and before/after values | Each change type has an audit test | `backend/app/audit/config.py` | `test_config_change_audit` | P0 | RISK-016 | [~] |
| AUDIT-007 | Audit integrity: records are hash-chained so tampering is detectable | Altering a stored record breaks chain verification in a test | `backend/app/audit/integrity.py` | `backend/tests/integration/test_audit.py::test_audit_hash_chain` | P2 | DB-012 | [✓] |

### 2.46 Self-learning (`LEARN`)

Bounded by contract §14 and §15: learning may inform, recommend and report — it may never autonomously alter risk limits or enable a strategy for LIVE.

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| LEARN-001 | Outcome labelling: every closed trade is labelled with result, R-multiple, exit reason, regime, strategy and adherence to plan | Labels computed deterministically from journal data | `backend/app/learning/labelling.py` | `test_outcome_labelling` | P2 | JRN-001 | [ ] |
| LEARN-002 | Performance attribution across strategy, regime, instrument, time-of-day, holding period and confidence bucket | Attribution tables reconcile to total P&L | `backend/app/learning/attribution.py` | `test_learning_attribution` | P2 | PNL-005 | [ ] |
| LEARN-003 | AI memory store: durable, queryable records of past setups, outcomes and lessons, retrievable as structured context for future decisions | A similar future setup retrieves the relevant prior records by structured similarity (not free-text recall) | `backend/app/learning/memory.py` | `test_ai_memory_retrieval` | P2 | DB-013 | [ ] |
| LEARN-004 | Memory is evidence-linked: every remembered lesson references the journal entries that produced it | A lesson with no supporting entries cannot be stored | `backend/app/learning/memory.py` | `test_memory_evidence_required` | P2 | LEARN-003 | [ ] |
| LEARN-005 | Calibration analysis: stated AI confidence versus realised win rate, reported per bucket | Calibration curve computed and included in monthly reports | `backend/app/learning/calibration.py` | `test_confidence_calibration` | P3 | LEARN-002 | [ ] |
| LEARN-006 | Parameter-review recommendations produced as **proposals for human approval only**, never applied automatically | An accepted recommendation requires an explicit owner action and a re-validation run before LIVE use | `backend/app/learning/recommendations.py` | `test_recommendations_require_approval` | P0 | STRAT-011 | [ ] |
| LEARN-007 | Strategy degradation detection feeding automatic **disablement** (a safe direction) while enablement always stays manual | Degradation auto-disables; no code path auto-enables | `backend/app/learning/degradation.py` | `test_auto_disable_never_auto_enable` | P0 | STRAT-012 | [ ] |
| LEARN-008 | Learning inputs are restricted to the system's own journal and market data — no external performance claims are ingested | Import/test asserts the learning module reads only internal tables | `backend/app/learning/` | `test_learning_inputs_internal_only` | P2 | LEARN-001 | [ ] |
| LEARN-009 | Overfitting guard: recommendations derived from fewer than a configured minimum number of trades are labelled statistically insufficient and cannot be acted upon | A recommendation from 5 trades is blocked with `INSUFFICIENT_SAMPLE` | `backend/app/learning/recommendations.py` | `test_insufficient_sample_guard` | P2 | LEARN-006 | [ ] |

### 2.47 Reporting (`RPT`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| RPT-001 | Monthly report generator producing a complete performance and behaviour report for a calendar month | Report generated for a fixture month with every section populated from real stored data | `backend/app/reporting/monthly.py` | `test_monthly_report_generation` | P3 | PNL-006 | [ ] |
| RPT-002 | Report contents: P&L summary, equity and drawdown curves, trade statistics, per-strategy and per-regime attribution, risk-limit utilisation, rejected-proposal analysis, execution quality, LLM cost, incidents and data-quality summary | Each section has a test asserting it is populated and reconciles to underlying data | `backend/app/reporting/sections/` | `test_monthly_report_sections` | P3 | RPT-001 | [ ] |
| RPT-003 | Reports are mode-segregated: paper and live performance never appear in the same aggregate figure | A month containing both produces clearly separated sections | `backend/app/reporting/monthly.py` | `test_report_mode_separation` | P3 | PNL-007 | [ ] |
| RPT-004 | Report export to PDF and HTML, archived and retrievable via the API and dashboard | Generated file is retrievable and its content matches the stored data | `backend/app/reporting/export.py` | `test_report_export` | P3 | RPT-001 | [ ] |
| RPT-005 | Scheduled generation on the first trading day of each month, with a notification on completion | Scheduler test with a fake clock triggers generation exactly once | `backend/app/reporting/schedule.py` | `test_report_schedule` | P3 | BE-012 | [ ] |
| RPT-006 | Daily and weekly summaries as lighter variants of the same pipeline | Both generate from the same section modules without duplicated logic | `backend/app/reporting/periodic.py` | `test_periodic_reports` | P3 | RPT-002 | [ ] |
| RPT-007 | Tax-oriented trade export (realised P&L per trade with dates, charges and instrument details) suitable for handing to an accountant | Export covers a financial year and reconciles to realised P&L | `backend/app/reporting/tax_export.py` | `test_tax_export` | P3 | PNL-001 | [ ] |

### 2.48 Compliance (`CMP`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| CMP-001 | Order-rate governor keeping order submissions below the SEBI retail self-algo threshold (10 orders/second) with a configurable safety margin | A burst is paced below the configured threshold; the governor is independent of the broker rate limiter and cannot be disabled in LIVE | `backend/app/compliance/order_rate.py` | `test_order_rate_governor` | P0 | EXEC-012 | [ ] |
| CMP-002 | Algo-order tagging support: every order carries the broker/exchange-provided algo identifier field when the broker exposes it, and the system records which identifier was used | Tag field is populated from configuration and stored with every order; absence is recorded explicitly rather than silently omitted | `backend/app/compliance/algo_tagging.py` | `test_algo_tagging` | P0 | DB-007 | [ ] |
| CMP-003 | Compliance configuration block declaring account type, API registration status and algo registration details, surfaced in the dashboard | Values are configured, displayed and included in the audit record for every order | `backend/app/compliance/config.py` | `test_compliance_config_surfaced` | P1 | CMP-002 | [ ] |
| CMP-004 | Single-account, single-owner operation only: the system explicitly does not support managing third-party funds or accounts | A documented constraint plus a config guard that rejects multiple account credentials | `backend/app/compliance/scope.py`, `docs/COMPLIANCE.md` | `test_single_account_guard` | P1 | ARCH-009 | [ ] |
| CMP-005 | Complete order and decision records retained for a configurable retention period sufficient for regulatory and broker queries | Retention configured, enforced and documented; deletion before the period raises | `backend/app/compliance/retention.py` | `test_retention_policy` | P2 | DB-012 | [ ] |
| CMP-006 | A compliance statement in the documentation covering: broker T&C adherence, SEBI retail-algo framework obligations, the owner's responsibility to register the algo with the broker where required, and the absence of any investment advice | `docs/COMPLIANCE.md` exists with all four sections | `docs/COMPLIANCE.md` | `test_compliance_doc_sections` | P1 | DOC-001 | [ ] |
| CMP-007 | Self-trade prevention: the system will not simultaneously hold opposing orders in the same instrument that could cross with itself | An opposing-order scenario is blocked before submission | `backend/app/compliance/self_trade.py` | `test_self_trade_prevention` | P1 | OMS-001 | [ ] |

### 2.49 Testing (`TEST`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| TEST-001 | Pytest suite with unit/integration/e2e separation, fixtures, a fake clock, a fake broker and a fake LLM | `pytest -m unit` runs with no external services; markers enforced in config | `backend/tests/conftest.py` | self-verifying | P0 | — | [✓] |
| TEST-002 | Unit tests for every indicator against hand-computed reference values (contract §8) | Each indicator in TA-002…TA-009 has at least one reference test | `backend/tests/unit/analysis/` | `test_indicators_*` | P0 | TA-001 | [✓] |
| TEST-003 | Unit tests for position sizing across normal, boundary and zero-size cases | Covers SIZE-001…SIZE-007 | `backend/tests/unit/sizing/` | `test_sizing_*` | P0 | SIZE-001 | [ ] |
| TEST-004 | Unit tests for every risk rule including boundary conditions exactly at each limit | Each rule in RISK-003…RISK-014 has pass, fail and exactly-at-limit cases | `backend/tests/unit/risk/` | `test_risk_*` | P0 | RISK-002 | [ ] |
| TEST-005 | Unit tests for P&L: realised FIFO, unrealised, costs, short positions, F&O lot multiples, partial exits | Covers PNL-001…PNL-006 with hand-computed expectations | `backend/tests/unit/portfolio/` | `test_pnl_*` | P0 | PNL-001 | [ ] |
| TEST-006 | Unit tests for option mathematics and Greeks against published reference values, plus IV round-trip | Covers GRK-001…GRK-007 | `backend/tests/unit/fno/` | `test_greeks_*` | P0 | GRK-002 | [✓] |
| TEST-007 | Unit tests for signal generation determinism and regime gating | Covers STRAT-003, STRAT-007, REG-004 | `backend/tests/unit/strategies/` | `test_signals_*` | P0 | STRAT-001 | [ ] |
| TEST-008 | Unit tests for market-regime detection including hysteresis and labelled fixtures | Covers REG-001…REG-003 | `backend/tests/unit/regime/` | `test_regime_*` | P1 | REG-001 | [ ] |
| TEST-009 | Unit tests for stop-loss calculation and placement logic, including wrong-side and zero-distance rejection | Covers RISK-009, EXEC-003 | `backend/tests/unit/risk/` | `test_stops_*` | P0 | RISK-009 | [ ] |
| TEST-010 | Unit tests for portfolio exposure and concentration across CASH and FNO with notional handling | Covers PORT-004, FNOR-001, RISK-007 | `backend/tests/unit/portfolio/` | `test_exposure_*` | P0 | PORT-004 | [ ] |
| TEST-011 | Unit tests for the daily loss limit, including the exact-threshold case and mid-session breach | Covers RISK-005, INTRA-007 | `backend/tests/unit/risk/` | `test_daily_loss_*` | P0 | RISK-005 | [ ] |
| TEST-012 | Integration tests against a real PostgreSQL+TimescaleDB and Redis in containers | Suite runs against `docker compose -f docker-compose.test.yml`; migrations applied automatically | `backend/tests/integration/` | `test_db_*`, `test_redis_*` | P0 | DB-001 | [ ] |
| TEST-013 | Integration tests for the Groww adapter driven by contract fixtures and a local mock HTTP server — clearly separated from production code and never presented as live verification | Every implemented endpoint has success and failure paths; the suite states explicitly that it validates the client, not the live API | `backend/tests/integration/groww/` | `test_groww_adapter_*` | P0 | GRW-026 | [ ] |
| TEST-014 | Integration tests for the market-data adapter including batching, chunking, staleness and failover | Covers MD-001…MD-010 | `backend/tests/integration/marketdata/` | `test_marketdata_*` | P1 | MD-001 | [ ] |
| TEST-015 | Integration tests for the news adapter including dedupe, verification and outage degradation | Covers NEWS-001…NEWS-012 | `backend/tests/integration/news/` | `test_news_*` | P2 | NEWS-001 | [ ] |
| TEST-016 | Integration tests for the full order lifecycle against the paper broker and the mock Groww server | Create → open → partial → filled → exit, plus cancel and reject paths | `backend/tests/integration/execution/` | `test_order_lifecycle` | P0 | OMS-001 | [ ] |
| TEST-017 | **End-to-end test of the contract §8 chain:** market data → analysis → strategy → risk → order → position → exit → P&L → journal | One test drives the entire chain in PAPER mode and asserts a complete journal entry and correct P&L at the end | `backend/tests/integration/test_reference_worker.py`, `test_paper_browser.py`; actual services, isolated external market fixture | `test_worker_produces_trade_exits_and_deduplicates_after_restart`, `test_real_browser_observes_costed_reference_trade` (real React/API, explicit estimated costs/net journal; not full-day/live validation) | P0 | JRN-001 | [✓] |
| TEST-018 | End-to-end test of the rejection path: a proposal that violates a risk rule produces no order and a complete auditable "why not" record | Asserts zero broker calls and a populated audit chain | `backend/tests/e2e/test_rejection_cycle.py` | `test_end_to_end_rejection` | P0 | AUDIT-002 | [ ] |
| TEST-019 | End-to-end test with the LLM disabled, proving the system trades deterministically or stands down as declared | Runs with `LLM_PROVIDER=fallback` and with the provider forced to fail | `backend/tests/e2e/test_llm_degradation.py` | `test_e2e_without_llm` | P0 | LLM-011 | [ ] |
| TEST-020 | Recovery tests for the four §12 failure classes | Covered by REC-008 | `backend/tests/integration/recovery/` | `test_recovery_*` | P0 | REC-008 | [ ] |
| TEST-021 | Safety tests asserting the impossible things stay impossible: LLM cannot alter limits, synthetic data cannot reach LIVE, unapproved strategies cannot trade live, unarmed LIVE cannot order, kill switch cannot be bypassed | Five dedicated tests, each asserting zero broker calls | `backend/tests/safety/` | `test_safety_invariants` | P0 | AID-010 | [ ] |
| TEST-022 | Property-based tests for money arithmetic, FIFO matching and sizing invariants | Hypothesis-based tests find no invariant violations over generated inputs | `backend/tests/property/` | `test_properties_*` | P2 | ARCH-014 | [ ] |
| TEST-023 | Frontend component and integration tests with mocked API responses, including error and stale states | Vitest suite covers every page listed in §2.4 | `frontend/src/**/*.test.tsx` | `npm run test` | P1 | FE-001 | [ ] |
| TEST-024 | Coverage measurement with a configured minimum on safety-critical modules (risk, sizing, execution, recovery, compliance) | CI fails if coverage on those packages drops below the configured threshold | `backend/pyproject.toml`, CI | CI job `coverage` | P1 | TEST-001 | [ ] |
| TEST-025 | A deterministic seeded market simulator used by e2e tests so results are reproducible | Two runs of the e2e suite produce identical trade sequences | `backend/tests/simulator/` | `test_simulator_determinism` | P1 | TEST-017 | [ ] |
| TEST-026 | Test-data policy: all fixtures are clearly labelled synthetic and live under `tests/`, never importable by production code | Import-graph test asserts production code never imports from `tests/` | `backend/tests/` | `test_fixtures_not_in_production` | P0 | ARCH-008 | [✓] |

### 2.50 Deployment (`DEPLOY`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| DEPLOY-001 | Docker Compose stack: backend, frontend, PostgreSQL+TimescaleDB, Redis, with health checks and dependency ordering | `docker compose up` brings up a working system reachable on the documented ports | `docker/docker-compose.yml` | `test_compose_smoke` (integration) | P0 | DB-001 | [~] |
| DEPLOY-002 | Separate compose overlays for development, test and production-like operation | Each overlay starts successfully; production overlay disables debug and enforces non-default secrets | `docker/` | `test_compose_overlays` | P1 | DEPLOY-001 | [ ] |
| DEPLOY-003 | CI pipeline running lint, type check, unit tests, integration tests and the security scan on every commit | Pipeline defined and green on the initial codebase | `.github/workflows/ci.yml` | CI | P1 | TEST-001 | [ ] |
| DEPLOY-004 | Startup script that runs migrations, executes health checks and refuses to start trading when checks fail | Starting with a broken dependency exits non-zero with a clear message | `docker/entrypoint.sh` | `test_entrypoint_gating` | P0 | MON-002 | [x] |
| DEPLOY-005 | Configuration validation at boot, printing a redacted effective configuration and the resolved mode | Boot log shows mode, broker provider, LLM provider and limit summary with secrets redacted | `backend/app/config.py` | `test_boot_config_summary` | P1 | ARCH-009 | [✓] |
| DEPLOY-006 | Time-zone and locale pinned to `Asia/Kolkata` in containers | Container date matches IST in a smoke test | `docker/Dockerfile` | `test_container_timezone` | P1 | ARCH-012 | [x] |
| DEPLOY-007 | Resource and restart policy: automatic restart on crash with backoff, and a documented "never auto-arm on restart" guarantee | Container restarts after a kill; the system comes back unarmed (LIVE-004) | `docker/docker-compose.yml` | `test_restart_unarmed` | P0 | LIVE-004 | [ ] |
| DEPLOY-008 | Single-machine deployment guide covering a local workstation and a small VPS, including firewall and TLS guidance | `docs/DEPLOYMENT.md` complete with both paths and verified command sequences | `docs/DEPLOYMENT.md` | doc review | P1 | SEC-009 | [ ] |
| DEPLOY-009 | Database migration and rollback procedure documented and exercised | Upgrade and downgrade both tested on a scratch database | `backend/alembic/`, `docs/OPERATIONS.md` | `test_migration_rollback` | P2 | DB-003 | [ ] |
| DEPLOY-010 | Version stamping: build version and git commit exposed in `/api/v1/system/status` and in logs | Version endpoint returns the build metadata injected at image build time | `backend/app/core/version.py` | `test_version_stamp` | P2 | BE-003 | [ ] |

### 2.51 Documentation (`DOC`)

| ID | Requirement | Acceptance criteria | Module/File | Test | Pri | Depends | Status |
|---|---|---|---|---|---|---|---|
| DOC-001 | `README.md`: what the system is, what it is not, architecture overview, quick start and the safety model | Present and accurate; a new reader can reach a running PAPER system from it alone | `README.md` | doc review | P0 | — | [ ] |
| DOC-002 | `docs/INSTALL.md`: prerequisites, installation, database setup, first run | Commands verified on a clean machine checklist | `docs/INSTALL.md` | doc review | P0 | DEPLOY-001 | [ ] |
| DOC-003 | `docs/CONFIGURATION.md`: every environment variable with type, default, effect and whether it is required per mode | A test asserts every `Settings` field appears in the document | `docs/CONFIGURATION.md` | `test_config_documented` | P0 | ARCH-009 | [ ] |
| DOC-004 | `docs/MODES.md`: PAPER, SUPERVISED and LIVE explained, including the exact steps to move between them and the arming ceremony | Steps match the implemented gates one-for-one | `docs/MODES.md` | doc review | P0 | LIVE-002 | [ ] |
| DOC-005 | `docs/ARCHITECTURE.md`: component map, data flow, the decision pipeline and the risk-precedence guarantee | Diagram matches the implemented module layout | `docs/ARCHITECTURE.md` | doc review | P1 | ARCH-001 | [ ] |
| DOC-006 | `docs/RISK.md`: every risk rule, its formula, its configuration key and its rejection code | A test asserts every registered rule appears in the document | `docs/RISK.md` | `test_risk_rules_documented` | P0 | RISK-002 | [ ] |
| DOC-007 | `docs/STRATEGIES.md` plus a `SPEC.md` per strategy following the §15 template | Every registered strategy has a spec containing all required sections (STRAT-013) | `docs/strategies/` | `test_strategy_spec_completeness` | P1 | STRAT-013 | [ ] |
| DOC-008 | `docs/OPERATIONS.md`: daily run book, start/stop, monitoring, backup/restore, incident response and emergency procedures | Run book covers each emergency control and recovery path | `docs/OPERATIONS.md` | doc review | P1 | EMG-005 | [ ] |
| DOC-009 | `docs/SECURITY.md`: secret handling, network exposure, credential rotation and what to do if a key leaks | Present and consistent with the implemented controls | `docs/SECURITY.md` | doc review | P0 | SEC-009 | [ ] |
| DOC-010 | `docs/COMPLIANCE.md` per CMP-006, including the explicit statement that the system provides no investment advice and that the owner bears responsibility for regulatory obligations | All four required sections present | `docs/COMPLIANCE.md` | `test_compliance_doc_sections` | P1 | CMP-006 | [ ] |
| DOC-011 | `docs/API.md` or published OpenAPI documentation covering every endpoint with examples | Generated from the live schema; examples execute successfully against a test server | `docs/API.md` | `test_api_docs_generated` | P2 | BE-001 | [ ] |
| DOC-012 | `docs/DATA.md`: data sources, coverage, the Groww 3-month intraday limitation, archival strategy and known data-quality caveats | Document exists and states the limitation explicitly (HD-010) | `docs/DATA.md` | doc review | P1 | HD-010 | [ ] |
| DOC-013 | `docs/LIMITATIONS.md`: everything the system does not do, everything unverified for lack of credentials, and every known risk | Maintained continuously; reconciled against `FINAL_IMPLEMENTATION_AUDIT.md` at completion | `docs/LIMITATIONS.md` (runtime/PAPER/infrastructure facts reconciled; historical vendor/regulatory details and final implementation audit remain pending) | doc review against verified checkpoints and current implementation | P0 | — | [~] |
| DOC-014 | `IMPLEMENTATION_STATUS.md` kept current after every phase, per contract §4 | File exists and reflects the true state at every checkpoint | `IMPLEMENTATION_STATUS.md` | manual review | P0 | — | [ ] |
| DOC-015 | Inline code documentation: every public module, risk rule and strategy carries a docstring stating purpose, inputs, outputs and safety assumptions | Docstring coverage check passes on the safety-critical packages | `backend/app/` | `test_docstring_coverage` | P2 | DOC-005 | [ ] |

---

## 3. Traceability summary

The per-requirement table above **is** the traceability matrix required by contract §2: every row carries its implementation location and its test. The rolled-up view by area:

**Total: 533 requirements across 51 areas.** No requirement is optional; priority controls build order only.

| Area | Requirements | Primary implementation root | Primary test root |
|---|---|---|---|
| Architecture (`ARCH`) | 16 | `backend/app/{modes,config,core}` | `tests/unit/core/` |
| Backend (`BE`) | 14 | `backend/app/api/` | `tests/integration/api/` |
| Database (`DB`) | 20 | `backend/app/db/`, `alembic/` | `tests/integration/db/` |
| Frontend (`FE`) | 21 | `frontend/src/` | `frontend/src/**/*.test.tsx` |
| Groww integration (`GRW`) | 27 | `backend/app/brokers/groww/` | `tests/integration/groww/` |
| Authentication (`AUTH`) | 7 | `backend/app/brokers/groww/auth.py` | `tests/unit/auth/` |
| Security (`SEC`) | 11 | `backend/app/security/`, `core/logging.py` | `tests/safety/`, `tests/unit/security/` |
| Market data (`MD`) | 11 | `backend/app/marketdata/` | `tests/integration/marketdata/` |
| Historical data (`HD`) | 11 | `backend/app/marketdata/` | `tests/integration/marketdata/` |
| Exchange rules (`EXCH`) | 8 | `backend/app/core/{calendar,sessions}.py` | `tests/unit/core/` |
| Equity analysis (`EQ`) | 7 | `backend/app/analysis/` | `tests/unit/analysis/` |
| Fundamentals (`FUND`) | 8 | `backend/app/analysis/fundamental/` | `tests/unit/analysis/` |
| Technical analysis (`TA`) | 12 | `backend/app/analysis/technical/` | `tests/unit/analysis/` |
| News (`NEWS`) | 12 | `backend/app/news/` | `tests/integration/news/` |
| Sentiment (`SENT`) | 5 | `backend/app/analysis/sentiment/`, `news/sentiment.py` | `tests/unit/analysis/` |
| Regime (`REG`) | 6 | `backend/app/analysis/regime/` | `tests/unit/regime/` |
| Intraday (`INTRA`) | 7 | `backend/app/execution/` | `tests/unit/execution/` |
| Futures (`FUT`) | 6 | `backend/app/fno/` | `tests/unit/fno/` |
| Options (`OPT`) | 11 | `backend/app/fno/` | `tests/unit/fno/` |
| Greeks (`GRK`) | 8 | `backend/app/fno/greeks/` | `tests/unit/fno/` |
| Option chain (`OC`) | 8 | `backend/app/fno/chain/` | `tests/unit/fno/` |
| F&O risk (`FNOR`) | 8 | `backend/app/risk/` | `tests/unit/risk/` |
| Strategy engine (`STRAT`) | 13 | `backend/app/strategies/` | `tests/unit/strategies/` |
| LLM layer (`LLM`) | 13 | `backend/app/llm/` | `tests/unit/llm/` |
| Research agents (`AIR`) | 10 | `backend/app/agents/` | `tests/unit/agents/` |
| Decision engine (`AID`) | 10 | `backend/app/agents/` | `tests/unit/agents/`, `tests/safety/` |
| Risk engine (`RISK`) | 20 | `backend/app/risk/` | `tests/unit/risk/` |
| Position sizing (`SIZE`) | 9 | `backend/app/sizing/` | `tests/unit/sizing/` |
| Execution (`EXEC`) | 14 | `backend/app/execution/` | `tests/integration/execution/` |
| Order management (`OMS`) | 8 | `backend/app/execution/oms/` | `tests/integration/execution/` |
| Portfolio (`PORT`) | 8 | `backend/app/portfolio/` | `tests/unit/portfolio/` |
| P&L (`PNL`) | 8 | `backend/app/portfolio/` | `tests/unit/portfolio/` |
| Journal (`JRN`) | 7 | `backend/app/journal/` | `tests/integration/journal/` |
| Backtesting (`BT`) | 14 | `backend/app/backtest/` | `tests/unit/backtest/` |
| Walk-forward (`WF`) | 5 | `backend/app/backtest/` | `tests/unit/backtest/` |
| Paper trading (`PAPER`) | 8 | `backend/app/brokers/paper/` | `tests/integration/paper/` |
| Supervised (`SUP`) | 7 | `backend/app/execution/supervision.py` | `tests/integration/execution/` |
| Live (`LIVE`) | 10 | `backend/app/execution/arming.py` | `tests/safety/` |
| Notifications (`NOTIF`) | 7 | `backend/app/notifications/` | `tests/integration/notifications/` |
| Logging (`LOG`) | 5 | `backend/app/core/logging.py` | `tests/unit/core/` |
| Monitoring (`MON`) | 9 | `backend/app/monitoring/` | `tests/integration/monitoring/` |
| Error handling (`ERR`) | 7 | `backend/app/core/` | `tests/unit/core/` |
| State recovery (`REC`) | 9 | `backend/app/recovery/` | `tests/integration/recovery/` |
| Emergency (`EMG`) | 7 | `backend/app/emergency/` | `tests/safety/` |
| Audit (`AUDIT`) | 7 | `backend/app/audit/` | `tests/integration/audit/` |
| Self-learning (`LEARN`) | 9 | `backend/app/learning/` | `tests/unit/learning/` |
| Reporting (`RPT`) | 7 | `backend/app/reporting/` | `tests/integration/reporting/` |
| Compliance (`CMP`) | 7 | `backend/app/compliance/` | `tests/unit/compliance/` |
| Testing (`TEST`) | 26 | `backend/tests/` | self-verifying |
| Deployment (`DEPLOY`) | 10 | `docker/`, `.github/` | CI |
| Documentation (`DOC`) | 15 | `docs/` | doc review + doc tests |

---

## 4. Proposed build order (phases)

Each phase ends with a checkpoint written to `IMPLEMENTATION_STATUS.md`. The repository is left runnable at every checkpoint.

| Phase | Contents | Exit condition |
|---|---|---|
| **P0 — Foundations** | ARCH, config, clock, money, ids, logging, errors, DB schema + migrations, Docker Compose | `docker compose up` yields a booting API with migrations applied and health checks reporting honestly |
| **P1 — Broker & data** | Groww client + auth + all endpoints (fixture-verified), instrument master, live and historical market data, calendar/sessions, paper broker | Market data flows end-to-end; the paper broker satisfies the full interface |
| **P2 — Analysis** | Indicators, technical snapshot, regime, equity/fundamental analysis, F&O math, Greeks, option chain | All indicator and Greeks unit tests green against reference values |
| **P3 — Decision core** | Strategy engine + reference strategies, sizing, **risk engine**, proposal validation, audit trail | A signal traverses validation → risk → decision with full audit, in tests |
| **P4 — Execution** | OMS, execution service, protection, idempotency, reconciliation, recovery, emergency controls | Full order lifecycle and all four recovery classes pass |
| **P5 — PAPER mode complete** | Intraday loop, portfolio, P&L, journal, paper engine, notifications, monitoring | End-to-end §8 chain test green in PAPER mode |
| **P6 — AI layer** | LLM provider, fallback provider, agents, decision engine, grounding and containment guards | System runs identically with the LLM enabled, disabled and failing |
| **P7 — Validation** | Backtesting, walk-forward, strategy approval gates | Reference strategies produce complete backtest + OOS reports |
| **P8 — Dashboard** | Every page in §2.4 against real backend data | No placeholder UI; all frontend tests green |
| **P9 — Supervised & Live** | Approval flow, arming ceremony, live guards, compliance controls | All safety invariants in TEST-021 pass |
| **P10 — Learning & reporting** | Attribution, AI memory, recommendations, monthly reports | Monthly report generated from real stored data |
| **P11 — Completion audit** | Full requirement audit per contract §16 | `FINAL_IMPLEMENTATION_AUDIT.md` with PARTIAL = 0 and NOT IMPLEMENTED = 0 (or explicitly blocked-by-external-dependency) |

---

## 5. Amendment process

1. Requirements are added, changed or removed only with the owner's explicit approval.
2. Amendments append a dated entry to §6 below; existing IDs are never renumbered.
3. A requirement may be marked **BLOCKED** only with a named external dependency (credential, API permission, vendor, regulator) and the specific action needed to unblock it.
4. `IMPLEMENTATION_STATUS.md` is updated at every phase checkpoint; `PROJECT_REQUIREMENTS.md` status columns are updated whenever a requirement changes state.

---

## 6. Amendment log

Implementation evidence update (2026-10-02; no acceptance criteria changed):
OMS-005 and FE-008 remain partial. Authenticated PAPER entry cancellation now
uses `backend/app/api/orders.py`, `backend/app/execution/owner_cancel.py` and the
existing supervisor recovery. `frontend/src/OrderControls.tsx` exposes it in the
Orders view. Evidence: `test_owner_cancel.py`, `test_order_hygiene.py`,
`OrderControls.test.tsx`, and real-browser `test_real_browser_cancels_partial_entry`.
Bulk PAPER-entry cancellation now uses `execution/bulk_cancel.py` with atomic
parent/child intents, restart recovery and per-order outcomes, verified by
`test_bulk_cancel.py` and the single/bulk real-browser cases. General cancellation
and risk-revalidated modification remain pending; protective exit cancellation
and real-broker acceptance are not claimed.

| Date | Change | Approved by |
|---|---|---|
| 2026-09-17 | Initial derivation from the execution contract (v0.1, pre-approval) | pending |

