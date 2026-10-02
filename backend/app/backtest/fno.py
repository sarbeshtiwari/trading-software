"""Reject option runs without recorded point-in-time chain history; never approximate it."""

from app.core.enums import InstrumentType


def require_chain_history(manifest, recording):
    selected = next(
        item for item in manifest.instruments if item.id == manifest.strategy_instrument_id
    )
    if selected.instrument_type != InstrumentType.OPTION:
        return
    chains = [
        row
        for row in recording.snapshots
        if row.kind == "CHAIN"
        and row.value.underlying == selected.underlying
        and row.value.expiry == selected.expiry_date
        and row.available_at <= manifest.start_at
    ]
    if not chains:
        raise ValueError("OPTION_CHAIN_HISTORY_UNAVAILABLE: matching chain required at run start")
