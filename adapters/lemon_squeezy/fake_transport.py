"""In-memory LemonSqueezyTransport for tests and contract harnesses (records every call and every write)."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from adapters.errors import AuthExpired, NotFound


class FakeLemonSqueezyTransport:
    def __init__(self):
        self.products: Dict[str, Dict[str, Any]] = {}
        self.calls: List[str] = []
        self.writes: List[tuple] = []
        self._faults: Dict[str, deque] = defaultdict(deque)
        self.auth_expired = False

    def add_product(self, product_id: str, name: str = "Guide", price: int = 1900, currency: str = "usd", status: str = "published",
                    buy_now_url: Optional[str] = None, **extra) -> None:
        self.products[product_id] = {"id": product_id, "name": name, "description": "d", "status": status, "price": price, "currency": currency,
                                     "buy_now_url": buy_now_url or f"https://shop.test/buy/{product_id}", **extra}

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

    def get_variant_ids(self, product_id: str) -> List[str]:
        self._gate("get_variant_ids")
        if product_id not in self.products:
            raise NotFound()
        return [f"{product_id}-v1"]

    def create_discount(self, *, code: str, amount: int, amount_type: str, variant_ids: List[str], expires_at: Optional[datetime]) -> Dict[str, Any]:
        self._gate("create_discount")
        self.writes.append(("discount", code, amount, amount_type, tuple(variant_ids), expires_at))
        return {"id": "d1", "attributes": {"code": code}}
