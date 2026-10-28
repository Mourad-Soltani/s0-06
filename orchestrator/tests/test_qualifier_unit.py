"""Qualifier handler with a fake repo and a recording publisher: no database. Names QU*."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.qualifier import CONSUMER, Qualifier
from orchestrator.scoring import IcpDefinition, RuleScorer, Scores, SignalFeatures
from shared.bus.events import Event, EventType, event_id_for

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
LEAD, TRACE = uuid.uuid4(), uuid.uuid4()
SCORER = RuleScorer(IcpDefinition(frozenset({"gr-1"})), now=lambda: NOW)


class Repo:
    def __init__(self, fresh=True):
        self.fresh, self.calls = fresh, []

    def apply_scores(self, conn, lead_id, scores, comp, status):
        self.calls.append((lead_id, comp, status, scores.risk_flags))
        return self.fresh


def event(product="gr-1", trace=TRACE, lead=LEAD, payload=None):
    p = payload if payload is not None else SignalFeatures("SALE", NOW - timedelta(hours=1), product, False).to_payload()
    return Event(id=1, event_id=uuid.uuid4(), event_type=EventType.SIGNAL_CAPTURED, marketplace="GUMROAD",
                 lead_id=lead, payload_ref=None, payload=p, trace_id=trace, attempts=1)


def qualifier(repo=None, scorer=SCORER):
    published = []
    q = Qualifier(scorer, repo or Repo(), publish=lambda conn, etype, **kw: published.append((etype, kw)))
    return q, published


def test_QU1_a_qualified_lead_publishes_scored_then_qualified_with_the_leads_trace():
    q, pub = qualifier()
    q.handle(None, event())
    assert [e for e, _ in pub] == [EventType.LEAD_SCORED, EventType.LEAD_QUALIFIED]
    scored, qualified = pub[0][1], pub[1][1]
    assert scored["payload"] == {"composite": 75, "status": "QUALIFIED"} and qualified["payload"] == {"composite": 75}
    assert scored["event_id"] == event_id_for(EventType.LEAD_SCORED, LEAD)
    assert qualified["event_id"] == event_id_for(EventType.LEAD_QUALIFIED, LEAD)
    assert all(kw["lead_id"] == LEAD and kw["trace_id"] == TRACE and kw["marketplace"] == "GUMROAD" for _, kw in pub)


def test_QU2_a_lead_that_needs_review_publishes_only_lead_scored():
    q, pub = qualifier()
    q.handle(None, event(product="other"))
    assert [e for e, _ in pub] == [EventType.LEAD_SCORED] and pub[0][1]["payload"]["status"] == "NEEDS_REVIEW"


def test_QU3_a_disqualified_lead_is_recorded_and_announced_once():
    class Cold:
        def score(self, features):
            return Scores(10, 10, 10, 10, 10, 0.9)

    repo = Repo()
    q, pub = qualifier(repo, Cold())
    q.handle(None, event())
    assert repo.calls == [(LEAD, 10, "DISQUALIFIED", ())] and [e for e, _ in pub] == [EventType.LEAD_SCORED]


def test_QU4_an_already_scored_lead_is_a_no_op():
    q, pub = qualifier(Repo(fresh=False))
    q.handle(None, event())
    assert pub == []


@pytest.mark.parametrize("bad", [dict(lead=None), dict(payload={}), dict(payload={"kind": "WAT", "occurred_at": "x"})])
def test_QU5_malformed_events_raise_and_publish_nothing(bad):
    repo = Repo()
    q, pub = qualifier(repo)
    with pytest.raises(ValueError):
        q.handle(None, event(**bad))
    assert pub == [] and repo.calls == []


def test_QU6_the_trace_falls_back_to_the_lead_id_and_the_consumer_registers_itself():
    q, pub = qualifier()
    q.handle(None, event(trace=None))
    assert pub[0][1]["trace_id"] == LEAD

    class W:
        subs = []

        def subscribe(self, etype, consumer, handler):
            self.subs.append((etype, consumer, handler))

    w = W()
    q.register(w)
    assert w.subs == [(EventType.SIGNAL_CAPTURED, CONSUMER, q.handle)] and CONSUMER == "qualifier"
