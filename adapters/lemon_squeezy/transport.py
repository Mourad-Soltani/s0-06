"""Lemon Squeezy over HTTP (JSON:API). From the documentation (and so reasonably trusted): base https://api.lemonsqueezy.com/v1; Bearer key;
`Accept` and `Content-Type: application/vnd.api+json` on every request; 300 requests a minute; X-Ratelimit-Limit and X-Ratelimit-Remaining on
responses; 429 when exceeded. NOT verified against the live API (open item): the exact attribute names, the discount body, pagination links.
The key never appears in an error. Redirects are not followed."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from adapters.errors import AuthExpired, MalformedResponse, NotFound, RateLimited, Rejected, TransportError

BASE = "https://api.lemonsqueezy.com"
DEFAULT_RETRY_SECONDS = 60.0              # the limit is per minute and no Retry-After is documented


class LemonSqueezyTransport(Protocol):
    def ping(self) -> None: ...
    def get_products(self) -> List[Dict[str, Any]]: ...
    def get_product(self, product_id: str) -> Dict[str, Any]: ...
    def get_variant_ids(self, product_id: str) -> List[str]: ...
    def create_discount(self, *, code: str, amount: int, amount_type: str, variant_ids: List[str], expires_at: Optional[datetime]) -> Dict[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _default_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


class HttpLemonSqueezyTransport:
    def __init__(self, api_key: str, store_id, *, opener: Optional[Callable] = None, timeout: float = 15.0, max_pages: int = 50):
        if not api_key:
            raise ValueError("an API key is required")
        self._key, self.store_id, self._timeout, self._max_pages = api_key, str(store_id), timeout, max_pages
        self._open = opener or _default_open
        self.rate_limit_remaining: Optional[int] = None       # from the last response: lets a governor slow down before a 429
        self._currency: Optional[str] = None

    def __repr__(self) -> str:
        return f"HttpLemonSqueezyTransport(store={self.store_id!r})"

    def _call(self, method: str, path_or_url: str, body: Optional[dict] = None) -> Dict[str, Any]:
        url = path_or_url if path_or_url.startswith("https://") else BASE + path_or_url
        if not url.startswith(BASE + "/v1/"):
            raise MalformedResponse("unexpected link")                                      # never follow a link off this API
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/vnd.api+json", "Content-Type": "application/vnd.api+json"}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None, method=method, headers=headers)
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read()
                self.rate_limit_remaining = _int(getattr(resp, "headers", {}).get("X-Ratelimit-Remaining") if getattr(resp, "headers", None) is not None else None)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthExpired() from None
            if exc.code == 404:
                raise NotFound() from None
            if exc.code == 429:
                self.rate_limit_remaining = 0
                raise RateLimited(_retry_after(exc)) from None
            raise Rejected(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportError() from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise MalformedResponse("invalid json") from None
        if not isinstance(parsed, dict):
            raise MalformedResponse("response is not an object")
        return parsed

    def _pages(self, path: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        nxt: Optional[str] = path
        for _ in range(self._max_pages):
            body = self._call("GET", nxt)
            data = body.get("data")
            if not isinstance(data, list):
                raise MalformedResponse("missing field data")
            items.extend(data)
            nxt = (body.get("links") or {}).get("next") if isinstance(body.get("links"), dict) else None
            if not nxt:
                return items
        raise MalformedResponse("too many pages")

    def _currency_code(self) -> str:
        if self._currency is None:
            attrs = (self._call("GET", f"/v1/stores/{urllib.parse.quote(self.store_id, safe='')}").get("data") or {}).get("attributes") or {}
            if not isinstance(attrs.get("currency"), str):
                raise MalformedResponse("missing field currency")
            self._currency = attrs["currency"]
        return self._currency

    def _normalise(self, item: Dict[str, Any]) -> Dict[str, Any]:
        a = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(a, dict) or "id" not in item:
            raise MalformedResponse("missing field attributes")
        return {"id": str(item["id"]), "name": a.get("name"), "description": a.get("description"), "status": a.get("status"),
                "price": a.get("price"), "buy_now_url": a.get("buy_now_url"), "currency": self._currency_code()}

    # ------------------------------------------------------------------ the protocol
    def ping(self) -> None:
        self._call("GET", "/v1/users/me")

    def get_products(self) -> List[Dict[str, Any]]:
        q = urllib.parse.urlencode({"filter[store_id]": self.store_id, "page[size]": "100"})
        return [self._normalise(i) for i in self._pages("/v1/products?" + q)]

    def get_product(self, product_id: str) -> Dict[str, Any]:
        return self._normalise(self._call("GET", "/v1/products/" + urllib.parse.quote(product_id, safe="")).get("data"))

    def get_variant_ids(self, product_id: str) -> List[str]:
        q = urllib.parse.urlencode({"filter[product_id]": product_id, "page[size]": "100"})
        return [str(i["id"]) for i in self._pages("/v1/variants?" + q) if isinstance(i, dict) and "id" in i]

    def create_discount(self, *, code: str, amount: int, amount_type: str, variant_ids: List[str], expires_at: Optional[datetime]) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {"name": code, "code": code, "amount": amount, "amount_type": amount_type, "is_limited_to_products": True}
        if expires_at is not None:
            attrs["expires_at"] = expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        body = {"data": {"type": "discounts", "attributes": attrs, "relationships": {
            "store": {"data": {"type": "stores", "id": self.store_id}},
            "variants": {"data": [{"type": "variants", "id": v} for v in variant_ids]}}}}
        data = self._call("POST", "/v1/discounts", body).get("data")
        if not isinstance(data, dict):
            raise MalformedResponse("missing field data")
        return data


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _retry_after(exc: urllib.error.HTTPError) -> float:
    try:
        value = float(exc.headers.get("Retry-After")) if exc.headers is not None else None
    except (TypeError, ValueError):
        value = None
    return value if value and value > 0 else DEFAULT_RETRY_SECONDS
