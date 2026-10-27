"""In-memory EmailTransport for tests and contract harnesses."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List

from adapters.email_channel.message import OutboundEmail
from adapters.errors import AuthExpired, NotFound


class FakeEmailTransport:
    def __init__(self):
        self.threads: Dict[str, Dict[str, Any]] = {}
        self.sent: List[OutboundEmail] = []
        self.sent_keys: List[str] = []
        self.calls: List[str] = []
        self._faults: Dict[str, deque] = defaultdict(deque)
        self.auth_expired = False

    def add_thread(self, thread_id: str, context: str, updated_at: str = "2026-10-19T12:00:00Z", messages=()) -> None:
        self.threads[thread_id] = {"thread_id": thread_id, "context": context, "updated_at": updated_at,
                                   "messages": list(messages)}

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

    def list_threads(self, since: datetime) -> List[Dict[str, Any]]:
        self._gate("list_threads")
        return [{k: v for k, v in t.items() if k != "messages"} for t in self.threads.values()
                if t["updated_at"] >= since.strftime("%Y-%m-%dT%H:%M:%SZ")]

    def read_thread(self, thread_id: str) -> List[Dict[str, Any]]:
        self._gate("read_thread")
        if thread_id not in self.threads:
            raise NotFound()
        return list(self.threads[thread_id]["messages"])

    def send(self, email: OutboundEmail, idempotency_key: str) -> str:
        self._gate("send")
        self.sent.append(email)
        self.sent_keys.append(idempotency_key)
        return f"esp-{len(self.sent)}"
