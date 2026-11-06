"""In-memory PayhipTransport for tests and contract harnesses."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

from adapters.errors import AuthExpired


class FakePayhipTransport:
    def __init__(self):
        self.calls: List[str] = []
        self.writes: List[tuple] = []
        self._faults: Dict[str, deque] = defaultdict(deque)
        self.auth_expired = False

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

    def create_coupon(self, *, code: str, product_key: str, percent_off: int, usage_limit: Optional[int] = None) -> Dict[str, Any]:
        self._gate("create_coupon")
        self.writes.append(("coupon", code, product_key, percent_off, usage_limit))
        return {"code": code, "product_key": product_key, "percent_off": percent_off}
