"""Lemon Squeezy webhooks (S4-01). Authentication is real: the X-Signature header is the hex HMAC-SHA256 of the RAW body keyed by the
webhook's signing secret, compared in constant time (documented by Lemon Squeezy). An order_created event from the configured store becomes a
SALE signal; the buyer's address and name are never read. If the checkout link carried a lead id as custom data (the Closer's `ref`), it is
returned as the attributed lead. Test-mode orders are ignored unless allowed. Every other event is ignored, not rejected, so the provider does
not retry it forever. Field names follow the documentation and were not verified against live traffic."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Mapping, Optional

from shared.models import Marketplace, Signal

_ID = re.compile(r"^[A-Za-z0-9_:.=-]{1,100}$")


class WebhookRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ParsedSignal:
    signal: Signal
    attributed_lead: Optional[uuid.UUID] = None


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class LemonSqueezyWebhook:
    name = "lemonsqueezy"

    def __init__(self, signing_secret: str, store_id, *, accept_test_mode: bool = False):
        if not isinstance(signing_secret, str) or len(signing_secret) < 8:
            raise ValueError("the webhook signing secret must be at least 8 characters")
        self._secret, self._store, self._test = signing_secret, str(store_id), accept_test_mode

    def handle(self, headers: Mapping[str, str], body: bytes) -> List[ParsedSignal]:
        given = headers.get("x-signature", "")
        if not isinstance(given, str) or not hmac.compare_digest(sign(self._secret, body).encode("ascii"), given.strip().lower().encode("utf-8", "replace")):
            raise WebhookRejected("BAD_SIGNATURE")
        try:
            payload = json.loads(body.decode("utf-8"))
            meta, data = payload["meta"], payload["data"]
            event = meta["event_name"]
        except (ValueError, UnicodeDecodeError, KeyError, TypeError):
            raise WebhookRejected("INVALID_PAYLOAD") from None
        if not isinstance(event, str) or event != headers.get("x-event-name", event):
            raise WebhookRejected("INVALID_PAYLOAD")
        if event != "order_created" or (meta.get("test_mode") and not self._test):
            return []
        try:
            attrs, order_id = data["attributes"], str(data["id"])
            if str(attrs["store_id"]) != self._store:
                return []                                                       # an order from some other store: not ours
            created = datetime.fromisoformat(str(attrs["created_at"]).replace("Z", "+00:00"))
            product = str(attrs["first_order_item"]["product_id"])
        except (KeyError, TypeError, ValueError):
            raise WebhookRejected("INVALID_PAYLOAD") from None
        if not _ID.match(order_id) or not _ID.match(product):
            raise WebhookRejected("INVALID_PAYLOAD")
        lead = None
        custom = meta.get("custom_data")
        if isinstance(custom, dict) and isinstance(custom.get("ref"), str):
            try:
                lead = uuid.UUID(custom["ref"])
            except ValueError:
                lead = None
        occurred = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        return [ParsedSignal(Signal(signal_ref=f"ls_order_{order_id}", kind="SALE", marketplace=Marketplace.LEMON_SQUEEZY,
                                    product_external_id=product, occurred_at=occurred), lead)]
