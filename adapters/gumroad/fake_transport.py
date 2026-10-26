"""In-memory GumroadTransport for tests and contract harnesses. Records every call, and every mutating call
separately in `writes`, so a contract check can prove an operation had no effect."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date
from typing import Any, Dict, List, Optional

from adapters.errors import AuthExpired, NotFound


class FakeGumroadTransport:
    def __init__(self):
        self.products: Dict[str, Dict[str, Any]] = {}
        self.sales: List[Dict[str, Any]] = []
        self.calls: List[str] = []
        self.writes: List[tuple] = []
        self._faults: Dict[str, deque] = defaultdict(deque)
        self.auth_expired = False

    def add_product(self, product_id: str, name: str = "Guide", price: int = 1900, currency: str = "usd",
                    published: bool = True, short_url: Optional[str] = None, **extra) -> None:
        self.products[product_id] = {"id": product_id, "name": name, "price": price, "currency": currency,
                                     "published": published, "short_url": short_url or f"https://gum.test/l/{product_id}",
                                     **extra}

    def inject(self, op: str, exc: Exception, times: int = 1) -> None:
        self._faults[op].extend([exc] * times)

    def _gate(self, op: str) -> None:
        self.calls.append(op)
        if self.auth_expired:
            raise AuthExpired()
        if self._faults[op]:
            raise self._faults[op].popleft()

    def ping(self) -> None:
        self._gate("ping")

    def get_products(self) -> List[Dict[str, Any]]:
        self._gate("get_products")
        return list(self.products.values())

    def get_product(self, product_id: str) -> Dict[str, Any]:
        self._gate("get_product")
        if product_id not in self.products:
            raise NotFound()
        return dict(self.products[product_id])

    def create_offer_code(self, product_id: str, *, name: str, percent_off: Optional[int] = None,
                          amount_off_cents: Optional[int] = None) -> Dict[str, Any]:
        self._gate("create_offer_code")
        if product_id not in self.products:
            raise NotFound()
        self.writes.append(("offer_code", product_id, name))
        return {"name": name, "percent_off": percent_off, "amount_off": amount_off_cents}

    def get_sales(self, after: date) -> List[Dict[str, Any]]:
        self._gate("get_sales")
        return [s for s in self.sales if str(s.get("created_at", ""))[:10] >= after.isoformat()]
