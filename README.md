# ATS — AI-Assisted Indian Equity & F&O Trading System

A single-owner, self-hosted algorithmic trading system for NSE/BSE (CASH and FNO),
executing through the **Groww TradeAPI**, with Claude-backed research agents whose
output is always subordinate to a deterministic risk engine.

> **Status: under construction.** P0/P1 are implemented; P2 analysis is in progress.
> Technical indicators, Greeks and option-chain analytics are tested. The full
> trading pipeline is not implemented yet. See [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md)
> for exactly what is built, and [`PROJECT_REQUIREMENTS.md`](PROJECT_REQUIREMENTS.md)
> for the 533 requirements this project is contracted to deliver.

## What this is

* A **deterministic risk engine** that every order must pass, which no AI component
  can bypass, widen or disable.
* Three strictly separated modes — `PAPER` (default), `SUPERVISED` (human approves
  every order), `LIVE` (autonomous within limits, and only after an explicit arming
  ceremony).
* A full audit trail that answers both *"why did you take this trade?"* and
  *"why did you **not** take this trade?"*.

## What this is not

* **Not investment advice**, and not a system for managing anyone else's money.
  Single account, single owner, by design and by code (`CMP-004`).
* **Not a guarantee of profit.** The reference strategies are candidates to be
  validated, not recommendations. Backtest and paper results are reported as
  measured, never assumed.
* **Not ready to trade live.** LIVE remains blocked until credentials exist, the
  Groww/SEBI algo-registration obligations are met, reconciliation succeeds, paper
  validation thresholds are met, and the owner completes the arming ceremony.

## Safety model

```
Market data ─┬─> Quantitative analysis ─┐
News/research ┘                          ├─> Claude agents ─> Trade proposal (JSON)
Fundamentals ─────────────────────────────┘                        │
                                                                   v
                                          Deterministic validation (schema, bounds)
                                                                   │
                                                                   v
                                    Deterministic risk engine ──REJECT──> NO TRADE (logged)
                                                                   │ APPROVE
                                                                   v
                                        Execution ─> Broker (Groww / paper)
```

The LLM proposes. It never sizes (the deterministic sizer recomputes every
quantity), never places an order, and never touches a risk limit.

## Requirements

* Python 3.11 (3.10 also runs the test suite)
* PostgreSQL 16 with TimescaleDB, and Redis 7 — both provided by Docker Compose
* A Groww trading-API subscription (for anything beyond `PAPER`)

## Quick start (PAPER)

For the authenticated React workspace, owner credential setup, and API contract,
see [CONTROL_PLANE.md](docs/CONTROL_PLANE.md). The dashboard reads persisted system
state; it does not yet make the entire PAPER trading loop operational.

```bash
cp .env.example .env          # then set STARTING_CAPITAL — it has no default
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Linux/macOS: .venv/bin/pip
.venv/Scripts/pytest tests                 # See IMPLEMENTATION_STATUS.md for latest results
```

With Docker:

```bash
cd docker
docker compose up --build
curl http://127.0.0.1:8000/api/v1/health/ready
```

The API refuses to report itself healthy until every critical component passes.
`STARTING_CAPITAL` is deliberately unset in the example configuration: position
sizing, the daily loss limit and the drawdown limit are all expressed against it,
so the system fails its `risk_config` health check rather than invent a number.

## Layout

```
backend/app/core/        clock, money, ids, logging, errors, events, locks
backend/app/db/          models + Alembic migrations
backend/app/brokers/     Groww adapter and paper broker (live Groww unverified)
backend/app/fno/         Greeks and option-chain analytics
backend/app/analysis/    technical indicators and snapshots
backend/app/marketdata/  MarketDataProvider interface, factory
backend/app/monitoring/  health checks, trading gate
backend/tests/           unit / integration / e2e / safety
docker/                  Compose stack, Dockerfile, entrypoint
```

## Documentation

| Document | Contents |
|---|---|
| `PROJECT_REQUIREMENTS.md` | All 533 requirements with acceptance criteria and tests |
| `REQUIREMENTS_AUDIT.md` | Ambiguities, conflicts, Groww API limits, SEBI constraints, blocked dependencies |
| `IMPLEMENTATION_STATUS.md` | The real state of the build, updated at every phase |

## Licence and responsibility

Private project. Trading carries risk of loss. The account holder is responsible
for all regulatory obligations, including any algo registration their broker
requires before automated order flow is permitted.
