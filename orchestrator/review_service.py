"""The review queue's logic (S2-05, spec 5.3): list pending proposals, approve, reject, edit, and read the shadow-mode
statistics. Every decision is recorded in core.review_decision / core.review_edit AND audited, inside the SAME
transaction, so no decision exists without its audit event and none is audited that did not happen.

Rules (ADR-18):
  * approve only a proposal that is PROPOSED and whose compliance verdict is PASS. BLOCK, ESCALATE and "no verdict"
    can be rejected or edited, never approved. Approving a Tier 0 draft counts toward the shadow-mode gate but can
    never be sent (the table CHECK forbids Tier 0 reaching SENDING).
  * a decision is final: one per proposal, no update, no delete.
  * an edit replaces the message body and its salted hash and CLEARS the compliance verdict, because the Guard
    judged the old text. The proposal cannot be approved until it is re-evaluated.
Reviewer ids and reason codes are opaque ids and UPPER_SNAKE codes: no free text, so no PII in the decision tables
or the audit trail. conn needs the review_svc role (core tables, audit functions, and pii for bodies)."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

_REVIEWER = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MARKETPLACE = re.compile(r"^[A-Z][A-Z_]{0,31}$")
MAX_PAYLOAD_BYTES = 65_536

NOT_FOUND, NOT_PENDING, NOT_APPROVABLE, EDIT_UNAVAILABLE = "NOT_FOUND", "NOT_PENDING", "NOT_APPROVABLE", "EDIT_UNAVAILABLE"


class ReviewError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PendingAction:
    proposal_id: uuid.UUID
    lead_id: uuid.UUID
    role: str
    marketplace: str
    channel: str
    action_type: str
    autonomy_tier: int
    confidence: Optional[float]
    compliance_verdict: Optional[str]
    created_at: datetime
    payload_ref: Optional[uuid.UUID]

    def as_dict(self) -> Dict[str, Any]:
        return {k: _jsonable(v) for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class ShadowStats:
    """Spec 9.1: counts for one marketplace and role at one autonomy tier (tier 0 by default)."""
    approved: int
    rejected: int
    edited: int
    proposals: int
    blocked: int
    first_at: Optional[datetime]

    @property
    def decided(self) -> int:
        return self.approved + self.rejected

    @property
    def approval_rate(self) -> Optional[float]:
        return self.approved / self.decided if self.decided else None

    @property
    def edit_rate(self) -> Optional[float]:
        return self.edited / self.proposals if self.proposals else None

    @property
    def block_rate(self) -> Optional[float]:
        return self.blocked / self.proposals if self.proposals else None

    def days_observed(self, now: datetime) -> float:
        return (now - self.first_at).total_seconds() / 86400 if self.first_at else 0.0

    def passes_tier1_gate(self, now: datetime, *, min_actions: int = 100, min_days: int = 14,
                          min_approval_pct: int = 80, max_block_pct: int = 5) -> bool:
        """At least min_actions proposals AND min_days observed (the longer of the two), approval rate at least 80%
        of decided proposals, and a block rate strictly under 5% of all proposals. Exact integer arithmetic."""
        if self.proposals < min_actions or self.days_observed(now) < min_days or not self.decided:
            return False
        return self.approved * 100 >= min_approval_pct * self.decided and self.blocked * 100 < max_block_pct * self.proposals


def _jsonable(v: Any) -> Any:
    if isinstance(v, (uuid.UUID,)):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _check_reviewer(reviewer: str) -> None:
    if not isinstance(reviewer, str) or not _REVIEWER.match(reviewer):
        raise ValueError("reviewer must be an opaque id (letters, digits and _:.-), never an email or a name")


def review_audit_hook(writer) -> Callable[[str, dict], None]:
    """Adapter for the audit writer (shared/audit.py): ids, enums and codes only."""
    def hook(action: str, d: dict) -> None:
        writer.record(d["actor"], action, marketplace=d.get("marketplace"), lead_id=d.get("lead_id"),
                      compliance_verdict=d.get("verdict"), autonomy_tier=d.get("tier"), outcome=d.get("outcome"))
    return hook


class ReviewQueue:
    def __init__(self, conn, audit: Optional[Callable[[str, dict], None]] = None,
                 hasher: Optional[Callable[[uuid.UUID, dict], bytes]] = None):
        self.conn, self._audit, self._hasher = conn, audit, hasher

    # ------------------------------------------------------------------ reading
    def list_pending(self, limit: int = 50, marketplace: Optional[str] = None) -> List[PendingAction]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be 1 to 200")
        if marketplace is not None and not _MARKETPLACE.match(marketplace):
            raise ValueError("invalid marketplace")
        rows = self.conn.execute(
            "SELECT id, lead_id, role::text, marketplace::text, channel::text, action_type::text, autonomy_tier, confidence, "
            "compliance_verdict::text, created_at, payload_ref FROM core.action_proposal WHERE status = 'PROPOSED' "
            "AND (%s::core.marketplace IS NULL OR marketplace = %s::core.marketplace) ORDER BY created_at, id LIMIT %s",
            (marketplace, marketplace, limit)).fetchall()
        return [PendingAction(r[0], r[1], r[2], r[3], r[4], r[5], r[6], float(r[7]) if r[7] is not None else None,
                              r[8], r[9], r[10]) for r in rows]

    def get(self, proposal_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """One proposal with its message body (personal data: needs the review_svc role)."""
        r = self.conn.execute(
            "SELECT p.id, p.lead_id, p.role::text, p.marketplace::text, p.channel::text, p.action_type::text, "
            "p.autonomy_tier, p.confidence, p.compliance_verdict::text, p.status::text, p.created_at, p.payload_ref, s.body "
            "FROM core.action_proposal p LEFT JOIN pii.payload_store s ON s.payload_id = p.payload_ref WHERE p.id = %s",
            (proposal_id,)).fetchone()
        if r is None:
            return None
        keys = ("proposal_id", "lead_id", "role", "marketplace", "channel", "action_type", "autonomy_tier", "confidence",
                "compliance_verdict", "status", "created_at", "payload_ref", "payload")
        return {k: _jsonable(v) for k, v in zip(keys, r)}

    def shadow_stats(self, marketplace: str, role: str, tier: int = 0) -> ShadowStats:
        d = self.conn.execute(
            "SELECT count(*) FILTER (WHERE d.decision = 'APPROVE'), count(*) FILTER (WHERE d.decision = 'REJECT') "
            "FROM core.review_decision d JOIN core.action_proposal p ON p.id = d.proposal_id "
            "WHERE p.marketplace = %s::core.marketplace AND p.role = %s::core.agent_role AND p.autonomy_tier = %s::smallint",
            (marketplace, role, tier)).fetchone()
        e = self.conn.execute(
            "SELECT count(DISTINCT e.proposal_id) FROM core.review_edit e JOIN core.action_proposal p ON p.id = e.proposal_id "
            "WHERE p.marketplace = %s::core.marketplace AND p.role = %s::core.agent_role AND p.autonomy_tier = %s::smallint",
            (marketplace, role, tier)).fetchone()
        a = self.conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE compliance_verdict = 'BLOCK'), min(created_at) FROM core.action_proposal "
            "WHERE marketplace = %s::core.marketplace AND role = %s::core.agent_role AND autonomy_tier = %s::smallint",
            (marketplace, role, tier)).fetchone()
        return ShadowStats(approved=d[0], rejected=d[1], edited=e[0], proposals=a[0], blocked=a[1], first_at=a[2])

    # ------------------------------------------------------------------ deciding
    def approve(self, proposal_id: uuid.UUID, reviewer: str) -> None:
        _check_reviewer(reviewer)
        with self.conn.transaction():
            row = self.conn.execute(
                "UPDATE core.action_proposal SET status = 'APPROVED', updated_at = now() WHERE id = %s "
                "AND status = 'PROPOSED' AND compliance_verdict = 'PASS' "
                "RETURNING lead_id, marketplace::text, autonomy_tier, compliance_verdict::text", (proposal_id,)).fetchone()
            if row is None:
                raise self._why_not(proposal_id, need_pass=True)
            self.conn.execute("INSERT INTO core.review_decision (proposal_id, decision, reviewer) VALUES (%s, 'APPROVE', %s)",
                              (proposal_id, reviewer))
            self._record("REVIEW_APPROVED", reviewer, row, "OK")

    def reject(self, proposal_id: uuid.UUID, reviewer: str, reason_code: str) -> None:
        _check_reviewer(reviewer)
        if not isinstance(reason_code, str) or not _REASON.match(reason_code):
            raise ValueError("reason_code must be an UPPER_SNAKE code")
        with self.conn.transaction():
            row = self.conn.execute(
                "UPDATE core.action_proposal SET status = 'REJECTED', updated_at = now() WHERE id = %s AND status = 'PROPOSED' "
                "RETURNING lead_id, marketplace::text, autonomy_tier, compliance_verdict::text", (proposal_id,)).fetchone()
            if row is None:
                raise self._why_not(proposal_id, need_pass=False)
            self.conn.execute("INSERT INTO core.review_decision (proposal_id, decision, reason_code, reviewer) "
                              "VALUES (%s, 'REJECT', %s, %s)", (proposal_id, reason_code, reviewer))
            self._record("REVIEW_REJECTED", reviewer, row, reason_code)

    def edit(self, proposal_id: uuid.UUID, reviewer: str, patched_payload: Dict[str, Any]) -> None:
        _check_reviewer(reviewer)
        if not isinstance(patched_payload, dict) or not patched_payload:
            raise ValueError("patched_payload must be a non-empty object")
        if len(json.dumps(patched_payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("patched_payload is too large")
        if self._hasher is None:
            raise ReviewError(EDIT_UNAVAILABLE)
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT lead_id, payload_ref, status::text, marketplace::text, autonomy_tier FROM core.action_proposal "
                "WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()
            if row is None:
                raise ReviewError(NOT_FOUND)
            lead_id, old_ref, status, marketplace, tier = row
            if status != "PROPOSED":
                raise ReviewError(NOT_PENDING)
            new_hash = self._hasher(lead_id, patched_payload)
            from psycopg.types.json import Json          # late import: the database driver
            new_ref = self.conn.execute("INSERT INTO pii.payload_store (lead_id, body) VALUES (%s, %s) RETURNING payload_id",
                                        (lead_id, Json(patched_payload))).fetchone()[0]
            self.conn.execute("UPDATE core.action_proposal SET payload_ref = %s, payload_hash = %s::bytea, "
                              "compliance_verdict = NULL, updated_at = now() WHERE id = %s", (new_ref, new_hash, proposal_id))
            if old_ref is not None:
                self.conn.execute("DELETE FROM pii.payload_store WHERE payload_id = %s", (old_ref,))
            self.conn.execute("INSERT INTO core.review_edit (proposal_id, reviewer) VALUES (%s, %s)", (proposal_id, reviewer))
            self._record("REVIEW_EDITED", reviewer, (lead_id, marketplace, tier, None), "OK")

    # ------------------------------------------------------------------ helpers
    def _why_not(self, proposal_id: uuid.UUID, need_pass: bool) -> ReviewError:
        row = self.conn.execute("SELECT status::text, compliance_verdict::text FROM core.action_proposal WHERE id = %s",
                                (proposal_id,)).fetchone()
        if row is None:
            return ReviewError(NOT_FOUND)
        if row[0] != "PROPOSED":
            return ReviewError(NOT_PENDING)
        return ReviewError(NOT_APPROVABLE)

    def _record(self, action: str, reviewer: str, row, outcome: str) -> None:
        if self._audit:                                  # inside the transaction: a failed audit undoes the decision
            lead_id, marketplace, tier, verdict = row
            self._audit(action, {"actor": reviewer, "lead_id": lead_id, "marketplace": marketplace, "tier": tier,
                                 "verdict": verdict, "outcome": outcome})
