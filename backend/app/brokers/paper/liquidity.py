"""Conserve recorded snapshot depth from durable PAPER fill evidence."""

import hashlib
from dataclasses import replace
from datetime import datetime

from app.audit.snapshots import canonical
from app.core.clock import UTC
from app.core.enums import TransactionType
from app.core.errors import ValidationError


def _book(quote):
    result = {"data_origin": quote.data_origin.value}
    for side in ("bids", "asks"):
        levels = getattr(quote, side)
        if any(
            not level.price.is_finite()
            or level.price <= 0
            or type(level.quantity) is not int
            or level.quantity < 0
            for level in levels
        ):
            raise ValidationError("Invalid PAPER depth snapshot")
        result[side] = [
            {"price": format(level.price.normalize(), "f"), "quantity": level.quantity}
            for level in levels
        ]
    return result


def _consumes_snapshot(fill, *, observed, book, side, mode, same_side):
    previous = fill.raw.get("liquidity")
    if previous is None:
        if same_side and (fill.executed_at is None or observed <= fill.executed_at):
            raise ValidationError("Legacy PAPER fill depth unavailable; newer observation required")
        return False
    if previous.get("version") != 1:
        raise ValidationError("Unknown PAPER liquidity evidence version")
    prior_time = datetime.fromisoformat(previous["observed_at"])
    if prior_time.utcoffset() is None or observed < prior_time:
        raise ValidationError("PAPER depth observation regressed")
    prior_digest = hashlib.sha256(canonical(previous["book"]).encode()).hexdigest()
    if prior_digest != previous["book_sha256"]:
        raise ValidationError("PAPER liquidity evidence integrity invalid")
    if observed != prior_time:
        return False
    if not same_side:
        return False
    if (
        canonical(book[side]) != canonical(previous["book"][side])
        or book["data_origin"] != previous["book"]["data_origin"]
    ):
        raise ValidationError("Conflicting PAPER depth at the same observation time")
    if previous["mode"] != mode:
        raise ValidationError("PAPER fill mode changed within a snapshot")
    return mode == "DEPTH"


def remaining_depth(quote, request, *, use_depth, orders):
    if quote.observed_at.utcoffset() is None:
        raise ValidationError("PAPER quote timestamp must be timezone-aware")
    identity = (request.exchange, request.segment, request.trading_symbol)
    if identity != (
        quote.instrument.exchange,
        quote.instrument.segment,
        quote.instrument.trading_symbol,
    ):
        raise ValidationError("PAPER quote instrument mismatch")
    observed = quote.observed_at.astimezone(UTC)
    book = _book(quote)
    digest = hashlib.sha256(canonical(book).encode()).hexdigest()
    side = "asks" if request.transaction_type == TransactionType.BUY else "bids"
    levels = getattr(quote, side)
    mode = "DEPTH" if use_depth and levels else "LTP_FALLBACK"
    evidence = {
        "version": 1,
        "observed_at": observed.isoformat(),
        "book_sha256": digest,
        "book": book,
        "mode": mode,
    }
    consumed = {}
    for order in orders:
        if identity != (
            order.request.exchange,
            order.request.segment,
            order.request.trading_symbol,
        ):
            continue
        same_side = request.transaction_type == order.request.transaction_type
        for fill in order.trades:
            if _consumes_snapshot(
                fill, observed=observed, book=book, side=side, mode=mode, same_side=same_side
            ):
                consumed[fill.price] = consumed.get(fill.price, 0) + fill.quantity
    if mode != "DEPTH":
        return quote, evidence
    remaining = []
    for level in levels:
        price = level.price
        used = min(level.quantity, consumed.get(price, 0))
        consumed[price] = consumed.get(price, 0) - used
        remaining.append(replace(level, quantity=level.quantity - used))
    if any(quantity for quantity in consumed.values()):
        raise ValidationError("PAPER fills exceed recorded snapshot depth")
    return replace(quote, **{side: tuple(remaining)}), evidence
