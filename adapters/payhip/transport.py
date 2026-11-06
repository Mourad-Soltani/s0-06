"""Payhip over HTTP. Documented: base https://payhip.com/api/v2; the key goes in the `payhip-api-key` header; form-encoded requests, JSON
responses with a `data` object; the API covers ONLY coupons and license keys (no products, no sales). Payhip publishes no numeric rate limit,
so the lane in the rate governor is our own conservative choice. NOT verified against the live API (open item): the coupon field names and
the 429 behaviour. The key never appears in an error. Redirects are not followed."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Protocol

from adapters.errors import AuthExpired, MalformedResponse, NotFound, RateLimited, Rejected, TransportError

BASE = "https://payhip.com"
CONSERVATIVE_BUCKET = {"capacity": 30, "refill_per_second": 0.5}      # ours, not Payhip's: 30 in a burst, then one every two seconds


class PayhipTransport(Protocol):
    def ping(self) -> None: ...
    def create_coupon(self, *, code: str, product_key: str, percent_off: int, usage_limit: Optional[int] = None) -> Dict[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _default_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


class HttpPayhipTransport:
    def __init__(self, api_key: str, *, opener: Optional[Callable] = None, timeout: float = 15.0):
        if not api_key:
            raise ValueError("an API key is required")
        self._key, self._timeout, self._open = api_key, timeout, opener or _default_open

    def __repr__(self) -> str:
        return "HttpPayhipTransport()"

    def _call(self, method: str, path: str, form: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(form).encode("utf-8") if form else None, method=method,
                                     headers={"payhip-api-key": self._key, "Accept": "application/json"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthExpired() from None
            if exc.code == 404:
                raise NotFound() from None
            if exc.code == 429:
                raise RateLimited(_retry_after(exc)) from None
            raise Rejected(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportError() from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise MalformedResponse("invalid json") from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), (dict, list)):    # a list endpoint answers with a list
            raise MalformedResponse("missing field data")
        return parsed

    def ping(self) -> None:
        self._call("GET", "/api/v2/coupons?limit=1")                      # the cheapest authenticated call this API has

    def create_coupon(self, *, code: str, product_key: str, percent_off: int, usage_limit: Optional[int] = None) -> Dict[str, Any]:
        form: Dict[str, Any] = {"code": code, "coupon_type": "single", "product_key": product_key, "percent_off": percent_off}
        if usage_limit is not None:
            form["usage_limit"] = usage_limit
        data = self._call("POST", "/api/v2/coupons", form)["data"]
        if not isinstance(data, dict):
            raise MalformedResponse("missing field data")
        return data


def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    try:
        value = float(exc.headers.get("Retry-After")) if exc.headers is not None else None
    except (TypeError, ValueError):
        return None
    return value if value and value > 0 else None
