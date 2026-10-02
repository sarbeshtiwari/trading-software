"""Startup/shutdown orchestration — ARCH-015.

Shutdown ordering matters in a trading system. On SIGTERM the process must:

1. stop accepting new work (no new entries),
2. let in-flight order operations finish or record themselves,
3. flush audit and journal writes,
4. release the exclusive trading lock,
5. close database and broker connections.

Hooks run in **reverse registration order** (like a stack), each with its own
timeout so one wedged hook cannot prevent the rest from running.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union

from app.core.logging import get_logger

logger = get_logger("core.lifecycle")

__all__ = ["Lifecycle", "ShutdownHook", "get_lifecycle", "reset_lifecycle"]

HookFn = Callable[[], Union[None, Awaitable[None]]]


@dataclass
class ShutdownHook:
    name: str
    fn: HookFn
    timeout_seconds: float = 10.0
    critical: bool = False


class Lifecycle:
    """Registry of shutdown hooks plus a process-wide shutting-down flag."""

    def __init__(self) -> None:
        self._hooks: list[ShutdownHook] = []
        self._shutting_down = False
        self._completed = False
        self._signals_installed = False

    @property
    def shutting_down(self) -> bool:
        """True once shutdown has begun. The trading loop must stop entering."""
        return self._shutting_down

    @property
    def completed(self) -> bool:
        return self._completed

    def on_shutdown(
        self,
        fn: HookFn,
        *,
        name: str,
        timeout_seconds: float = 10.0,
        critical: bool = False,
    ) -> None:
        """Register a hook. ``critical`` hooks log at ERROR when they fail."""
        self._hooks.append(
            ShutdownHook(name=name, fn=fn, timeout_seconds=timeout_seconds, critical=critical)
        )
        logger.debug("Registered shutdown hook", extra={"hook": name})

    async def shutdown(self, reason: str = "requested") -> None:
        """Run every hook in reverse order. Safe to call more than once."""
        if self._shutting_down:
            logger.debug("Shutdown already in progress")
            return
        self._shutting_down = True
        logger.info("Shutdown started", extra={"reason": reason, "hooks": len(self._hooks)})

        for hook in reversed(self._hooks):
            await self._run_hook(hook)

        self._completed = True
        logger.info("Shutdown complete", extra={"reason": reason})

    async def _run_hook(self, hook: ShutdownHook) -> None:
        try:
            result = hook.fn()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=hook.timeout_seconds)
            logger.info("Shutdown hook finished", extra={"hook": hook.name})
        except asyncio.TimeoutError:
            log = logger.error if hook.critical else logger.warning
            log(
                "Shutdown hook timed out",
                extra={"hook": hook.name, "timeout_seconds": hook.timeout_seconds},
            )
        except Exception as exc:  # noqa: BLE001 - one bad hook must not stop the rest
            log = logger.error if hook.critical else logger.warning
            log("Shutdown hook failed", extra={"hook": hook.name, "error": str(exc)})

    def install_signal_handlers(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Route SIGTERM/SIGINT into :meth:`shutdown`.

        ``add_signal_handler`` is unavailable on Windows, where the fallback is
        the synchronous :mod:`signal` handler; both end at the same coroutine.
        """
        if self._signals_installed:
            return
        active_loop = loop or asyncio.get_event_loop()

        def _handle(signame: str) -> None:
            logger.info("Received signal", extra={"signal": signame})
            asyncio.ensure_future(self.shutdown(reason=signame))

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                active_loop.add_signal_handler(sig, _handle, sig.name)
            except (NotImplementedError, AttributeError, RuntimeError):
                signal.signal(sig, lambda s, _f: _handle(signal.Signals(s).name))
        self._signals_installed = True


_lifecycle = Lifecycle()


def get_lifecycle() -> Lifecycle:
    return _lifecycle


def reset_lifecycle() -> Lifecycle:
    """Test helper: start from a clean hook registry."""
    global _lifecycle
    _lifecycle = Lifecycle()
    return _lifecycle
