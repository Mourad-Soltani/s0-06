"""Errors adapters raise. Messages are fixed strings: no marketplace response text, no identifiers, no PII."""
from __future__ import annotations

from typing import Optional


class AdapterError(Exception):
    """Base class for everything an adapter raises on purpose."""


class RateLimited(AdapterError):
    """The marketplace refused the call for rate reasons (HTTP 429 or a cost-based throttle)."""

    def __init__(self, retry_after_seconds: Optional[float] = None):
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds


class AuthExpired(AdapterError):
    """Credentials are no longer valid. The adapter has made no change; the lane should pause."""

    def __init__(self):
        super().__init__("authentication expired")


class NotFound(AdapterError):
    def __init__(self):
        super().__init__("not found")


class DuplicateOfferCode(AdapterError):
    def __init__(self):
        super().__init__("offer code already exists")
