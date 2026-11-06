"""Lemon Squeezy product data to canonical models. The transport hands over a flat dict (id, name, description, status, price, currency,
buy_now_url); anything missing or mistyped raises MalformedResponse naming the FIELD, never the value. Field names follow Lemon Squeezy's public
documentation and were NOT verified against the live API (open item)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from adapters.errors import MalformedResponse
from shared.models import ListingStatus, Money, ProductSnapshot


def _need(raw: Mapping[str, Any], key: str, kind) -> Any:
    if key not in raw:
        raise MalformedResponse(f"missing field {key}")
    value = raw[key]
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise MalformedResponse(f"wrong type for field {key}")
    return value


def product_snapshot(raw: Mapping[str, Any], now: datetime) -> ProductSnapshot:
    pid, name = str(_need(raw, "id", (str, int))), _need(raw, "name", str)
    status, price, currency = _need(raw, "status", str), _need(raw, "price", int), _need(raw, "currency", str)
    if not name.strip():
        raise MalformedResponse("empty field name")
    if price < 0 or len(currency) != 3 or not currency.isalpha():
        raise MalformedResponse("invalid field price or currency")
    url, description = raw.get("buy_now_url"), raw.get("description")
    return ProductSnapshot(external_id=pid, title=name[:512], description=description[:20000] if isinstance(description, str) else "",
                           price=Money(amount_minor=price, currency=currency.upper()),
                           status={"published": ListingStatus.ACTIVE, "draft": ListingStatus.DRAFT}.get(status, ListingStatus.PAUSED),
                           checkout_url=url if isinstance(url, str) and url.startswith("https://") else None, last_synced_at=now)
