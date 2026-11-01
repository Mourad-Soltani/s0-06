"""ReviewQueue.get with the lead and findings added: shape, privacy, backward compatibility. No database. Names RD*."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from orchestrator.review_service import ReviewQueue

PID, LEAD, REF = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
T = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
MAIN = (PID, LEAD, "COPYWRITER", "EMAIL", "EMAIL", "SEND_MESSAGE", 1, Decimal("0.80"), "BLOCK", "PROPOSED", T, REF, {"body": "Subject: x\n\nhi"})
LEADROW = ("QUALIFIED", 75, Decimal("0.80"), 90, 50, 50, 80, 80, ["CONSENT_RISK"], "gr-1", "INBOUND", "EMAIL", T)


class Cur:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, *cursors):
        self.queue, self.calls = list(cursors), []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return self.queue.pop(0) if self.queue else Cur()


def test_RD1_the_detail_carries_the_lead_scores_and_the_latest_findings_as_json_ready_values():
    conn = Conn(Cur(MAIN), Cur(LEADROW), Cur(rows=[("LINK_HOST_NOT_ALLOWED", "BLOCK", T), ("URGENCY_PRESSURE", "ESCALATE", T)]))
    got = ReviewQueue(conn).get(PID)
    assert got["proposal_id"] == str(PID) and got["payload"] == {"body": "Subject: x\n\nhi"} and got["compliance_verdict"] == "BLOCK"
    assert got["lead"] == {"status": "QUALIFIED", "composite_score": 75, "confidence": 0.8, "icp_match_score": 90, "pain_intensity": 50,
                           "authority_signal": 50, "timeline_signal": 80, "budget_signal": 80, "risk_flags": ["CONSENT_RISK"],
                           "product_external_id": "gr-1", "source_type": "INBOUND", "source_marketplace": "EMAIL", "created_at": T.isoformat()}
    assert got["findings"] == [{"rule_code": "LINK_HOST_NOT_ALLOWED", "severity": "BLOCK", "evaluated_at": T.isoformat()},
                               {"rule_code": "URGENCY_PRESSURE", "severity": "ESCALATE", "evaluated_at": T.isoformat()}]


def test_RD2_the_lead_is_looked_up_by_the_proposals_lead_and_findings_come_from_the_latest_run_only():
    conn = Conn(Cur(MAIN), Cur(LEADROW), Cur(rows=[]))
    ReviewQueue(conn).get(PID)
    (_, p1), (lead_sql, p2), (find_sql, p3) = conn.calls
    assert p1 == (PID,) and p2 == (LEAD,) and p3 == (PID, PID)
    assert "FROM core.lead WHERE id = %s" in lead_sql
    assert "run_id = (SELECT run_id FROM core.compliance_finding WHERE proposal_id = %s ORDER BY evaluated_at DESC, id DESC LIMIT 1)" in find_sql


def test_RD3_no_contact_or_address_column_is_ever_selected_for_the_lead_or_findings():
    conn = Conn(Cur(MAIN), Cur(LEADROW), Cur(rows=[]))
    ReviewQueue(conn).get(PID)
    for sql, _ in conn.calls[1:]:
        assert "contact" not in sql.lower() and "email" not in sql.lower() and "name" not in sql.lower().replace("rule_code", "")


def test_RD4_a_proposal_with_no_lead_row_or_no_findings_still_returns_cleanly():
    got = ReviewQueue(Conn(Cur(MAIN), Cur(None), Cur(rows=[]))).get(PID)
    assert got["lead"] is None and got["findings"] == [] and got["status"] == "PROPOSED"
    assert ReviewQueue(Conn(Cur(None))).get(PID) is None


def test_RD5_the_original_keys_are_unchanged_so_existing_callers_keep_working():
    got = ReviewQueue(Conn(Cur(MAIN))).get(PID)                                       # the old fake answers only the first query
    for key in ("proposal_id", "lead_id", "role", "marketplace", "channel", "action_type", "autonomy_tier", "confidence",
                "compliance_verdict", "status", "created_at", "payload_ref", "payload"):
        assert key in got
    assert got["lead"] is None and got["findings"] == []
