"""PayhipAdapter (S4-02). Payhip's public API only does coupons and license keys, so this adapter is smaller than the others and says so:
  * products come from a CATALOGUE the seller keeps (key, name, price): Payhip cannot list them;
  * signals come only from webhooks, via the inbox (Payhip has no sales endpoint to poll);
  * offer codes are single-product PERCENT coupons: fixed amounts (unit unconfirmed) and expiry are refused, not guessed;
  * the checkout link is the product page; only `coupon` is honoured. Payhip hands nothing back to us, so a lead id would be shared for no
    benefit: every other parameter is dropped, and a Payhip sale cannot be attributed to a lead through the link.
Rate limits are unpublished: declared as a token bucket, with CONSERVATIVE_BUCKET (our own number) as the governor lane."""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from adapters import policy
from adapters.base import MarketplaceAdapter
from adapters.errors import NotFound, UnsupportedParameter
from adapters.payhip.transport import PayhipTransport
from shared.models import (AdapterCapabilities, AdapterHealth, ComplianceVerdict, Discount, ListingStatus, Marketplace, Money, OfferResult,
                           ProductSnapshot, ProposedAction, RateModel, Signal)

_CODE = re.compile(r"^[A-Za-z0-9]{3,64}$")
_KEY = re.compile(r"^[A-Za-z0-9_:.=-]{1,100}$")
PRODUCT_URL = "https://payhip.com/b/{key}"          # the form Payhip's own webhooks give as product_permalink


@dataclass(frozen=True)
class PayhipProduct:
    key: str
    name: str
    price_minor: int
    currency: str = "USD"
    description: str = ""

    def __post_init__(self):
        if not _KEY.match(self.key) or not self.name.strip() or self.price_minor < 0 or len(self.currency) != 3:
            raise ValueError("invalid catalogue entry")


class PayhipAdapter(MarketplaceAdapter):
    marketplace = Marketplace.PAYHIP
    capabilities = AdapterCapabilities(
        supports_outbound_messaging=False, supports_offer_codes=True, supports_checkout_url=True, supports_listing_mutation=False,
        supports_acp_checkout=False, max_message_length=None, allowed_message_contexts=(), rate_limit_model=RateModel.TOKEN_BUCKET,
        requires_idempotency_key=False)

    def __init__(self, transport: PayhipTransport, catalog: Sequence[PayhipProduct] = (), signals=None, *,
                 clock: Optional[Callable[[], datetime]] = None):
        self.transport, self.catalog, self._signals = transport, list(catalog), signals
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _find(self, key: str) -> PayhipProduct:
        for p in self.catalog:
            if p.key == key:
                return p
        raise NotFound()

    def _snapshot(self, p: PayhipProduct) -> ProductSnapshot:
        return ProductSnapshot(external_id=p.key, title=p.name, description=p.description, price=Money(amount_minor=p.price_minor, currency=p.currency.upper()),
                               status=ListingStatus.ACTIVE, checkout_url=PRODUCT_URL.format(key=p.key), last_synced_at=self._clock())

    def list_products(self) -> List[ProductSnapshot]:
        return [self._snapshot(p) for p in self.catalog]

    def get_product(self, external_id: str) -> ProductSnapshot:
        return self._snapshot(self._find(external_id))

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
        self._find(product_external_id)                                   # only products in the catalogue
        if expires_at is not None:
            raise UnsupportedParameter("expires_at")
        if discount.kind != "PERCENT":
            raise UnsupportedParameter("fixed amount")                   # the unit of amount_off is not confirmed
        if discount.percent != int(discount.percent) or not 1 <= int(discount.percent) <= 100:
            raise UnsupportedParameter("percent")
        self.transport.create_coupon(code=code, product_key=product_external_id, percent_off=int(discount.percent))
        return OfferResult(code=code, checkout_url=self._build_checkout_url(product_external_id, {"coupon": code}))

    def _build_checkout_url(self, product_external_id: str, params: Dict[str, str]) -> str:
        url = PRODUCT_URL.format(key=self._find(product_external_id).key)
        return url + (f"?{urllib.parse.urlencode({'coupon': params['coupon']})}" if "coupon" in params else "")
