"""Provider registration.

Registration is explicit rather than implicit-on-package-import: import side
effects that wire up a trading system are hard to reason about, and a provider
that registers itself merely because something imported a sibling module is how
the wrong broker ends up selected.

Called once during startup, and by tests that need the real providers.
"""

from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger("brokers.registry")

__all__ = ["register_all"]

_registered = False


def register_all(*, force: bool = False) -> None:
    """Import every provider module so it registers with the factories."""
    global _registered
    if _registered and not force:
        return

    from app.brokers.groww import provider as groww_provider
    from app.brokers.paper import provider as paper_provider

    groww_provider.register()
    paper_provider.register()

    _registered = True
    logger.debug("Broker providers registered")
