"""The review queue's HTTP API (S2-05, spec 5.3) as a plain WSGI application: standard library only, so it needs no
framework decision and runs under any WSGI server. Endpoints (all JSON, all require a bearer token):
  GET  /review/actions[?status=PROPOSED|APPROVAL_PENDING][&marketplace=ETSY][&limit=50]   pending proposals
  GET  /review/actions/{id}                       one proposal with its message body (an addition to the spec)
  POST /review/actions/{id}/approve
  POST /review/actions/{id}/reject                {"reason_code": "TONE_OFF"}
  POST /review/actions/{id}/edit                  {"patched_payload": {...}}
Authentication is a static token per reviewer, compared in constant time; the token decides the reviewer id, so the
body never names one. With no tokens configured the app will not start (fail closed). TLS and real identity (SSO)
are deployment concerns: bind to localhost and put a TLS proxy in front. Errors are fixed codes: never a message,
a body or a stack trace."""
from __future__ import annotations

import hmac
import http
import json
import re
import urllib.parse
import uuid
from typing import Callable, ContextManager, Mapping

from orchestrator.review_service import ReviewError

_REVIEWER = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_ROUTE = re.compile(r"^/review/actions(?:/(?P<id>[^/]+)(?:/(?P<verb>approve|reject|edit))?)?$")
MAX_BODY = 65_536
_STATUS = {"NOT_FOUND": 404, "NOT_PENDING": 409, "NOT_APPROVABLE": 409, "EDIT_UNAVAILABLE": 501}


class _Http(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


class ReviewApi:
    def __init__(self, queue_factory: Callable[[], ContextManager], tokens: Mapping[str, str]):
        if not tokens:
            raise ValueError("no review tokens configured: refusing to start an unauthenticated review API")
        for token, reviewer in tokens.items():
            if len(token) < 16 or not _REVIEWER.match(reviewer):
                raise ValueError("tokens must be at least 16 characters and map to opaque reviewer ids")
        self._factory, self._tokens = queue_factory, dict(tokens)

    # ------------------------------------------------------------------ WSGI
    def __call__(self, environ, start_response):
        try:
            status, body = self._dispatch(environ)
        except _Http as exc:
            status, body = exc.status, {"error": exc.code}
        except ReviewError as exc:
            status, body = _STATUS.get(exc.code, 409), {"error": exc.code}
        except ValueError:
            status, body = 400, {"error": "INVALID_REQUEST"}
        except Exception:  # noqa: BLE001 - never leak detail
            status, body = 500, {"error": "INTERNAL"}
        data = json.dumps(body).encode("utf-8")
        headers = [("Content-Type", "application/json"), ("Content-Length", str(len(data))), ("Cache-Control", "no-store")]
        if status == 401:
            headers.append(("WWW-Authenticate", "Bearer"))
        start_response(f"{status} {http.HTTPStatus(status).phrase}", headers)
        return [data]

    # ------------------------------------------------------------------ routing
    def _dispatch(self, environ):
        reviewer = self._authenticate(environ)                      # before routing: unknown paths need a token too
        m = _ROUTE.match(environ.get("PATH_INFO", ""))
        if m is None:
            raise _Http(404, "NOT_FOUND")
        method, pid, verb = environ.get("REQUEST_METHOD", "GET"), m.group("id"), m.group("verb")
        if pid is None:
            if method != "GET":
                raise _Http(405, "METHOD_NOT_ALLOWED")
            return 200, self._list(environ)
        try:
            proposal_id = uuid.UUID(pid)
        except ValueError:
            raise _Http(404, "NOT_FOUND") from None
        if verb is None:
            if method != "GET":
                raise _Http(405, "METHOD_NOT_ALLOWED")
            with self._factory() as q:
                found = q.get(proposal_id)
            if found is None:
                raise _Http(404, "NOT_FOUND")
            return 200, {"action": found}
        if method != "POST":
            raise _Http(405, "METHOD_NOT_ALLOWED")
        body = self._json_body(environ)
        with self._factory() as q:
            if verb == "approve":
                q.approve(proposal_id, reviewer)
                return 200, {"status": "APPROVED"}
            if verb == "reject":
                q.reject(proposal_id, reviewer, body.get("reason_code"))
                return 200, {"status": "REJECTED"}
            q.edit(proposal_id, reviewer, body.get("patched_payload"))
            return 200, {"status": "PROPOSED", "compliance_verdict": None, "note": "re-evaluation required before approval"}

    def _list(self, environ):
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        status = qs.get("status", ["PROPOSED"])[0]
        if status not in ("PROPOSED", "APPROVAL_PENDING"):         # the spec's name for the same queue
            raise _Http(400, "INVALID_REQUEST")
        marketplace = qs.get("marketplace", [None])[0]
        try:
            limit = int(qs.get("limit", ["50"])[0])
        except ValueError:
            raise _Http(400, "INVALID_REQUEST") from None
        with self._factory() as q:
            return {"actions": [a.as_dict() for a in q.list_pending(limit=limit, marketplace=marketplace)]}

    # ------------------------------------------------------------------ helpers
    def _authenticate(self, environ) -> str:
        header = environ.get("HTTP_AUTHORIZATION", "")
        provided = header[7:] if header.startswith("Bearer ") else ""
        found = None
        for token, reviewer in self._tokens.items():               # no early exit: constant work per request
            if hmac.compare_digest(provided.encode("utf-8"), token.encode("utf-8")) and provided:
                found = reviewer
        if found is None:
            raise _Http(401, "UNAUTHENTICATED")
        return found

    @staticmethod
    def _json_body(environ) -> dict:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise _Http(400, "INVALID_REQUEST") from None
        if length > MAX_BODY:
            raise _Http(413, "PAYLOAD_TOO_LARGE")
        if length == 0:
            return {}
        if not environ.get("CONTENT_TYPE", "").startswith("application/json"):
            raise _Http(415, "UNSUPPORTED_MEDIA_TYPE")
        try:
            body = json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _Http(400, "INVALID_REQUEST") from None
        if not isinstance(body, dict):
            raise _Http(400, "INVALID_REQUEST")
        return body
