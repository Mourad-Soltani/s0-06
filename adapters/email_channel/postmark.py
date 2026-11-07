"""Postmark transport for the email channel (cutover kit 1, ADR-27). It implements the EmailTransport protocol and nothing else changes:
the adapter still composes the footer and the List-Unsubscribe header, the Closer still claims before it sends, the database gate still decides
who may be contacted.

What this class does and refuses to do:
 * The adapter hands over an opaque thread id (the contact's id). An injected resolver turns it into an address at the last moment, so the address
   exists in memory for one call and never in an event, a log or an exception. A contact that cannot be resolved raises NotFound: nothing is sent.
 * Link tracking and open tracking are OFF. Postmark would otherwise rewrite the checkout link to its own domain, which changes the host the Guard
   approved and drops the attributed lead id (ADR-23, ADR-25).
 * The idempotency key travels as message metadata. Postmark has no idempotency header, so a retried HTTP call is NOT deduplicated by Postmark; the
   Closer's claim-before-send is what prevents a second send. `lookup` (asking Postmark whether a message with that key exists) is OFF unless
   explicitly enabled: it relies on Postmark's message search, which is eventually consistent, and a false "never accepted" would let the
   reconciler release a message that did go out. Enable it only after the first live check (cutover C3).
 * Errors carry no response text: an HTTP error body can contain the recipient's address, so it is never read. 401/403 -> AuthExpired, 429 ->
   RateLimited, 404 -> NotFound, 5xx and network trouble -> TransportError (the outcome is unknown), anything else -> Rejected(status).
 * Inbound replies are not supported yet (list_threads is empty, read_thread raises NotFound): v1.1 only sends.
Unverified against the live service (no network access when this was written): the exact message-search parameter for metadata, and the response
shapes below. The unit tests pin what this code sends and expects, not what Postmark does."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from adapters.email_channel.message import OutboundEmail
from adapters.errors import AuthExpired, LookupUnsupported, MalformedResponse, NotFound, RateLimited, Rejected, TransportError

BASE = "https://api.postmarkapp.com"
_STREAM = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_KEY = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_FROM = re.compile(r"^[^@\s<>\r\n]+@[^@\s<>\r\n]+\.[^@\s<>\r\n]+$")
METADATA_KEY = "idem"
AddressResolver = Callable[[str], Optional[str]]


def _default_open(req, timeout):
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None                                     # the token must never follow a redirect to another host
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    try:
        return float(exc.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None


class PostmarkEmailTransport:
    def __init__(self, server_token: str, from_address: str, resolve: AddressResolver, *, message_stream: str = "outbound",
                 enable_lookup: bool = False, opener: Optional[Callable] = None, timeout: float = 15.0):
        if not isinstance(server_token, str) or not server_token.strip():
            raise ValueError("a Postmark server token is required")
        if not isinstance(from_address, str) or not _FROM.match(from_address):
            raise ValueError("from_address must be a plain address such as hello@example.com")
        if not _STREAM.match(message_stream or ""):
            raise ValueError("message_stream must be lowercase letters, digits and hyphens")
        self._token, self._from, self._resolve = server_token.strip(), from_address, resolve
        self._stream, self._timeout, self._open = message_stream, timeout, opener or _default_open
        self.supports_lookup = bool(enable_lookup)

    def __repr__(self) -> str:                                # never the token, never the sender address
        return f"PostmarkEmailTransport(stream={self._stream!r}, lookup={self.supports_lookup})"

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method, headers={
            "X-Postmark-Server-Token": self._token, "Accept": "application/json", **({"Content-Type": "application/json"} if data else {})})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:                 # the body is deliberately not read
            if exc.code in (401, 403):
                raise AuthExpired() from None
            if exc.code == 404:
                raise NotFound() from None
            if exc.code == 429:
                raise RateLimited(_retry_after(exc)) from None
            if exc.code >= 500:
                raise TransportError() from None
            raise Rejected(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportError() from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise MalformedResponse("invalid json") from None
        if not isinstance(parsed, dict):
            raise MalformedResponse("unexpected response shape")
        return parsed

    # ------------------------------------------------------------------ EmailTransport
    def ping(self) -> None:
        self._call("GET", "/server")                          # authenticated, read-only, and sends nothing

    def list_threads(self, since: datetime) -> List[Dict[str, Any]]:
        return []                                             # replies are not read in v1.1

    def read_thread(self, thread_id: str) -> List[Dict[str, Any]]:
        raise NotFound()

    def send(self, email: OutboundEmail, idempotency_key: str) -> str:
        if not isinstance(idempotency_key, str) or not _KEY.match(idempotency_key):
            raise ValueError("the idempotency key must be an opaque id")
        headers = []
        for name, value in email.headers.items():
            if not _HEADER_NAME.match(str(name)) or any(c in str(value) for c in "\r\n"):
                raise ValueError("a header name or value is not allowed")
            headers.append({"Name": str(name), "Value": str(value)})
        if any(c in email.subject for c in "\r\n"):
            raise ValueError("a subject cannot contain a line break")
        to = self._resolve(email.thread_id)
        if not to or not _FROM.match(to):
            raise NotFound()                                  # no address on record, or not a plain one: nothing is sent
        result = self._call("POST", "/email", {
            "From": self._from, "To": to, "Subject": email.subject, "TextBody": email.text, "MessageStream": self._stream,
            "Headers": headers, "Metadata": {METADATA_KEY: idempotency_key}, "TrackOpens": False, "TrackLinks": "None"})
        message_id = result.get("MessageID")
        if result.get("ErrorCode") not in (0, None) or not isinstance(message_id, str) or not message_id:
            raise MalformedResponse("missing field MessageID")
        return message_id

    def lookup(self, idempotency_key: str) -> Optional[str]:
        if not self.supports_lookup:
            raise LookupUnsupported()
        if not isinstance(idempotency_key, str) or not _KEY.match(idempotency_key):
            raise ValueError("the idempotency key must be an opaque id")
        query = urllib.parse.urlencode({"count": 1, "offset": 0, "messagestream": self._stream, f"metadata_{METADATA_KEY}": idempotency_key})
        result = self._call("GET", f"/messages/outbound?{query}")
        messages = result.get("Messages")
        if not isinstance(messages, list) or not isinstance(result.get("TotalCount"), int):
            raise MalformedResponse("missing field Messages")
        if result["TotalCount"] == 0 or not messages:
            return None
        message_id = messages[0].get("MessageID") if isinstance(messages[0], dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise MalformedResponse("missing field MessageID")
        return message_id
