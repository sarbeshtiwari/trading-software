"""Explicit synthetic publisher configuration; never production source defaults."""

from app.core.clock import UTC
from app.db.models.news import NewsSource
from tests.unit.test_option_chain import OBSERVED


def source(session, *, slug="test-exchange", publisher="fixture-exchange", tier=1, kind="filings"):
    timestamp = OBSERVED.astimezone(UTC)
    row = NewsSource(
        id="source-" + slug,
        slug=slug,
        publisher_id=publisher,
        tier=tier,
        kind=kind,
        name="Isolated source fixture",
        endpoint=f"https://{slug}.example.test/feed",
        is_enabled=True,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(row)
    return {
        "slug": slug,
        "url": f"https://{slug}.example.test/story",
        "fetched_at": timestamp.isoformat(),
    }
