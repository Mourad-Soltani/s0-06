"""Lead scoring (spec 3.2, 3.3; ADR-06, ADR-17). Pure Python. The weights and the rule scorer are PLACEHOLDERS to be
recalibrated after 100 labelled leads (ADR-06). The thresholds are ADR-06's."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, FrozenSet, Mapping, Optional, Protocol, Tuple

# ADR-17 placeholder weights, in percent, summing to 100. Authority and timeline weigh little: the products are
# low-ticket digital goods, where a buyer is their own decision maker.
WEIGHTS = {"icp_match": 35, "pain_intensity": 20, "authority_signal": 10, "timeline_signal": 10, "budget_signal": 25}
QUALIFY_AT, DISQUALIFY_BELOW, MIN_CONFIDENCE = 60, 40, 0.75      # ADR-06
KINDS = ("SALE", "REVIEW", "QA", "CART", "SUBSCRIBER", "OFFER_CODE_USE")
_FLAG = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class Scores:
    icp_match: int
    pain_intensity: int
    authority_signal: int
    timeline_signal: int
    budget_signal: int
    confidence: float
    risk_flags: Tuple[str, ...] = ()

    def __post_init__(self):
        for name in WEIGHTS:
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
                raise ValueError(f"{name} must be an integer 0 to 100")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be 0 to 1")
        if not all(_FLAG.match(f) for f in self.risk_flags):
            raise ValueError("risk flags must be UPPER_SNAKE codes")


def composite(s: Scores) -> int:
    """Weighted mean, rounded half up, in exact integer arithmetic."""
    return (sum(w * getattr(s, k) for k, w in WEIGHTS.items()) + 50) // 100


def decide(composite_score: int, confidence: float) -> str:
    """Disqualify first (a low score is final whatever the confidence), then qualify only when both the score and
    the confidence clear their bars; everything else goes to a human."""
    if composite_score < DISQUALIFY_BELOW:
        return "DISQUALIFIED"
    if composite_score >= QUALIFY_AT and confidence >= MIN_CONFIDENCE:
        return "QUALIFIED"
    return "NEEDS_REVIEW"


@dataclass(frozen=True)
class SignalFeatures:
    """What the Qualifier is allowed to know about a signal. Ids and codes only; travels in the event payload."""
    kind: str
    occurred_at: datetime
    product_external_id: Optional[str] = None
    has_contact: bool = False

    def to_payload(self) -> dict:
        p = {"kind": self.kind, "occurred_at": self.occurred_at.isoformat(), "has_contact": self.has_contact}
        if self.product_external_id:
            p["product_external_id"] = self.product_external_id
        return p

    @staticmethod
    def from_payload(p: Mapping[str, Any]) -> "SignalFeatures":
        kind, when = p.get("kind"), p.get("occurred_at")
        if kind not in KINDS or not isinstance(when, str):
            raise ValueError("invalid signal payload")
        t = datetime.fromisoformat(when.replace("Z", "+00:00"))
        pid = p.get("product_external_id")
        return SignalFeatures(kind, t if t.tzinfo else t.replace(tzinfo=timezone.utc),
                              pid if isinstance(pid, str) else None, p.get("has_contact") is True)


@dataclass(frozen=True)
class IcpDefinition:
    product_ids: FrozenSet[str]          # products whose buyers are the ideal customer profile


class Scorer(Protocol):
    def score(self, features: SignalFeatures) -> Scores: ...


_BUDGET = {"SALE": 80, "OFFER_CODE_USE": 70, "CART": 60, "REVIEW": 50, "SUBSCRIBER": 40, "QA": 40}
_CONFIDENT = ("SALE", "OFFER_CODE_USE")        # structured facts, not inferences


class RuleScorer:
    """Deterministic baseline. No LLM, so no cost. An LLM scorer (routed per ADR-04) would implement the same
    protocol; none exists yet because no provider is configured (cutover item C2)."""

    def __init__(self, icp: IcpDefinition, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.icp, self._now = icp, now

    def score(self, f: SignalFeatures) -> Scores:
        recent = self._now() - f.occurred_at <= timedelta(days=7)
        return Scores(
            icp_match=90 if f.product_external_id in self.icp.product_ids else 30,
            pain_intensity=50, authority_signal=50, timeline_signal=80 if recent else 50,
            budget_signal=_BUDGET[f.kind], confidence=0.8 if f.kind in _CONFIDENT else 0.6,
            risk_flags=() if f.has_contact else ("CONSENT_RISK",))   # no contact or consent on record: never Tier 2
