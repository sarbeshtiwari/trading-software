# Market regime analysis

The classifier produces the existing `MarketRegime` labels, plus `UNKNOWN` for
missing/stale inputs. It is deterministic and does not consult an LLM.

Inputs retain numeric values, observation time, availability time and source:

- ADX: 0–100, computed by the existing Wilder implementation.
- MA structure: +1 for close > fast SMA > slow SMA; -1 for the reverse; 0 otherwise.
- Realised and implied volatility: annualised percentage points, bounded to
  0–10000; impossible/out-of-domain inputs are rejected, not clipped.
- Breadth: fraction in [0, 1], supplied with source and timestamp.
- Event risk: 0/1 and event ids from an explicitly covered, sourced calendar.

Indicator periods, annualisation, bar interval, regime thresholds, freshness,
confirmation count and maximum ordinary changes per IST session are explicit
policy inputs, recorded in decisions. No universal calibration or profitability
is implied. The indicator builder rejects future/open/unordered/malformed bars;
insufficient history stays unavailable. IV and breadth providers are not invented.

Classification precedence is event risk, high volatility, missing/stale inputs,
low volatility, ADX/MA/breadth-confirmed up/down trends, then ranging. High-impact
events use configured inclusive windows and may cover all underlyings or a named
subset. An uncovered calendar is unavailable, not evidence of no events. Future
scheduled events may be used only when the calendar was already known at the
decision time. Automatic external event ingestion is not implemented here.

Ordinary transitions require consecutive candidate confirmations and respect a
per-session change cap. Event risk, high volatility and unknown state override
that cap immediately; safety deterioration must never wait for hysteresis.
Recovery uses normal confirmation. A new IST date starts a new session counter.

`RegimeStore` persists inputs, policy, label, candidate, reason, pending candidate,
confirmation count and session counter. Restart tests verify hysteresis continuity.
Idempotent repeats return the original id; conflicting or out-of-order decisions
fail. Historical reads filter decision time and receipt time, with provenance
separated. The service follows single-writer ownership; database-level append-only
triggers for this new table are not claimed. Migration 0003 adds only the new table.
SQLite upgrade/downgrade/upgrade and schema equivalence are tested; PostgreSQL
runtime verification remains unavailable.

REG-004 remains pending: no strategy engine exists to enforce regime permissions.
REG-005 is partial: history and exact record ids exist, but journal integration
must preserve the entry record id when the journal service is built. REG-006 is
partial: event-driven classification exists, but affected-strategy suppression
depends on REG-004. These integration requirements must not be marked complete
on the strength of classifier tests alone.
