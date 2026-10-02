# LIMITATIONS.md

**Requirement:** DOC-013
**Last updated:** 2026-09-24 (integrated journal and historical evidence)

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
eligibility evidence, PostgreSQL operation, Groww LIVE and real notification
delivery remain externally unverified. Missing credentials, capital or
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

The Groww adapter is complete and tested against 23 contract fixtures through the
real client code. **No call has ever been made to the live API**, because no
credentials exist yet. The following are therefore unproven:

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
| G8 | **No fundamentals API** | `FUND-*` needs an external vendor (blocked dependency D5) |
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
| **Paper fills carry no transaction costs** | Paper P&L is **gross**. Intraday and F&O edges are frequently smaller than costs, so gross paper results overstate viability | P5 (`PNL-003`); the paper broker logs this warning on every connect |
| **Holiday data is incomplete** | Only fixed-date national holidays are shipped. Lunar-calendar holidays (Holi, Diwali, Eid…) are missing, so the system may believe a closed market is open | Owner action: populate `backend/data/holidays.json` from the NSE circular. The calendar reports the gap and the market-status check degrades until it is filled |
| **No live-verified order path** | Idempotency, timeout reconciliation and rejection handling are tested against fixtures and the paper broker, not against Groww | Blocked on credentials (D1) |
| **Docker stack unbuilt on this machine** | Compose, Dockerfile and entrypoint are written but have never been run — Docker is not installed on the development host | Owner action: run `docker compose up` on a machine with Docker |
| **PostgreSQL-specific guarantees untested** | TimescaleDB hypertables, append-only audit triggers and the cross-row fill-sum trigger are written but exercised only on a real PostgreSQL server | Set `ATS_TEST_POSTGRES_URL` and run the skipped tests |

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
* **No Docker**, so every container path is unbuilt (see §5).
* **No PostgreSQL**, so the suite runs on SQLite and three PostgreSQL-specific
  tests skip.
* **No Redis**, so the in-process lock and cache backends are used. These are
  correct for a single process and are *refused* in any mode that reaches a real
  broker.

---

## 8. What would make this document shorter

In order of value:

1. Groww credentials (removes most of §2).
2. The NSE holiday circular (removes the calendar gap in §5).
3. A machine with Docker and PostgreSQL (removes §5 rows 4–5 and §7).
4. Groww/SEBI algo onboarding (removes the LIVE gate in §6).
