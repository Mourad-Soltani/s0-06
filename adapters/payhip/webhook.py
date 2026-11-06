"""Payhip webhooks (S4-02). Payhip's public API cannot list sales or products, so a `paid` webhook is the ONLY source of Payhip signals.
Its authentication is weak by design: the payload's `signature` is the SHA-256 of the account's API key, a constant, not an HMAC of the
body. Anyone who has ever seen one payload can forge another. This handler still checks it (constant time), and the receiver adds what the
platform does not: a secret URL token (webhook_app.py), a freshness window (a replayed old event is ignored), and the one-signal-per-item
idempotency key. Do not treat a Payhip signal as proof of payment. A `paid` event becomes one SALE signal per item; refunds and subscription
events are ignored. The buyer's address, name and IP are never read. Field names follow Payhip's webhook article."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Mapping

from adapters.lemon_squeezy.webhook import ParsedSignal, WebhookRejected
from shared.models import Marketplace, Signal

_ID = re.compile(r"^[A-Za-z0-9_:.=-]{1,100}$")


class PayhipWebhook:
    name = "payhip"

    def __init__(self, api_key: str, *, max_age: timedelta = timedelta(hours=24), clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not isinstance(api_key, str) or len(api_key) < 8:
            raise ValueError("the API key must be at least 8 characters")
        self._expected, self._max_age, self._clock = hashlib.sha256(api_key.encode("utf-8")).hexdigest(), max_age, clock

    def handle(self, headers: Mapping[str, str], body: bytes) -> List[ParsedSignal]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise WebhookRejected("INVALID_PAYLOAD") from None
        if not isinstance(payload, dict):
            raise WebhookRejected("INVALID_PAYLOAD")
        given = payload.get("signature")
        if not isinstance(given, str) or not hmac.compare_digest(self._expected.encode("ascii"), given.strip().lower().encode("utf-8", "replace")):
            raise WebhookRejected("BAD_SIGNATURE")
        if payload.get("type") != "paid":
            return []                                                          # refunds, subscriptions: ignored, not rejected
        tx, when, items = payload.get("id"), payload.get("date"), payload.get("items")
        if not isinstance(tx, str) or not _ID.match(tx) or isinstance(when, bool) or not isinstance(when, int) or not isinstance(items, list):
            raise WebhookRejected("INVALID_PAYLOAD")
        occurred = datetime.fromtimestamp(when, tz=timezone.utc)
        now = self._clock()
        if now - occurred > self._max_age or occurred - now > timedelta(days=1):
            return []                                                          # a stale or future-dated event: probably a replay
        out: List[ParsedSignal] = []
        for n, item in enumerate(items):
            key = item.get("product_key") if isinstance(item, dict) else None
            if not isinstance(key, str) or not _ID.match(key):
                raise WebhookRejected("INVALID_PAYLOAD")
            out.append(ParsedSignal(Signal(signal_ref=f"ph_tx_{tx}_{n}", kind="SALE", marketplace=Marketplace.PAYHIP, product_external_id=key,
                                           occurred_at=occurred)))
        return out
