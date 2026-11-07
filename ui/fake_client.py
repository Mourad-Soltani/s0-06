"""An in-memory stand-in for ReviewClient, for tests and for trying the dashboard without a server. It behaves like the real
API: only PASSED proposals can be approved, a decision is final, and a bad token is refused."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ui.review_client import AuthError, ReviewApiError

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
GOOD_BODY = "Subject: Your guide is ready\n\nHi,\n\nHere it is: https://shop.example.test/l/gr-1\n\nThanks!"


def make_detail(*, verdict: Optional[str] = "PASS", tier: int = 0, status: str = "PROPOSED", body: str = GOOD_BODY, source: str = "TEMPLATE",
                findings=(), age_minutes: int = 0, marketplace: str = "EMAIL", lead: Optional[dict] = ...,
                route: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
    """route (S4-03) is (marketplace, reason): the checkout marketplace the draft was routed to, recorded in its payload as the pipeline does."""
    default_lead = {"status": "QUALIFIED", "composite_score": 75, "confidence": 0.8, "icp_match_score": 90, "pain_intensity": 50,
                    "authority_signal": 50, "timeline_signal": 80, "budget_signal": 80, "risk_flags": [], "product_external_id": "gr-1",
                    "source_type": "INBOUND", "source_marketplace": "EMAIL", "created_at": T0.isoformat()}
    payload = {"context": "consented_outreach", "body": body, "source": source, "product_external_id": "gr-1", "discount_pct": None, "offer_code": None}
    if route is not None:
        payload["route_marketplace"], payload["route_reason"] = route
    return {"proposal_id": str(uuid.uuid4()), "lead_id": str(uuid.uuid4()), "role": "COPYWRITER", "marketplace": marketplace, "channel": "EMAIL",
            "action_type": "SEND_MESSAGE", "autonomy_tier": tier, "confidence": 0.6, "compliance_verdict": verdict, "status": status,
            "created_at": (T0 - timedelta(minutes=age_minutes)).isoformat(), "payload_ref": str(uuid.uuid4()),
            "payload": payload,
            "lead": default_lead if lead is ... else lead, "findings": [{"rule_code": c, "severity": s, "evaluated_at": T0.isoformat()} for c, s in findings]}


class FakeReviewClient:
    def __init__(self, *details: Dict[str, Any], token_ok: bool = True):
        self.items: Dict[str, Dict[str, Any]] = {d["proposal_id"]: d for d in details}
        self.token_ok, self.calls, self.fail_with = token_ok, [], None

    def _gate(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if not self.token_ok:
            raise AuthError()
        if self.fail_with is not None:
            raise self.fail_with

    def list_actions(self, limit: int = 50, marketplace: Optional[str] = None) -> List[Dict[str, Any]]:
        self._gate("list")
        pending = [d for d in self.items.values() if d["status"] == "PROPOSED"]
        return [{k: d[k] for k in ("proposal_id", "lead_id", "role", "marketplace", "channel", "action_type", "autonomy_tier", "confidence",
                                   "compliance_verdict", "created_at", "payload_ref")}
                for d in sorted(pending, key=lambda d: d["created_at"])]

    def get_action(self, proposal_id) -> Dict[str, Any]:
        self._gate("get", str(proposal_id))
        if str(proposal_id) not in self.items:
            raise ReviewApiError("NOT_FOUND", 404)
        return self.items[str(proposal_id)]

    def approve(self, proposal_id) -> None:
        self._gate("approve", str(proposal_id))
        d = self.items.get(str(proposal_id))
        if d is None:
            raise ReviewApiError("NOT_FOUND", 404)
        if d["status"] != "PROPOSED":
            raise ReviewApiError("NOT_PENDING", 409)
        if d["compliance_verdict"] != "PASS":
            raise ReviewApiError("NOT_APPROVABLE", 409)
        d["status"] = "APPROVED"

    def reject(self, proposal_id, reason_code: str) -> None:
        self._gate("reject", str(proposal_id), reason_code)
        d = self.items.get(str(proposal_id))
        if d is None:
            raise ReviewApiError("NOT_FOUND", 404)
        if d["status"] != "PROPOSED":
            raise ReviewApiError("NOT_PENDING", 409)
        d["status"] = "REJECTED"
