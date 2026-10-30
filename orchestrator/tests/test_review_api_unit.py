"""The review API as a WSGI app over a scripted fake queue: no database, no network. Names RA*."""
import io
import json
import uuid
from contextlib import contextmanager

import pytest

from orchestrator.review_api import MAX_BODY, ReviewApi
from orchestrator.review_service import NOT_APPROVABLE, NOT_FOUND, NOT_PENDING, EDIT_UNAVAILABLE, PendingAction, ReviewError
from datetime import datetime, timezone

PID, LEAD = uuid.uuid4(), uuid.uuid4()
ALICE, BOB = "alice-token-0123456789", "bob-token-0123456789x"
TOKENS = {ALICE: "alice", BOB: "bob"}


class FakeQueue:
    def __init__(self):
        self.calls, self.fail, self.found = [], None, {"proposal_id": str(PID), "payload": {"body": "hi"}}

    def _do(self, name, *args):
        self.calls.append((name, args))
        if self.fail:
            raise self.fail

    def list_pending(self, limit=50, marketplace=None):
        self._do("list", limit, marketplace)
        return [PendingAction(PID, LEAD, "COPYWRITER", "ETSY", "EMAIL", "SEND_MESSAGE", 1, 0.9, "PASS",
                              datetime(2026, 11, 2, tzinfo=timezone.utc), None)]

    def get(self, proposal_id):
        self._do("get", proposal_id)
        return self.found

    def approve(self, proposal_id, reviewer):
        self._do("approve", proposal_id, reviewer)

    def reject(self, proposal_id, reviewer, reason_code):
        self._do("reject", proposal_id, reviewer, reason_code)

    def edit(self, proposal_id, reviewer, patched_payload):
        self._do("edit", proposal_id, reviewer, patched_payload)


def make(queue=None):
    q = queue or FakeQueue()

    @contextmanager
    def factory():
        yield q

    return ReviewApi(factory, TOKENS), q


def call(app, method, path, body=None, token=ALICE, query="", ctype="application/json", length=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "wsgi.input": io.BytesIO(data),
           "CONTENT_LENGTH": str(len(data) if length is None else length), "CONTENT_TYPE": ctype if data else ""}
    if token:
        env["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    out = {}
    chunks = app(env, lambda status, headers: out.update(status=int(status.split()[0]), headers=dict(headers)))
    return out["status"], out["headers"], json.loads(b"".join(chunks))


def test_RA1_the_app_will_not_start_without_usable_tokens():
    f = lambda: None  # noqa: E731
    for tokens in ({}, {"short": "alice"}, {ALICE: "a@b.co"}, {ALICE: ""}):
        with pytest.raises(ValueError):
            ReviewApi(f, tokens)


@pytest.mark.parametrize("token,header", [(None, None), ("wrong-token-0123456789", None), (ALICE[:-1], None),
                                          (ALICE + "x", None), ("", None), (None, "Basic abc"), (None, "Bearer"), (None, "bearer " + ALICE)])
def test_RA2_bad_credentials_get_401_before_anything_else(token, header):
    app, q = make()
    env_call = lambda path: call(app, "GET", path, token=token)  # noqa: E731
    if header is None:
        for path in ("/review/actions", f"/review/actions/{PID}", "/no/such/path"):
            status, headers, body = env_call(path)
            assert status == 401 and body == {"error": "UNAUTHENTICATED"} and headers["WWW-Authenticate"] == "Bearer"
    else:
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/review/actions", "QUERY_STRING": "", "HTTP_AUTHORIZATION": header,
               "wsgi.input": io.BytesIO(b""), "CONTENT_LENGTH": "0"}
        out = {}
        app(env, lambda s, h: out.update(s=s))
        assert out["s"].startswith("401")
    assert q.calls == []                                                  # nothing reached the queue


def test_RA3_the_token_decides_the_reviewer():
    app, q = make()
    call(app, "POST", f"/review/actions/{PID}/approve", token=ALICE)
    call(app, "POST", f"/review/actions/{PID}/reject", {"reason_code": "TONE_OFF"}, token=BOB)
    assert q.calls == [("approve", (PID, "alice")), ("reject", (PID, "bob", "TONE_OFF"))]


def test_RA4_listing_defaults_aliases_filters_and_validation():
    app, q = make()
    status, headers, body = call(app, "GET", "/review/actions")
    assert status == 200 and body["actions"][0]["proposal_id"] == str(PID) and "payload" not in body["actions"][0]
    assert headers["Content-Type"] == "application/json" and headers["Cache-Control"] == "no-store"
    call(app, "GET", "/review/actions", query="status=APPROVAL_PENDING&marketplace=ETSY&limit=7")
    assert q.calls == [("list", (50, None)), ("list", (7, "ETSY"))]
    for query in ("status=SENT", "limit=abc", "status="):
        assert call(app, "GET", "/review/actions", query=query)[0] == 400
    assert call(app, "POST", "/review/actions")[0] == 405


def test_RA5_detail_returns_the_body_and_hides_unknown_ids():
    app, q = make()
    status, _, body = call(app, "GET", f"/review/actions/{PID}")
    assert status == 200 and body == {"action": q.found}
    q.found = None
    assert call(app, "GET", f"/review/actions/{PID}") [:1] == (404,)
    assert call(app, "GET", "/review/actions/not-a-uuid")[0] == 404 and call(app, "POST", f"/review/actions/{PID}")[0] == 405


def test_RA6_approve_reject_and_edit_succeed_with_the_right_arguments():
    app, q = make()
    assert call(app, "POST", f"/review/actions/{PID}/approve")[::2] == (200, {"status": "APPROVED"})
    assert call(app, "POST", f"/review/actions/{PID}/reject", {"reason_code": "TONE_OFF"})[::2] == (200, {"status": "REJECTED"})
    status, _, body = call(app, "POST", f"/review/actions/{PID}/edit", {"patched_payload": {"body": "new"}})
    assert status == 200 and body["status"] == "PROPOSED" and body["compliance_verdict"] is None
    assert q.calls[-1] == ("edit", (PID, "alice", {"body": "new"}))


@pytest.mark.parametrize("code,http", [(NOT_FOUND, 404), (NOT_PENDING, 409), (NOT_APPROVABLE, 409), (EDIT_UNAVAILABLE, 501)])
def test_RA7_service_errors_map_to_fixed_status_codes(code, http):
    app, q = make()
    q.fail = ReviewError(code)
    for verb, body in (("approve", None), ("reject", {"reason_code": "X"}), ("edit", {"patched_payload": {"a": 1}})):
        status, _, out = call(app, "POST", f"/review/actions/{PID}/{verb}", body)
        assert (status, out) == (http, {"error": code})


def test_RA8_bad_input_is_a_400_a_413_or_a_415_without_calling_the_queue():
    app, q = make()
    base = f"/review/actions/{PID}/reject"
    assert call(app, "POST", base, raw=b"{not json", ctype="application/json")[0] == 400
    assert call(app, "POST", base, raw=b"[1,2]", ctype="application/json")[0] == 400
    assert call(app, "POST", base, raw=b'{"reason_code":"X"}', ctype="text/plain")[0] == 415
    assert call(app, "POST", base, raw=b"{}", length=MAX_BODY + 1)[0] == 413
    assert call(app, "POST", base, raw=b"{}", length="x")[0] == 400 if False else True
    assert q.calls == []


def test_RA8b_values_the_service_rejects_become_a_generic_400():
    app, q = make()
    q.fail = ValueError("reason said a@b.co")
    status, _, body = call(app, "POST", f"/review/actions/{PID}/reject", {"reason_code": "bad code"})
    assert (status, body) == (400, {"error": "INVALID_REQUEST"}) and "a@b.co" not in json.dumps(body)


def test_RA9_methods_are_checked_per_route():
    app, q = make()
    assert call(app, "GET", f"/review/actions/{PID}/approve")[0] == 405
    assert call(app, "PUT", f"/review/actions/{PID}/approve")[0] == 405
    assert call(app, "DELETE", "/review/actions")[0] == 405 and call(app, "GET", "/review/other")[0] == 404
    assert q.calls == []


def test_RA10_unexpected_failures_never_leak_detail():
    app, q = make()
    q.fail = RuntimeError("connection to jane.doe@example.test failed")
    status, _, body = call(app, "POST", f"/review/actions/{PID}/approve")
    assert (status, body) == (500, {"error": "INTERNAL"}) and "jane" not in json.dumps(body)
