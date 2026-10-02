"""Read-only authoritative configuration lookup shared by decision and execution."""

import sqlalchemy as sa

from app.db.models.config import RiskConfigVersion
from app.risk.config import RiskLimits


async def active_limits(session):
    rows = list((await session.scalars(sa.select(RiskConfigVersion).where(
        RiskConfigVersion.is_active.is_(True)
    ))).all())
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("multiple active risk configurations")
    return RiskLimits.model_validate(rows[0].extra_limits["engine_limits"])
