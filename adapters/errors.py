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


class MalformedResponse(AdapterError):
    """The marketplace answered with something the adapter cannot map. The detail names a FIELD, never a value."""

    def __init__(self, detail: str = "malformed response"):
        super().__init__(detail)


class UnsupportedParameter(AdapterError):
    """The operation exists but this marketplace cannot honour one of its parameters (for example an expiry)."""

    def __init__(self, parameter: str):
        super().__init__(f"unsupported parameter: {parameter}")


class TransportError(AdapterError):
    """Network failure or timeout. Nothing is known about whether the request arrived."""

    def __init__(self):
        super().__init__("network error")


class Rejected(AdapterError):
    """The marketplace refused the request (HTTP status other than auth, not-found and rate-limit)."""

    def __init__(self, status: int | None = None):
        super().__init__("request rejected" if status is None else f"request rejected ({status})")
        self.status = status
