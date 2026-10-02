"""Versioned mention attribution, not event verification or trading eligibility."""

import json
import re
from hashlib import sha256
from typing import Literal

import sqlalchemy as sa
from pydantic import AwareDatetime, BaseModel

from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.audit.snapshots import freeze_snapshot
from app.core.clock import UTC, get_clock
from app.core.enums import InstrumentType
from app.db.models.audit import AuditEvent
from app.db.models.instrument import Instrument
from app.modes import TradingMode


class Mention(BaseModel):
    alias: str
    rule: Literal["QUALIFIED_SYMBOL", "CATALOG_ISIN", "FULL_NAME", "BARE_SYMBOL"]
    confidence: str
    instrument_ids: list[str]
    anchor: str
    start: int
    end: int
    reason: Literal["AMBIGUOUS", "BELOW_THRESHOLD"] | None = None


class Attribution(BaseModel):
    version: Literal[1]
    article_id: str
    article_audit_id: str
    catalog_chain: str
    catalog_event_id: str
    matches: list[Mention]
    excluded: list[Mention]
    confidence_kind: Literal["RULE_SCORE_NOT_PROBABILITY"]
    minimum_confidence: Literal["0.9"]
    meaning: Literal["MENTION_ONLY_NOT_TRADABILITY"]


class AttributionView(BaseModel):
    event_id: str
    known_at: AwareDatetime
    attribution: Attribution


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def entity_chain(article_id):
    return "news-entity-" + sha256(article_id.encode()).hexdigest()[:28]


def mention_matches(text, catalog):
    aliases = {}
    for instrument in catalog:
        symbol = instrument["symbol"]
        choices = [(f"{instrument['exchange']}:{symbol}", "QUALIFIED_SYMBOL", "1.0")]
        if instrument["isin"] and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", instrument["isin"]):
            choices.append((instrument["isin"], "CATALOG_ISIN", "1.0"))
        name = instrument["name"]
        if name and len(name.split()) >= 2 and len(name) >= 10:
            choices.append((name, "FULL_NAME", "0.95"))
        if len(symbol) >= 2:
            choices.append((symbol, "BARE_SYMBOL", "0.5"))
        for alias, rule, confidence in choices:
            aliases.setdefault((alias.casefold(), rule, confidence), []).append(instrument["id"])
    matches, excluded = [], []
    for (alias, rule, confidence), instruments in sorted(aliases.items()):
        pattern = re.escape(alias).replace(r"\ ", r"\s+")
        if rule == "QUALIFIED_SYMBOL":
            pattern = pattern.replace(":", r"\s*:\s*")
        mention = re.search(r"(?<!\w)" + pattern + r"(?!\w)", text, re.IGNORECASE)
        if mention is None:
            continue
        detail = {
            "alias": alias,
            "rule": rule,
            "confidence": confidence,
            "instrument_ids": sorted(set(instruments)),
            "anchor": mention.group(),
            "start": mention.start(),
            "end": mention.end(),
        }
        if len(set(instruments)) != 1 or rule == "BARE_SYMBOL":
            excluded.append(
                {
                    **detail,
                    "reason": "AMBIGUOUS" if len(set(instruments)) != 1 else "BELOW_THRESHOLD",
                }
            )
        else:
            matches.append(detail)
    return {
        "matches": matches,
        "excluded": excluded,
        "confidence_kind": "RULE_SCORE_NOT_PROBABILITY",
        "minimum_confidence": "0.9",
        "meaning": "MENTION_ONLY_NOT_TRADABILITY",
    }


async def history(session, chain, as_of=None):
    query = sa.select(AuditEvent).where(AuditEvent.chain_id == chain)
    if as_of is not None:
        query = query.where(AuditEvent.occurred_at <= as_of.astimezone(UTC))
    records = list((await session.scalars(query.order_by(AuditEvent.sequence))).all())
    if not verify_records(records):
        raise ValueError("Entity evidence audit unavailable")
    return records


async def attribute(session, article_id, article_audit_id, title, body, *, actor, clock=None):
    clock = clock or get_clock()
    cutoff = clock.utcnow()
    records = await history(session, entity_chain(article_id))
    if records and aware(records[-1].occurred_at) > cutoff:
        raise ValueError("Entity attribution clock regression")
    rows = list(
        (
            await session.scalars(
                sa.select(Instrument)
                .where(
                    Instrument.instrument_type.in_([InstrumentType.EQUITY, InstrumentType.INDEX]),
                    Instrument.created_at <= cutoff,
                    sa.or_(Instrument.updated_at.is_(None), Instrument.updated_at <= cutoff),
                )
                .order_by(Instrument.id)
                .limit(25001)
            )
        ).all()
    )
    if len(rows) > 25000:
        raise ValueError("Entity catalog scope exceeded")
    catalog = freeze_snapshot(
        [
            {
                "id": row.id,
                "exchange": row.exchange.value,
                "symbol": row.trading_symbol,
                "isin": row.isin,
                "name": row.name,
                "source": row.source,
                "known_at": (row.updated_at or row.created_at).replace(tzinfo=UTC)
                if (row.updated_at or row.created_at).tzinfo is None
                else row.updated_at or row.created_at,
            }
            for row in rows
        ]
    )
    catalog_id = "news-catalog-" + digest(catalog)[:27]
    previous_catalog = await history(session, catalog_id)
    if previous_catalog:
        if (
            len(previous_catalog) != 1
            or previous_catalog[0].result != {"instruments": catalog}
            or previous_catalog[0].event_type != "NEWS_ENTITY_CATALOG"
            or aware(previous_catalog[0].occurred_at) > cutoff
        ):
            raise ValueError("Entity catalog identity mismatch")
        catalog_event = previous_catalog[0]
    else:
        catalog_event = await AuditService(clock).append_in_session(
            session,
            AuditIdentity(
                chain_id=catalog_id,
                event_type="NEWS_ENTITY_CATALOG",
                actor=actor,
                mode=TradingMode.PAPER,
            ),
            {"result": {"instruments": catalog}},
            expected_count=0,
        )
    result = {
        "version": 1,
        "article_id": article_id,
        "article_audit_id": article_audit_id,
        "catalog_chain": catalog_id,
        "catalog_event_id": catalog_event.id,
        **mention_matches(title + "\n" + body, catalog),
    }
    if records and records[-1].result == result:
        return records[-1]
    event = await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=entity_chain(article_id),
            event_type="NEWS_ENTITY_ATTRIBUTION",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {"result": result},
        expected_count=len(records),
    )
    if aware(event.occurred_at) < max(cutoff, aware(catalog_event.occurred_at)):
        raise ValueError("Entity attribution clock regression")
    return event


async def read_attribution(session, article_id, article_audit_id, *, as_of):
    records = await history(session, entity_chain(article_id), as_of)
    if not records:
        return None
    event = records[-1]
    result = event.result
    if (
        event.event_type != "NEWS_ENTITY_ATTRIBUTION"
        or result["article_id"] != article_id
        or result["article_audit_id"] != article_audit_id
    ):
        raise ValueError("Entity attribution identity mismatch")
    catalog = await history(session, result["catalog_chain"], as_of)
    if (
        len(catalog) != 1
        or catalog[0].id != result["catalog_event_id"]
        or catalog[0].event_type != "NEWS_ENTITY_CATALOG"
        or aware(catalog[0].occurred_at) > aware(event.occurred_at)
        or "news-catalog-" + digest(catalog[0].result["instruments"])[:27]
        != result["catalog_chain"]
    ):
        raise ValueError("Entity catalog unavailable at decision time")
    return AttributionView(event_id=event.id, known_at=aware(event.occurred_at), attribution=result)
