"""Qualifier (S2-03): a bus consumer. On each signal.captured it scores the lead and publishes lead.scored (and
lead.qualified when the lead clears both bars). It runs inside the worker's per-consumer transaction, so the
receipt, the lead update and the new events commit together or not at all. Scoring an already-scored lead is a
no-op, which makes redelivery harmless. QUALIFIED never means "may be contacted": that needs a ContactPermission,
checked by the Compliance Guard and the database send gate (ADR-11)."""
from __future__ import annotations

from typing import Callable, Optional, Protocol

from orchestrator.scoring import Scorer, Scores, SignalFeatures, composite, decide
from shared.bus.events import Event, EventType, event_id_for

CONSUMER = "qualifier"


class LeadRepo(Protocol):
    def apply_scores(self, conn, lead_id, scores: Scores, composite_score: int, status: str) -> bool: ...


class Qualifier:
    def __init__(self, scorer: Scorer, repo: LeadRepo, publish: Optional[Callable] = None):
        self.scorer, self.repo, self._publish = scorer, repo, publish

    def register(self, worker) -> None:
        worker.subscribe(EventType.SIGNAL_CAPTURED, CONSUMER, self.handle)

    def handle(self, conn, event: Event) -> None:
        if event.lead_id is None:
            raise ValueError("signal.captured without a lead_id")          # malformed: retried, then dead-lettered
        scores = self.scorer.score(SignalFeatures.from_payload(event.payload))
        comp = composite(scores)
        status = decide(comp, scores.confidence)
        if not self.repo.apply_scores(conn, event.lead_id, scores, comp, status):
            return                                                          # already scored
        publish = self._publish or _default_publish()
        common = dict(marketplace=event.marketplace, lead_id=event.lead_id, trace_id=event.trace_id or event.lead_id)
        publish(conn, EventType.LEAD_SCORED, event_id=event_id_for(EventType.LEAD_SCORED, event.lead_id),
                payload={"composite": comp, "status": status}, **common)
        if status == "QUALIFIED":
            publish(conn, EventType.LEAD_QUALIFIED, event_id=event_id_for(EventType.LEAD_QUALIFIED, event.lead_id),
                    payload={"composite": comp}, **common)


def _default_publish():
    from shared.bus.publisher import publish          # imported late: needs the database driver
    return publish
