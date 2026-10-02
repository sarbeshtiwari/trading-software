"""Disclosure of declared research inputs, never certification of historical eligibility."""

from typing import Literal

from pydantic import AwareDatetime

from app.analysis.equity import EvidenceModel

SURVIVORSHIP_NOTE = (
    "Owner-declared static instrument lists, not a verified historical universe. "
    "Declared knowledge timestamps are checked against each window start; listing, "
    "delisting and universe completeness are externally unverified. "
    "Survivorship and selection bias remain possible."
)

UNIVERSE_WARNING = (
    "Historical listing/delisting eligibility and universe completeness are externally "
    "unverified. Survivorship and selection bias remain possible. Missing construction "
    "metadata is UNAVAILABLE, not evidence of a historically complete universe."
)


class UniverseInstrument(EvidenceModel):
    id: str
    trading_symbol: str
    instrument_type: str
    source: str
    known_at: AwareDatetime
    is_active: bool
    is_restricted: bool
    role: Literal["STRATEGY_INSTRUMENT", "CONTEXT_ONLY"]


class UniverseWindow(EvidenceModel):
    source_run_id: str
    source: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    instruments: tuple[UniverseInstrument, ...]


class UniverseDisclosure(EvidenceModel):
    version: Literal[1] = 1
    method: Literal["OWNER_DECLARED_STATIC_LISTS"] = "OWNER_DECLARED_STATIC_LISTS"
    historical_eligibility_verified: Literal[False] = False
    completeness_verified: Literal[False] = False
    limitation: str = SURVIVORSHIP_NOTE
    windows: tuple[UniverseWindow, ...]


def disclose_universe(manifests):
    windows = []
    for manifest in manifests:
        instruments = []
        for item in manifest["instruments"]:
            fields = {key: item[key] for key in UniverseInstrument.model_fields if key != "role"}
            instruments.append(
                UniverseInstrument(
                    **fields,
                    role="STRATEGY_INSTRUMENT"
                    if item["id"] == manifest["strategy_instrument_id"]
                    else "CONTEXT_ONLY",
                )
            )
        windows.append(
            UniverseWindow(
                source_run_id=manifest["run_id"],
                source=manifest["source"],
                start_at=manifest["start_at"],
                end_at=manifest["end_at"],
                instruments=tuple(instruments),
            )
        )
    return UniverseDisclosure(windows=tuple(windows)).model_dump(mode="json")
