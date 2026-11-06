"""LemonSqueezyAdapter (S4-01). Declares what Lemon Squeezy can do for this platform (discount codes and checkout links; it has no buyer
messaging API, ADR-08), maps its schema and enforces its rules. Signals come from webhooks via the inbox, so fetch_signals only reads what
the receiver stored. The rate limit is 300 requests a minute (documented): declared as a fixed window, and DEFAULT_BUCKET is the lane
configuration for the rate governor. Checkout links carry custom data and discounts in the documented query form
(checkout[custom][key]=value, discount=CODE), which is NOT verified against the live site (open item)."""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from adapters import policy
from adapters.base import MarketplaceAdapter
from adapters.errors import MalformedResponse, UnsupportedParameter
from adapters.lemon_squeezy import mapping
from adapters.lemon_squeezy.transport import LemonSqueezyTransport
from shared.models import (AdapterCapabilities, AdapterHealth, ComplianceVerdict, Discount, Marketplace, OfferResult, ProductSnapshot,
                           ProposedAction, RateModel, Signal)

_CODE = re.compile(r"^[A-Za-z0-9]{3,64}$")                 # conservative: letters and digits (the platform's rule is not confirmed)
_CUSTOM_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DEFAULT_BUCKET = {"capacity": 300, "refill_per_second": 5.0}      # 300 requests per minute


class LemonSqueezyAdapter(MarketplaceAdapter):
    marketplace = Marketplace.LEMON_SQUEEZY
    capabilities = AdapterCapabilities(
        supports_outbound_messaging=False, supports_offer_codes=True, supports_checkout_url=True, supports_listing_mutation=False,
        supports_acp_checkout=False, max_message_length=None, allowed_message_contexts=(), rate_limit_model=RateModel.FIXED_WINDOW,
        requires_idempotency_key=False)

    def __init__(self, transport: LemonSqueezyTransport, signals=None, *, clock: Optional[Callable[[], datetime]] = None):
        self.transport, self._signals = transport, signals
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def list_products(self) -> List[ProductSnapshot]:
        now = self._clock()
        return [mapping.product_snapshot(p, now) for p in self.transport.get_products()]

    def get_product(self, external_id: str) -> ProductSnapshot:
        return mapping.product_snapshot(self.transport.get_product(external_id), self._clock())

    def fetch_signals(self, since: datetime) -> List[Signal]:
        return list(self._signals.read(self.marketplace.value, since)) if self._signals is not None else []

    def preflight_check(self, action: ProposedAction) -> ComplianceVerdict:
        return policy.verdict_for(policy.base_reasons(action, self.marketplace, self.capabilities))

    def health_check(self) -> AdapterHealth:
        return policy.health_from_ping(self.transport.ping, self._clock)

    # ------------------------------------------------------------------ declared capabilities
    def _create_offer_code(self, product_external_id: str, code: str, discount: Discount, expires_at: Optional[datetime]) -> OfferResult:
        if not _CODE.match(code):
            raise ValueError("offer code must be 3 to 64 letters or digits")
        if discount.kind == "PERCENT":
            if discount.percent != int(discount.percent) or not 1 <= int(discount.percent) <= 100:
                raise UnsupportedParameter("percent")
            amount, amount_type = int(discount.percent), "percent"
        else:
            if not discount.fixed_minor or discount.fixed_minor <= 0:
                raise UnsupportedParameter("fixed amount")
            amount, amount_type = int(discount.fixed_minor), "fixed"
        if expires_at is not None and expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        variants = self.transport.get_variant_ids(product_external_id)           # a discount is limited to this product's variants
        if not variants:
            raise MalformedResponse("missing variants")
        self.transport.create_discount(code=code, amount=amount, amount_type=amount_type, variant_ids=variants, expires_at=expires_at)
        return OfferResult(code=code, checkout_url=self._build_checkout_url(product_external_id, {"discount": code}), expires_at=expires_at)

    def _build_checkout_url(self, product_external_id: str, params: Dict[str, str]) -> str:
        base = self.transport.get_product(product_external_id).get("buy_now_url")
        if not isinstance(base, str) or not base.startswith("https://"):
            raise MalformedResponse("missing field buy_now_url")
        pairs = []
        for key, value in sorted(params.items()):
            if key == "discount":
                pairs.append(("discount", value))
            elif _CUSTOM_KEY.match(key):
                pairs.append((f"checkout[custom][{key}]", value))               # comes back in the order webhook as custom data
            else:
                raise ValueError("custom checkout keys are lowercase letters, digits and underscores")
        return base + (("&" if "?" in base else "?") + urllib.parse.urlencode(pairs) if pairs else "")
