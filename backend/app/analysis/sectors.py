"""EQ-004: sector classification comes from dated evidence, never guessed symbols."""

from datetime import datetime

from app.analysis.equity import EquitySnapshot, EvidenceModel
from app.fno.chain.model import aware


class SectorMapping(EvidenceModel):
    mapped: dict[str, tuple[str, str | None]]
    unmapped: tuple[str, ...]


def sector_mapping(snapshots: list[EquitySnapshot], *, as_of: datetime) -> SectorMapping:
    aware(as_of)
    mapped, unmapped = {}, []
    for snapshot in snapshots:
        if snapshot.known_at > as_of:
            raise ValueError("future sector knowledge")
        if snapshot.key in mapped or snapshot.key in unmapped:
            raise ValueError("duplicate instrument")
        if snapshot.sector is None:
            unmapped.append(snapshot.key)
        else:
            mapped[snapshot.key] = (snapshot.sector, snapshot.industry)
    return SectorMapping(mapped=dict(sorted(mapped.items())), unmapped=tuple(sorted(unmapped)))
