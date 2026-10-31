"""The Compliance Guard (S3-02): the rules plus the marketplace adapter's own preflight (the adapter is the policy
enforcement point, ADR-02), and the service that records a verdict on a proposal. An adapter that is missing for the
proposal's marketplace is a BLOCK: no adapter means nobody has vouched for the rules. The verdict, its findings, the
audit event and (on PASS) the compliance.passed event commit in ONE transaction, so there is no verdict without its
audit trail. Re-evaluating a proposal after an edit adds new findings and keeps the old ones. The tier is NOT decided
here. conn needs the review_svc role: it reads message bodies."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Optional, Tuple

from orchestrator.guard_rules import BLOCK, ESCALATE, PASS, DraftFacts, Finding, GuardConfig, merge, run_rules, verdict_of
from shared.bus.events import EventType, event_id_for
from shared.models import ActionType, Marketplace, ProposedAction, Verdict

NOT_FOUND, NOT_PENDING = "NOT_FOUND", "NOT_PENDING"


class GuardError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GuardResult:
    verdict: str
    findings: Tuple[Finding, ...]

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(f.code for f in self.findings)


class ComplianceGuard:
    def __init__(self, adapters: Mapping[str, object], config: Optional[GuardConfig] = None):
        self.adapters, self.config = dict(adapters), config or GuardConfig()

    def evaluate(self, facts: DraftFacts, action: ProposedAction) -> GuardResult:
        findings = list(run_rules(facts, self.config))
        adapter = self.adapters.get(action.marketplace.value)
        if adapter is None:
            findings.append(Finding("NO_ADAPTER", BLOCK))                    # fail closed
        else:
            pre = adapter.preflight_check(action)
            if pre.verdict is not Verdict.PASS:
                sev = BLOCK if pre.verdict is Verdict.BLOCK else ESCALATE
                findings.extend(Finding(code, sev) for code in pre.reason_codes)
        merged = merge(findings)
        return GuardResult(verdict_of(merged), merged)


def guard_audit_hook(writer) -> Callable[[str, dict], None]:
    def hook(action: str, d: dict) -> None:
        writer.record("compliance-guard", action, marketplace=d.get("marketplace"), lead_id=d.get("lead_id"),
                      compliance_verdict=d.get("verdict"), autonomy_tier=d.get("tier"), outcome=d.get("outcome"))
    return hook


def _discount(raw) -> Optional[Decimal]:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
        return Decimal(-1)                                                   # present but not a number: invalid
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return Decimal(-1)


class GuardService:
    def __init__(self, conn, guard: ComplianceGuard, audit: Optional[Callable[[str, dict], None]] = None,
                 publish: Optional[Callable] = None):
        self.conn, self.guard, self._audit, self._publish = conn, guard, audit, publish

    def evaluate(self, proposal_id: uuid.UUID) -> GuardResult:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT p.lead_id, p.marketplace::text, p.action_type::text, p.autonomy_tier, p.confidence, p.status::text, "
                "p.contact_permission_id, s.body, l.risk_flags FROM core.action_proposal p "
                "JOIN core.lead l ON l.id = p.lead_id LEFT JOIN pii.payload_store s ON s.payload_id = p.payload_ref "
                "WHERE p.id = %s FOR UPDATE OF p", (proposal_id,)).fetchone()
            if row is None:
                raise GuardError(NOT_FOUND)
            lead_id, marketplace, action_type, tier, confidence, status, permission, payload, flags = row
            if status != "PROPOSED":
                raise GuardError(NOT_PENDING)
            payload = payload if isinstance(payload, dict) else {}
            live = False
            if permission is not None:
                live = self.conn.execute(
                    "SELECT 1 FROM pii.contact_permission cp JOIN pii.contact c ON c.contact_id = cp.contact_id "
                    "WHERE cp.id = %s AND cp.withdrawn_at IS NULL AND cp.expires_at > now() AND NOT c.quarantined",
                    (permission,)).fetchone() is not None
            facts = DraftFacts(context=str(payload.get("context", "")), body=payload.get("body"),
                               has_current_permission=live, discount_pct=_discount(payload.get("discount_pct")),
                               confidence=float(confidence) if confidence is not None else None,
                               risk_flags=tuple(flags or ()))
            action = ProposedAction(action_type=ActionType(action_type), marketplace=Marketplace(marketplace), payload=payload,
                                    execution_plan=tuple(payload.get("execution_plan", ())))
            result = self.guard.evaluate(facts, action)
            run_id = uuid.uuid4()
            self.conn.execute("UPDATE core.action_proposal SET compliance_verdict = %s::core.verdict, updated_at = now() "
                              "WHERE id = %s", (result.verdict, proposal_id))
            for f in result.findings:
                self.conn.execute("INSERT INTO core.compliance_finding (proposal_id, run_id, rule_code, severity) "
                                  "VALUES (%s, %s, %s, %s)", (proposal_id, run_id, f.code, f.severity))
            if self._audit:
                self._audit("COMPLIANCE_EVALUATED", {"marketplace": marketplace, "lead_id": lead_id, "verdict": result.verdict,
                                                     "tier": tier, "outcome": result.codes[0] if result.codes else "OK"})
            if result.verdict == PASS:
                publish = self._publish or _default_publish()
                publish(self.conn, EventType.COMPLIANCE_PASSED, event_id=event_id_for(EventType.COMPLIANCE_PASSED, run_id),
                        marketplace=marketplace, lead_id=lead_id, payload={"proposal_id": str(proposal_id), "tier": tier},
                        trace_id=lead_id)
            return result


def _default_publish():
    from shared.bus.publisher import publish          # imported late: needs the database driver
    return publish
