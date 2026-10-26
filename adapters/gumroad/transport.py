from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from adapters.errors import (
    AuthExpired,
    MalformedResponse,
    NotFound,
    RateLimited,
    Rejected,
    TransportError,
)

class HttpGumroadTransport:
    BASE = "https://api.gumroad.com"

    def __init__(self, token: str, opener=None, *, max_pages: int = 100, timeout: float = 30.0):
        if not token or not isinstance(token, str):
            raise ValueError("token is required")
        self._token = token
        self._opener = opener
        self._max_pages = max_pages
        self._timeout = timeout

    def _build_request(self, method: str, path: str, data: dict | None = None):
        url = self.BASE + path
        body = None
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return urllib.request.Request(url, data=body, headers=headers, method=method)

    def _open(self, req):
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(req, timeout=self._timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            self._raise_http(e)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            msg = str(e)
            if self._token in msg:
                msg = "network error"
            raise TransportError(msg) from None

    def _raise_http(self, e):
        code = e.code
        if code in (401, 403):
            raise AuthExpired("authentication expired") from None
        if code == 404:
            raise NotFound("not found") from None
        if code == 429:
            raise RateLimited(self._retry_after(e)) from None
        raise Rejected(code) from None

    def _retry_after(self, e):
        try:
            header = e.headers.get("Retry-After") if e.headers else None
        except Exception:
            return None
        if header is None:
            return None
        try:
            seconds = float(header)
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None

    def _parse(self, body: bytes):
        try:
            data = json.loads(body)
        except Exception:
            raise MalformedResponse("invalid JSON") from None
        if not isinstance(data, dict):
            raise MalformedResponse("expected a JSON object")
        return data

    def _unwrap(self, data: dict, key: str):
        if data.get("success") is not True:
            raise Rejected(None)
        if key not in data:
            raise MalformedResponse(f"missing '{key}'")
        return data[key]

    def get_products(self):
        body = self._open(self._build_request("GET", "/v2/products"))
        products = self._unwrap(self._parse(body), "products")
        if not isinstance(products, list):
            raise MalformedResponse("invalid 'products'")
        return products

    def ping(self):
        body = self._open(self._build_request("GET", "/v2/user"))
        return self._unwrap(self._parse(body), "user")

    def get_product(self, product_id: str):
        pid = urllib.parse.quote(str(product_id), safe="")
        body = self._open(self._build_request("GET", f"/v2/products/{pid}"))
        return self._unwrap(self._parse(body), "product")

    def create_offer_code(self, product_id: str, *, name: str,
                          percent_off: int | None = None,
                          amount_off_cents: int | None = None):
        if (percent_off is None) == (amount_off_cents is None):
            raise ValueError("exactly one of percent_off / amount_off_cents is required")
        payload = {"name": name}
        if percent_off is not None:
            payload["percent_off"] = percent_off
        else:
            payload["amount_off"] = amount_off_cents
        pid = urllib.parse.quote(str(product_id), safe="")
        body = self._open(self._build_request("POST", f"/v2/products/{pid}/offer_codes", data=payload))
        return self._unwrap(self._parse(body), "offer_code")

    def get_sales(self, after_date):
        path = f"/v2/sales?after={after_date.isoformat()}"
        collected = []
        for _ in range(self._max_pages):
            body = self._open(self._build_request("GET", path))
            data = self._parse(body)
            page = self._unwrap(data, "sales")
            if not isinstance(page, list):
                raise MalformedResponse("invalid 'sales'")
            collected.extend(page)
            nxt = data.get("next_page_url")
            if nxt is None:
                return collected
            if not isinstance(nxt, str) or not nxt.startswith("/v2/"):
                raise MalformedResponse("invalid 'next_page_url'")
            path = nxt
        raise MalformedResponse("pagination exceeded max_pages")
