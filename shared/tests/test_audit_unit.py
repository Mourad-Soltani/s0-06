"""Audit writer validation and helpers: no database. Names are AU*."""
import secrets

import pytest

from shared.audit import AuditWriter, canonical_bytes, kill_switch_audit_hook


def test_AU1_canonical_bytes_is_stable_and_type_checked():
    assert canonical_bytes({"b": 1, "a": [2, 3]}) == canonical_bytes({"a": [2, 3], "b": 1}) == b'{"a":[2,3],"b":1}'
    assert canonical_bytes({"name": "Jos\u00e9"}) == '{"name":"Jos\u00e9"}'.encode("utf-8")   # no \u escapes
    assert canonical_bytes(b"\x00raw") == b"\x00raw" and canonical_bytes("text") == b"text"
    for bad in (42, [1, 2], None):
        with pytest.raises(TypeError):
            canonical_bytes(bad)


@pytest.mark.parametrize("kw", [
    dict(actor="a@b.co", action_type="NOTE"), dict(actor="", action_type="NOTE"),
    dict(actor="two words", action_type="NOTE"), dict(actor="x" * 129, action_type="NOTE"),
    dict(actor="system", action_type="lowercase"), dict(actor="system", action_type="1STARTS_WITH_DIGIT"),
    dict(actor="system", action_type=""), dict(actor="system", action_type="NOTE", outcome="ok"),
    dict(actor="system", action_type="NOTE", compliance_verdict="MAYBE"),
    dict(actor="system", action_type="NOTE", autonomy_tier=3), dict(actor="system", action_type="NOTE", autonomy_tier=True),
    dict(actor="system", action_type="SEND_MESSAGE"), dict(actor="system", action_type="CREATE_OFFER"),
    dict(actor="system", action_type="REPLY"),
    dict(actor="system", action_type="NOTE", payload={"k": "v"}),                  # no lead: nothing to salt with
    dict(actor="system", action_type="NOTE", external_response=b"bytes")])
def test_AU2_invalid_events_are_refused_before_the_database_is_touched(kw):
    with pytest.raises(ValueError):
        AuditWriter(None).record(**kw)                  # conn=None: reaching the database would raise AttributeError


def test_AU3_salts_come_from_secrets_token_bytes_by_default():
    assert AuditWriter(None)._token_bytes is secrets.token_bytes     # checklist A1
    assert len(AuditWriter(None)._token_bytes(32)) == 32


class SpyWriter:
    def __init__(self):
        self.calls = []

    def record(self, actor, action_type, **kw):
        self.calls.append((actor, action_type, kw))


def test_AU4_kill_switch_hook_records_ids_and_codes_only():
    w = SpyWriter()
    hook = kill_switch_audit_hook(w)
    hook("KILL", {"scope": "GUMROAD", "actor": "ops-1", "reason_code": "INCIDENT", "changed": True,
                  "failed_revocations": 0})
    hook("KILL", {"scope": "GLOBAL", "actor": "ops-1", "reason_code": "INCIDENT", "changed": True,
                  "failed_revocations": 2})
    hook("REVIVE", {"scope": "GUMROAD", "actor": "ops-2", "changed": True})
    assert w.calls == [("ops-1", "KILL_SWITCH_ENGAGED", {"marketplace": "GUMROAD", "outcome": "OK"}),
                       ("ops-1", "KILL_SWITCH_ENGAGED", {"marketplace": None, "outcome": "REVOCATION_FAILED"}),
                       ("ops-2", "KILL_SWITCH_CLEARED", {"marketplace": "GUMROAD", "outcome": "OK"})]
