"""Owner-authored source policies with optimistic, transactional audit versions."""

from decimal import Decimal
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit

import sqlalchemy as sa
from pydantic import Field, model_validator

from app.analysis.equity import EvidenceModel
from app.audit.integrity import verify_records
from app.audit.service import AuditIdentity, AuditService
from app.core.clock import UTC, get_clock
from app.core.ids import new_id
from app.db.models.audit import AuditEvent
from app.db.models.news import NewsSource
from app.modes import TradingMode


class SourcePolicy(EvidenceModel):
    name: str = Field(min_length=1, max_length=128)
    publisher_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    tier: int = Field(ge=1, le=4, strict=True)
    kind: Literal["rss", "api", "filings", "regulator"]
    endpoint: str = Field(min_length=1, max_length=512)
    weight: Decimal = Field(ge=0, le=1)
    is_enabled: bool = False

    @model_validator(mode="after")
    def valid_configuration(self):
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except ValueError:
            raise ValueError("Invalid source endpoint") from None
        if (
            not self.name.strip()
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or port not in (None, 443)
            or any(ord(character) <= 32 for character in self.endpoint)
            or (self.tier == 1 and self.kind not in {"filings", "regulator"})
            or (self.kind in {"filings", "regulator"} and self.tier != 1)
        ):
            raise ValueError("HTTPS configuration and consistent primary-source tier required")
        return self


class SourceView(EvidenceModel):
    slug: str
    configuration: SourcePolicy | None
    event_id: str | None
    integrity: Literal["AUDITED", "UNMANAGED", "INVALID"]
    acquisition_status: Literal["SUPPORTED_ADAPTER", "UNAVAILABLE"] = "UNAVAILABLE"


def chain_id(slug):
    return "news-" + sha256(slug.encode()).hexdigest()[:35]


def snapshot(row):
    return SourcePolicy.model_validate(
        {name: getattr(row, name) for name in SourcePolicy.model_fields}
    )


async def records(session, slug):
    return list(
        (
            await session.scalars(
                sa.select(AuditEvent)
                .where(
                    AuditEvent.chain_id == chain_id(slug),
                )
                .order_by(AuditEvent.sequence)
                .with_for_update()
            )
        ).all()
    )


async def describe(session, row):
    history = await records(session, row.slug)
    try:
        policy = snapshot(row)
        valid = (
            bool(history)
            and verify_records(history)
            and history[-1].event_type == "NEWS_SOURCE_CONFIGURATION"
            and SourcePolicy.model_validate(history[-1].result["configuration"]) == policy
        )
    except (ValueError, TypeError, KeyError):
        policy, valid = None, False
    return SourceView(
        slug=row.slug,
        configuration=policy if valid else None,
        event_id=history[-1].id if valid else None,
        integrity="AUDITED" if valid else "INVALID" if history else "UNMANAGED",
        acquisition_status="SUPPORTED_ADAPTER"
        if valid and policy.kind in {"rss", "api"}
        else "UNAVAILABLE",
    )


async def configure(session, slug, policy, *, expected_event_id, actor, reason, clock=None):
    policy = SourcePolicy.model_validate(policy.model_dump())
    clock = clock or get_clock()
    row = await session.scalar(
        sa.select(NewsSource).where(NewsSource.slug == slug).with_for_update()
    )
    history = await records(session, slug)
    if not verify_records(history) or (history[-1].id if history else None) != expected_event_id:
        raise ValueError("Source version changed or audit unavailable")
    if history:
        if row is None or (await describe(session, row)).integrity != "AUDITED":
            raise ValueError("Source configuration integrity failure")
        occurred = history[-1].occurred_at
        occurred = occurred.replace(tzinfo=UTC) if occurred.tzinfo is None else occurred
        if occurred > clock.utcnow():
            raise ValueError("Source configuration clock regression")
    if row is None:
        row = NewsSource(id=new_id("nsr"), slug=slug)
        session.add(row)
    for name, value in policy.model_dump().items():
        setattr(row, name, value)
    row.updated_at = clock.utcnow()
    event = await AuditService(clock).append_in_session(
        session,
        AuditIdentity(
            chain_id=chain_id(slug),
            event_type="NEWS_SOURCE_CONFIGURATION",
            actor=actor,
            mode=TradingMode.PAPER,
        ),
        {
            "result": {
                "slug": slug,
                "configuration": policy.model_dump(mode="json"),
                "reason": reason,
            }
        },
        expected_count=len(history),
    )
    return SourceView(
        slug=slug,
        configuration=policy,
        event_id=event.id,
        integrity="AUDITED",
        acquisition_status="SUPPORTED_ADAPTER" if policy.kind in {"rss", "api"} else "UNAVAILABLE",
    )
