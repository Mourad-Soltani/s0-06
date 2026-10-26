"""GumroadAdapter (S2-01). Declares what Gumroad can do for this platform, maps its schema, and enforces its
rules. It holds no sales logic. Declared capabilities: offer codes and checkout links only. Gumroad has no
buyer-messaging API (ADR-08), and listing mutation is not declared because it is not confirmed (S0-03)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from adapters import policy
from adapters.base import MarketplaceAdapter
from adapters.errors import MalformedResponse, UnsupportedParameter
from adapters.gumroad import mapping
from adapters.gumroad.transport import GumroadTransport
from shared.models import (AdapterCapabilities, AdapterHealth, ComplianceVerdict, Discount, Marketplace, OfferResult,
                           ProductSnapshot, ProposedAction, RateModel, Signal)

_CODE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class GumroadAdapter(MarketplaceAdapter):
    marketplace = Marketplace.GUMROAD
    capabilities = AdapterCapabilities(
        supports_outbound_messaging=False, supports_offer_codes=True, supports_checkout_url=True,
        supports_listing_mutation=False, supports_acp_checkout=False, max_message_length=None,
        allowed_message_contexts=(), rate_limit_model=RateModel.TOKEN_BUCKET,   # limits undocumented: governor config decides
        requires_idempotency_key=False)

    def __init__(self, transport: GumroadTransport, *, clock: Optional[Callable[[], datetime]] = None):
        self.transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ always required
    def list_products(self) -> List[ProductSnapshot]:
        now = self._clock()
        return [mapping.product_snapshot(p, now) for p in self.transport.get_products()]

    def get_product(self, external_id: str) -> ProductSnapshot:
        return mapping.product_snapshot(self.transport.get_product(external_id), self._clock())

    def fetch_signals(self, since: datetime) -> List[Signal]:
        since = since.astimezone(timezone.utc)
        signals = [mapping.sale_signal(s) for s in self.transport.get_sales(since.date())]   # the API filters by day
        return sorted((s for s in signals if s.occurred_at >= since), key=lambda s: s.occurred_at)

    def preflight_check(self, action: ProposedAction) -> ComplianceVerdict:
        """Pure policy: no network, no side effect."""
        return policy.verdict_for(policy.base_reasons(action, self.marketplace, self.capabilities))

    def health_check(self) -> AdapterHealth:
        return policy.health_from_ping(self.transport.ping, self._clock)

    # ------------------------------------------------------------------ declared capabilities
    def _create_offer_code(self, product_external_id: str, code: str, discount: Discount,
                           expires_at: Optional[datetime]) -> OfferResult:
        if not _CODE.match(code):                       # refuse before any remote change
            raise ValueError("offer code must be 1 to 64 letters, digits, underscores or hyphens")
        if expires_at is not None:
            raise UnsupportedParameter("expires_at")      # no expiry on Gumroad offer codes in the API we know of
        if discount.kind == "PERCENT":
            if discount.percent != int(discount.percent):
                raise UnsupportedParameter("fractional percent")
            raw = self.transport.create_offer_code(product_external_id, name=code, percent_off=int(discount.percent))
        else:
            raw = self.transport.create_offer_code(product_external_id, name=code, amount_off_cents=discount.fixed_minor)
        product = self.transport.get_product(product_external_id)
        url = product.get("short_url")
        if not isinstance(url, str):
            raise MalformedResponse("missing field short_url")
        return OfferResult(code=code, checkout_url=f"{url.rstrip('/')}/{code}")   # offer link form is UNVERIFIED (S0-03)

    def _build_checkout_url(self, product_external_id: str, params: Dict[str, str]) -> str:
        url = self.transport.get_product(product_external_id).get("short_url")
        if not isinstance(url, str):
            raise MalformedResponse("missing field short_url")
        return url + (f"?{urlencode(sorted(params.items()))}" if params else "")
