"""HTTP client for the review API (S2-05), standard library only. The token is held privately: it never appears in a repr,
an error or a message. A remote address must use https (a bearer token must not travel in the clear); plain http is for
localhost only. Redirects are NOT followed: a redirect could carry the token to another host. Errors are fixed codes."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

_LOCAL = {"localhost", "127.0.0.1", "::1"}
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")


class ReviewApiError(Exception):
    def __init__(self, code: str, status: Optional[int] = None):
        super().__init__(code)
        self.code, self.status = code, status


class AuthError(ReviewApiError):
    def __init__(self):
        super().__init__("UNAUTHENTICATED", 401)


class Unreachable(ReviewApiError):
    def __init__(self):
        super().__init__("UNREACHABLE")


def validate_base_url(url) -> str:
    """Messages are fixed text: they are shown to the person typing the address."""
    if not isinstance(url, str):
        raise ValueError("The address must be text.")
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("The address must start with https:// (or http:// for localhost).")
    if u.username or u.password or u.query or u.fragment:
        raise ValueError("The address must not contain credentials, a query or a fragment.")
    if u.scheme == "http" and u.hostname not in _LOCAL:
        raise ValueError("A remote address must use https so the token is not sent in the clear.")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                                   # the 3xx surfaces as an error instead of being followed


def _default_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


def _uuid(proposal_id) -> str:
    try:
        return str(uuid.UUID(str(proposal_id)))
    except ValueError:
        raise ValueError("invalid proposal id") from None


class ReviewClient:
    def __init__(self, base_url: str, token: str, *, opener: Optional[Callable] = None, timeout: float = 10.0):
        self.base_url = validate_base_url(base_url)
        if not isinstance(token, str) or not token.strip():
            raise ValueError("A token is required.")
        self._token, self._open, self._timeout = token.strip(), opener or _default_open, timeout

    def __repr__(self) -> str:
        return f"ReviewClient({self.base_url!r})"

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Unreachable() from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ReviewApiError("BAD_RESPONSE") from None
        if not isinstance(parsed, dict):
            raise ReviewApiError("BAD_RESPONSE")
        return parsed

    # ------------------------------------------------------------------ the API
    def list_actions(self, limit: int = 50, marketplace: Optional[str] = None) -> List[Dict[str, Any]]:
        query = {"limit": str(limit)}
        if marketplace:
            query["marketplace"] = marketplace
        actions = self._call("GET", "/review/actions?" + urllib.parse.urlencode(query)).get("actions")
        if not isinstance(actions, list):
            raise ReviewApiError("BAD_RESPONSE")
        return actions

    def get_action(self, proposal_id) -> Dict[str, Any]:
        action = self._call("GET", f"/review/actions/{_uuid(proposal_id)}").get("action")
        if not isinstance(action, dict):
            raise ReviewApiError("BAD_RESPONSE")
        return action

    def approve(self, proposal_id) -> None:
        self._call("POST", f"/review/actions/{_uuid(proposal_id)}/approve", {})

    def reject(self, proposal_id, reason_code: str) -> None:
        self._call("POST", f"/review/actions/{_uuid(proposal_id)}/reject", {"reason_code": reason_code})

    def edit(self, proposal_id, patched_payload: dict) -> None:
        self._call("POST", f"/review/actions/{_uuid(proposal_id)}/edit", {"patched_payload": patched_payload})


def _http_error(exc: urllib.error.HTTPError) -> ReviewApiError:
    if exc.code == 401:
        return AuthError()
    code = None
    try:
        body = json.loads(exc.read().decode("utf-8"))
        candidate = body.get("error") if isinstance(body, dict) else None
        code = candidate if isinstance(candidate, str) and _CODE.match(candidate) else None
    except Exception:  # noqa: BLE001 - an unreadable error body is just an unknown error
        pass
    return ReviewApiError(code or f"HTTP_{exc.code}", exc.code)
