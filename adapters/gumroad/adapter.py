from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone

from adapters.base import UnsupportedCapability
from adapters.errors import (
    AdapterError,
    AuthExpired,
    MalformedResponse,
    NotFound,
    RateLimited,
    UnsupportedParameter,
)
from adapters.gumroad import mapping
from shared.models import (
    ActionType,
    Capabilities,
    Discount,
    Health,
    ListingStatus,
    Marketplace,
    OfferResult,
    ProposedAction,
    Verification,
    Verdict,
)

_CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

class GumroadAdapter:
    capabilities = Capabilities(
        supports_offer_codes=True,
        supports_checkout_url=True,
        supports_outbound_messaging=False,
        supports_listing_mutation=False,
        supports_acp_checkout=False,
    )

    def __init__(self, transport, *, clock=None):
        self.transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return self._clock()

    def preflight_check(self, action: ProposedAction) -> Verification:
        reasons: list[str] = []
        if action.marketplace is not Marketplace.GUMROAD:
            reasons.append("WRONG_MARKETPLACE")
        if action.action_type in (ActionType.SEND_MESSAGE, ActionType.REPLY):
            reasons.append("MESSAGING_UNSUPPORTED")
        if action.action_type is ActionType.UPDATE_LISTING:
            reasons.append("LISTING_MUTATION_UNSUPPORTED")
        if "captcha_bypass" in action.execution_plan:
            reasons.append("CAPTCHA_BYPASS")
        if "browser_automation" in action.execution_plan:
            reasons.append("BROWSER_AUTOMATION")
        reasons = sorted(set(reasons))
        verdict = Verdict.BLOCK if reasons else Verdict.PASS
        return Verification(verdict=verdict, reason_codes=tuple(reasons))

    def list_products(self):
        raw = self.transport.get_products()
        now = self._now()
        return [mapping.product_snapshot(p, now) for p in raw]

    def get_product(self, product_id: str):
        raw = self.transport.get_product(product_id)
        return mapping.product_snapshot(raw, self._now())

    def build_checkout_url(self, product_id: str, params: dict) -> str:
        product = self.transport.get_product(product_id)
        url = product.get("short_url")
        if not url:
            raise MalformedResponse("product has no short_url")
        if not params:
            return url
        query = urllib.parse.urlencode(sorted(params.items()))
        return f"{url}?{query}"

    def create_offer_code(self, product_id: str, code: str,
                          discount: Discount, expires_at) -> OfferResult:
        if not _CODE_RE.match(code or ""):
            raise ValueError("invalid offer code")
        if expires_at is not None:
            raise UnsupportedParameter("expires_at is not supported by Gumroad")

        if discount.kind == "PERCENT":
            if discount.percent is None:
                raise UnsupportedParameter("percent is required for PERCENT discount")
            whole = discount.percent.to_integral_value()
            if whole != discount.percent or not (1 <= int(whole) <= 100):
                raise UnsupportedParameter("percent must be an integer in 1..100")
            kwargs = {"percent_off": int(whole)}
        elif discount.kind == "FIXED":
            if discount.fixed_minor is None or discount.fixed_minor < 1:
                raise UnsupportedParameter("fixed_minor must be a positive integer")
            kwargs = {"amount_off_cents": int(discount.fixed_minor)}
        else:
            raise UnsupportedParameter(f"unknown discount kind {discount.kind!r}")

        product = self.transport.get_product(product_id)
        short_url = product.get("short_url")
        if not short_url:
            raise MalformedResponse("product has no short_url")

        self.transport.create_offer_code(product_id, name=code, **kwargs)
        return OfferResult(code=code, checkout_url=f"{short_url}/{code}")

    def fetch_signals(self, since: datetime):
        raws = self.transport.get_sales(since.date())
        signals = [mapping.sale_signal(r) for r in raws]
        signals = [s for s in signals if s.occurred_at >= since]
        signals.sort(key=lambda s: s.occurred_at)
        return signals

    def health_check(self) -> Health:
        now = self._now()
        try:
            self.transport.ping()
        except AuthExpired:
            return Health(ok=False, detail_code="AUTH_EXPIRED", checked_at=now)
        except RateLimited:
            return Health(ok=False, detail_code="RATE_LIMITED", checked_at=now)
        except AdapterError:
            return Health(ok=False, detail_code="DEGRADED", checked_at=now)
        return Health(ok=True, detail_code=None, checked_at=now)

    def send_reply(self, conversation_id, text, message_id):
        raise UnsupportedCapability("Gumroad does not support outbound messaging")

    def update_status(self, product_id: str, status: ListingStatus):
        raise UnsupportedCapability("Gumroad listing mutation is not supported")
