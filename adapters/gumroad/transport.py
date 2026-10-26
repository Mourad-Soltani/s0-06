"""The thin seam between the adapter and Gumroad. GumroadTransport is what the adapter needs; HttpGumroadTransport
implements it over Gumroad's REST API (api.gumroad.com/v2). The spec prefers wrapping an MCP server, which can
implement this same protocol later. Endpoint paths and offer-code parameter names are from third-party
documentation and are UNVERIFIED (open item S0-03). The access token never appears in an error or a log line."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Protocol

from adapters.errors import (AdapterError, AuthExpired, MalformedResponse, NotFound, RateLimited, Rejected,
                             TransportError)


class GumroadTransport(Protocol):
    def ping(self) -> None: ...
    def get_products(self) -> List[Dict[str, Any]]: ...
    def get_product(self, product_id: str) -> Dict[str, Any]: ...
    def create_offer_code(self, product_id: str, *, name: str, percent_off: Optional[int] = None,
                          amount_off_cents: Optional[int] = None) -> Dict[str, Any]: ...
    def get_sales(self, after: date) -> List[Dict[str, Any]]: ...


class HttpGumroadTransport:
    BASE = "https://api.gumroad.com"

    def __init__(self, access_token: str, *, opener: Optional[Callable] = None, timeout: float = 15.0,
                 max_pages: int = 100):
        if not access_token:
            raise ValueError("an access token is required")
        self._token, self._timeout, self._max_pages = access_token, timeout, max_pages
        self._open = opener or urllib.request.urlopen

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, path: str, form: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.BASE + path, method=method, data=urllib.parse.urlencode(form).encode() if form else None,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
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
        except (ValueError, UnicodeDecodeError):
            raise MalformedResponse("invalid json") from None
        if not isinstance(body, dict):
            raise MalformedResponse("response is not an object")
        if body.get("success") is False:
            raise Rejected()
        return body

    @staticmethod
    def _field(body: Dict[str, Any], key: str, kind):
        if not isinstance(body.get(key), kind):
            raise MalformedResponse(f"missing or wrong type for {key}")
        return body[key]

    # ------------------------------------------------------------------ the protocol
    def ping(self) -> None:
        self._call("GET", "/v2/user")

    def get_products(self) -> List[Dict[str, Any]]:
        return self._field(self._call("GET", "/v2/products"), "products", list)

    def get_product(self, product_id: str) -> Dict[str, Any]:
        return self._field(self._call("GET", "/v2/products/" + urllib.parse.quote(product_id, safe="")),
                           "product", dict)

    def create_offer_code(self, product_id: str, *, name: str, percent_off: Optional[int] = None,
                          amount_off_cents: Optional[int] = None) -> Dict[str, Any]:
        if (percent_off is None) == (amount_off_cents is None):
            raise ValueError("exactly one of percent_off and amount_off_cents is required")
        form: Dict[str, Any] = {"name": name}
        form["percent_off" if percent_off is not None else "amount_off"] = percent_off if percent_off is not None else amount_off_cents
        path = "/v2/products/" + urllib.parse.quote(product_id, safe="") + "/offer_codes"
        return self._field(self._call("POST", path, form), "offer_code", dict)

    def get_sales(self, after: date) -> List[Dict[str, Any]]:
        sales: List[Dict[str, Any]] = []
        path: Optional[str] = "/v2/sales?" + urllib.parse.urlencode({"after": after.isoformat()})
        for _ in range(self._max_pages):
            body = self._call("GET", path)
            sales.extend(self._field(body, "sales", list))
            path = body.get("next_page_url")
            if not path:
                return sales
            if not isinstance(path, str) or not path.startswith("/v2/"):      # never follow a link off this API
                raise MalformedResponse("unexpected next_page_url")
        raise MalformedResponse("too many pages")


def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    try:
        value = float(exc.headers.get("Retry-After")) if exc.headers is not None else None
    except (TypeError, ValueError):
        return None
    return value if value and value > 0 else None
