"""Gumroad JSON to canonical models. Field names follow Gumroad's public API v2 as described by third-party
references; they were NOT verified against the live API (open item S0-03). Anything missing or mistyped raises
MalformedResponse naming the FIELD, never the value, so a surprise fails loudly instead of being guessed at.
Buyer details in sale payloads (email, name, IP) are never read."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from adapters.errors import MalformedResponse
from shared.models import ListingStatus, Marketplace, Money, ProductSnapshot, Signal


def _need(raw: Mapping[str, Any], key: str, kind) -> Any:
    if key not in raw:
        raise MalformedResponse(f"missing field {key}")
    value = raw[key]
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise MalformedResponse(f"wrong type for field {key}")
    return value


def product_snapshot(raw: Mapping[str, Any], now: datetime) -> ProductSnapshot:
    pid, name = str(_need(raw, "id", (str, int))), _need(raw, "name", str)
    price, currency = _need(raw, "price", int), _need(raw, "currency", str)
    published = _need(raw, "published", bool)
    if not name.strip():
        raise MalformedResponse("empty field name")
    if price < 0 or len(currency) != 3 or not currency.isalpha():
        raise MalformedResponse("invalid field price or currency")
    description = raw.get("description")
    return ProductSnapshot(
        external_id=pid, title=name[:512], description=description[:20000] if isinstance(description, str) else "",
        price=Money(amount_minor=price, currency=currency.upper()),     # price is in the currency's minor unit
        status=ListingStatus.ACTIVE if published else ListingStatus.PAUSED,
        checkout_url=raw.get("short_url") if isinstance(raw.get("short_url"), str) else None, last_synced_at=now)


def parse_time(value: str) -> datetime:
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise MalformedResponse("invalid timestamp") from None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def sale_signal(raw: Mapping[str, Any]) -> Signal:
    """A sale becomes a SALE signal. Gumroad ids may contain characters outside the opaque alphabet, so the
    signal_ref is a stable hash of the id: opaque, unique, and safe for core.lead.signal_ref."""
    sale_id = str(_need(raw, "id", (str, int)))
    occurred = parse_time(_need(raw, "created_at", str))
    product_id = raw.get("product_id")
    return Signal(signal_ref="gr_sale_" + hashlib.sha256(sale_id.encode("utf-8")).hexdigest()[:40], kind="SALE",
                  marketplace=Marketplace.GUMROAD,
                  product_external_id=str(product_id) if isinstance(product_id, (str, int)) and product_id != "" else None,
                  occurred_at=occurred)
