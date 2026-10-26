from __future__ import annotations

import re
from datetime import datetime, timezone

from adapters.errors import MalformedResponse
from shared.models import ListingStatus, Marketplace, Money, ProductSnapshot, Signal

_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")

def _bad(field: str) -> MalformedResponse:
    return MalformedResponse(f"invalid or missing field: {field}")

def _require(raw: dict, field: str):
    if field not in raw:
        raise _bad(field)
    return raw[field]

def product_snapshot(raw: dict, now: datetime) -> ProductSnapshot:
    if not isinstance(raw, dict):
        raise _bad("product")

    pid = _require(raw, "id")
    if isinstance(pid, bool) or not isinstance(pid, (str, int)) or pid == "":
        raise _bad("id")

    name = _require(raw, "name")
    if not isinstance(name, str) or not name.strip():
        raise _bad("name")

    price = _require(raw, "price")
    if isinstance(price, bool) or not isinstance(price, int) or price < 0:
        raise _bad("price")

    currency = _require(raw, "currency")
    if not isinstance(currency, str) or not _CURRENCY_RE.match(currency.upper()):
        raise _bad("currency")

    published = _require(raw, "published")
    if not isinstance(published, bool):
        raise _bad("published")

    short_url = raw.get("short_url")
    if short_url is not None and not isinstance(short_url, str):
        raise _bad("short_url")

    description = raw.get("description")
    if description is None:
        description = ""

    return ProductSnapshot(
        external_id=str(pid),
        title=name,
        price=Money(amount_minor=price, currency=currency.upper()),
        status=ListingStatus.ACTIVE if published else ListingStatus.PAUSED,
        checkout_url=short_url,
        last_synced_at=now,
        description=description,
    )

def _signal_ref(sale_id) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "", str(sale_id))
    if not safe:
        raise _bad("id")
    return "gr_sale_" + safe

def _parse_dt(value):
    if not isinstance(value, str):
        raise _bad("created_at")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _bad("created_at") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def sale_signal(raw: dict) -> Signal:
    if not isinstance(raw, dict):
        raise _bad("sale")

    sale_id = _require(raw, "id")
    if sale_id in (None, ""):
        raise _bad("id")

    occurred_at = _parse_dt(_require(raw, "created_at"))

    product_id = raw.get("product_id")
    product_external_id = str(product_id) if product_id is not None else None

    return Signal(
        kind="SALE",
        marketplace=Marketplace.GUMROAD,
        product_external_id=product_external_id,
        signal_ref=_signal_ref(sale_id),
        occurred_at=occurred_at,
    )
