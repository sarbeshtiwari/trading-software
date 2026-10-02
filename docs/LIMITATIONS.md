# LIMITATIONS.md

**Requirement:** DOC-013
**Last updated:** 2026-10-02 (runtime infrastructure and PAPER acceptance)

Everything this system does **not** do, everything that is built but unverified,
and every known risk. This document is maintained continuously and is reconciled
against `FINAL_IMPLEMENTATION_AUDIT.md` at completion.

The latest checkpoint in `IMPLEMENTATION_STATUS.md` and scoped verification in
`PAPER_WORKER.md`, `PAPER_COSTS.md`, `PAPER_FIFO.md` and `PAPER_EMERGENCY.md` record
current implementation evidence. Later sections retain historical P1 external
integration limitations; they are not proof that later software is absent.

---

## 1. Not yet built

The repository now contains analysis, a reference strategy, the shared
validation/sizing/risk pipeline, durable PAPER execution, FIFO/cost estimates,
journals, owner emergency/review controls and eleven API-backed dashboard views.
Provider-driven integration fixtures verify a trade through entry, monitoring,
exit, accounting and API state. This is not the full M1/M2 acceptance contract.
Recorded-data historical execution and walk-forward/OOS reports now use the shared
PAPER worker in isolated processes, with authenticated API/UI inspection and
evidence review. Journal queries, exports, annotations and append-only contextual
versions are integrated. Consult `HISTORICAL_RUNS.md`, `WALK_FORWARD.md`,
`PAPER_EVIDENCE.md` and `JOURNAL.md` for their exact bounds; none establish strategy
profitability or LIVE permission.

Complete external source coverage, protection certification, notification
coverage, broader cross-broker execution/portfolio recovery and the remaining
validation/AI/supervised layers remain incomplete. Actual time-based PAPER
eligibility evidence, Groww LIVE and real notification delivery remain
externally unverified. Actual PostgreSQL migration and safety checks have passed;
they do not certify every deployment/recovery requirement. Missing capital or
calendar/evidence must never be replaced by invented values.

At the P1 checkpoint these startup checks were absent. Consult the current
authenticated health/monitoring API for actual readiness rather than inferring
that an earlier table or a running HTTP server means trading is enabled:

| Missing check | Arrives in |
|---|---|
| `news` | P2 (news ingestion) |
| `order_service` | P4 (execution) |

---

## 2. Built but unverified against the live broker

The Groww adapter is tested against isolated contract fixtures. On 2026-10-02,
owner-configured credentials successfully minted a real token through SDK 1.5.0.
A read-only NIFTY LTP request failed with HTTP 403; its exact broker/network cause
is unresolved. No order was created, modified, cancelled or executed. Token
authentication is now externally observed, but live data, execution and account
reconciliation remain unverified. The following historical adapter limitations
must still be reviewed against current documentation and actual responses:

### 2.1 Endpoint paths that are inferred, not documented

Groww publishes the base URL, the response envelope, the error codes, the rate
limits, the enums and a handful of paths. The rest are inferred from the SDK
reference. Each of these logs a warning the first time it is used, and each must
be confirmed against a real account before LIVE trading:

| Path | Note |
|---|---|
| `/order/modify` | path not published in the REST reference |
| `/order/cancel` | path not published in the REST reference |
| `/order/list` | path not published in the REST reference |
| `/order/status/{groww_order_id}` | path not published in the REST reference |
| `/order/status/reference/{order_reference_id}` | The operation is documented (`get_order_status_by_reference`); the path is not |
| `/order/trades/{groww_order_id}` | path not published in the REST reference |
| `/positions/user`, `/positions/symbol` | path not published in the REST reference |
| `/holdings/user` | path not published in the REST reference |
| `/margins/detail/user`, `/margins/detail/orders` | path not published in the REST reference |
| `/live-data/quote`, `/live-data/ltp`, `/live-data/ohlc` | path not published in the REST reference |
| `/live-data/option-chain`, `/live-data/greeks` | path not published in the REST reference |
| `/historical/candle/range` | path not published in the REST reference |
| `/instruments`, instrument CSV URL | path not published in the REST reference |

**Documented and therefore trusted:** `POST /token/api/access`,
`POST /order/create`, `GET /order/detail/{groww_order_id}`.

### 2.2 Request bodies that are inferred

* **Token exchange over REST.** The endpoint path is documented; its request body
  field names are not. The SDK path (`growwapi.GrowwAPI.get_access_token`) is the
  default precisely because it cannot drift from the documented call. The direct
  REST exchange logs a warning whenever it is used.
* **Websocket feed message shapes.** The SDK method names are documented; the
  payload structure is not. The normalisation in `app/marketdata/live.py` is a
  best reading and will need adjusting against a live feed.

### 2.3 Response field names

Parsers accept the documented field names plus the common snake/camel variants.
Where a field is absent the value is `None`, never `0` — but a field arriving
under a *third* spelling would read as absent. The first live session should be
run with `LOG_LEVEL=DEBUG` and the raw payloads inspected.

---

## 3. Verified Groww API limitations (properties of the broker, not the build)

| # | Limitation | Consequence |
|---|---|---|
| G1 | Intraday history is **3 months**; daily/weekly is full | Intraday backtests are data-poor until the local archive fills. Every intraday backtest report must state its data window (BT-014) |
| G2 | Validity is **`DAY` only** — no IOC, no GTT | A stop cannot be parked at the broker overnight. Protection is re-placed each session and monitored locally |
| G3 | **No bracket or cover orders** | Entry and stop are separate orders, so there is a window where a position is unprotected. `EXEC-003` escalates if the stop cannot be placed |
| G4 | Order docs list **NSE**; BSE is unconfirmed on the order path | BSE is refused locally until a live account confirms it. MCX/commodities are out of scope |
| G5 | API key + secret flow needs **daily approval**, token endpoint capped at **150/24h** | TOTP is the default flow; a budget guard blocks the 151st daily token request |
| G6 | Order rate limits **10/s, 250/min** | Bursty exits are paced by the local limiter; a separate compliance governor keeps orders below the SEBI threshold |
| G7 | LTP/OHLC batches cap at **50**; feed caps at **1000 subscriptions** | Universe size is bounded by the data budget; subscriptions are priority-evicted |
| G8 | **No fundamentals API** | Point-in-time manual CSV/JSON fundamentals loading exists; source data must be supplied, never invented |
| G9 | **No corporate-actions API** | Splits and bonuses need an external or manual source; discontinuities are flagged, never silently adjusted |
| G10 | Margin/SPAN exposure is unconfirmed | F&O margin may have to be estimated locally; estimates are tagged `ESTIMATED` and carry a safety multiplier |
| G11 | Historical candles carry **no bid/ask** | Backtest slippage is a modelled assumption, not measured spread |
| G12 | Option-chain Greeks: source, model and refresh cadence undocumented | Broker and locally computed Greeks are both kept, labelled by source, and divergence raises a data-quality event |

---

## 4. Things this build deliberately does not do

* **Naked short options** are disabled and require an explicit flag plus a hard
  risk cap. Unbounded loss on a retail account is not a default.
* **Averaging down / martingale sizing** is not implemented at all.
* **Third-party funds or accounts.** Single account, single owner, enforced in
  code (`CMP-004`).
* **Investment advice.** The system produces no recommendations for anyone else.
* **Auto-enabling anything.** Learning may auto-*disable* a degrading strategy; it
  can never auto-enable one, widen a limit, or apply a parameter change.

---

## 5. Known gaps in the current build

| Gap | Effect | Scheduled |
|---|---|---|
| **Cost coverage is partial** | CASH/MIS and single-long-option PAPER paths compute configured cost estimates and distinguish gross, charges and net P&L. Missing tariffs remain unavailable; estimates are not verified broker bills | Complete remaining segment/product coverage and external billing validation; see `PAPER_COSTS.md` and `PAPER_OPTIONS.md` |
| **Exchange calendar coverage is incomplete** | Source-grounded NSE annual/amendment dates are loaded, but full exchange/year coverage and some special-session hours remain unverified. Incomplete or unknown sessions block affected new entries, rather than guessing hours | Complete source-grounded coverage; see the latest calendar checkpoints in `../IMPLEMENTATION_STATUS.md` |
| **No live-verified order path** | Idempotency, timeout reconciliation and rejection handling are tested against fixtures and the PAPER broker, not actual Groww execution. Configured authentication succeeds, but read-only LTP returns HTTP 403 | Resolve the externally observed data-access failure and complete authorized validation; no real orders or LIVE arming |
| **Full deployment acceptance is incomplete** | Existing Docker PostgreSQL/TimescaleDB and Redis are running and real connections were verified. This does not establish complete containerized application, backup or disaster-recovery acceptance | Verify remaining deployment/recovery requirements using existing infrastructure |
| **Database safety has scoped verification** | Actual PostgreSQL fresh migrations, schema-drift detection, audit protections and fill constraints have passing evidence. SQLite remains the default isolated test database | Retain real PostgreSQL tests and complete broader operational acceptance; do not describe all database behavior as unverified |

---

## 6. Regulatory constraints that block LIVE

| # | Constraint | Status |
|---|---|---|
| R1 | SEBI's retail-algo framework governs algo orders placed through broker APIs | Owner must confirm Groww's requirements |
| R2 | **Every algo order must carry an exchange-assigned Algo-ID** (in force since 1 April 2026) | Tagging plumbing is built (`CMP-002`); the identifier itself must come from Groww. **Hard gate on LIVE** |
| R3 | Orders above the retail threshold (initially 10/second) require registration | The compliance governor paces below a configurable threshold and cannot be disabled in LIVE |
| R5 | Registration duty sits with the account holder, not with software | Owner action |

---

## 7. Environment constraints on the development host

* **Python 3.10**, not 3.11. The code is written to run on both; containers pin
  3.11. Nothing 3.11-only is used.
* **Docker PostgreSQL/TimescaleDB and Redis exist and run locally.** Application
  `SELECT 1` and Redis `PING` were verified against those existing services.
* The normal isolated suite uses SQLite. PostgreSQL-specific acceptance is
  opt-in via `ATS_TEST_POSTGRES_URL` and has been exercised on the actual server.
  Skip counts depend on the selected environment, not a permanent missing server.
* In-process fallbacks are not proof of distributed readiness. Broader deployment
  and failure/recovery requirements remain incomplete despite running services.

---

## 8. What would make this document shorter

In order of value:

1. Resolve the observed Groww read-only HTTP 403 and verify permitted market data.
2. Complete source-grounded exchange calendars and announced session hours.
3. Complete runtime/deployment/recovery acceptance on the existing infrastructure.
4. Complete required broker/compliance onboarding and genuine PAPER evidence;
   neither credentials nor passing fixture tests remove the LIVE gates.
