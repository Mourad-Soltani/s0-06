"""The webhook receiver (S4-01, S4-02): one small WSGI app, standard library only, for every provider that pushes signals. A request is
POST /webhooks/<name>/<token>. In this order: the route and the secret path token (constant time; a wrong name or token is a plain 404, so
nothing is revealed), the body size, the provider's own signature check (inside the handler), then the signals go into the inbox. The path token
matters most for Payhip, whose signature is a constant: the secret URL is what makes a forged request hard. Answers are fixed codes:
200 for anything accepted or deliberately ignored (so providers stop retrying), 401 bad signature, 400 bad payload, 413 too large, 404 unknown
route, 405 wrong method, 500 internal (the provider will retry). The buyer's address is in the payload but never leaves the handler. Bind it to
localhost behind a TLS proxy."""
from __future__ import annotations

import hmac
import http
import json
import re
from typing import Mapping, Tuple

from adapters.lemon_squeezy.webhook import WebhookRejected

MAX_BODY = 262_144
_ROUTE = re.compile(r"^/webhooks/(?P<name>[a-z]{1,32})/(?P<token>[A-Za-z0-9_-]{16,128})$")
_STATUS = {"BAD_SIGNATURE": 401}


class WebhookApp:
    def __init__(self, inbox, handlers: Mapping[str, Tuple[object, str]]):
        if not handlers:
            raise ValueError("no webhook handlers configured")
        for name, (handler, token) in handlers.items():
            if not _ROUTE.match(f"/webhooks/{name}/{token}") or getattr(handler, "name", None) != name:
                raise ValueError("each handler needs a lowercase name and a secret path token of at least 16 characters")
        self._inbox, self._handlers = inbox, dict(handlers)

    def __call__(self, environ, start_response):
        try:
            status, body = self._dispatch(environ)
        except Exception:  # noqa: BLE001 - never leak detail; the provider retries a 500
            status, body = 500, {"error": "INTERNAL"}
        data = json.dumps(body).encode("utf-8")
        start_response(f"{status} {http.HTTPStatus(status).phrase}", [("Content-Type", "application/json"), ("Content-Length", str(len(data))),
                                                                   ("Cache-Control", "no-store")])
        return [data]

    def _dispatch(self, environ):
        m = _ROUTE.match(environ.get("PATH_INFO", ""))
        entry = self._handlers.get(m.group("name")) if m else None
        if m is None or entry is None or not hmac.compare_digest(m.group("token").encode("ascii"), entry[1].encode("ascii")):
            return 404, {"error": "NOT_FOUND"}
        if environ.get("REQUEST_METHOD") != "POST":
            return 405, {"error": "METHOD_NOT_ALLOWED"}
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return 400, {"error": "INVALID_PAYLOAD"}
        if length > MAX_BODY:
            return 413, {"error": "PAYLOAD_TOO_LARGE"}
        if length <= 0:
            return 400, {"error": "INVALID_PAYLOAD"}
        body = environ["wsgi.input"].read(length)
        headers = {k[5:].lower().replace("_", "-"): v for k, v in environ.items() if k.startswith("HTTP_")}
        try:
            parsed = entry[0].handle(headers, body)
        except WebhookRejected as exc:
            return _STATUS.get(exc.code, 400), {"error": exc.code}
        new = sum(1 for p in parsed if self._inbox.write(p.signal, p.attributed_lead))
        return 200, {"ok": True, "accepted": new, "duplicates": len(parsed) - new}
