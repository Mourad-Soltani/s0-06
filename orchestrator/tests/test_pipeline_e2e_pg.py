"""The whole product on Postgres: import consented contacts, score, draft, check, review, approve, send. Every consumer on one bus.
Needs TEST_ADMIN_DSN. Names E2E*."""
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adapters.email_channel.adapter import EmailChannelAdapter
from adapters.email_channel.fake_transport import FakeEmailTransport
from adapters.email_channel.message import EmailConfig
from adapters.gumroad.adapter import GumroadAdapter
from adapters.gumroad.fake_transport import FakeGumroadTransport
from orchestrator.closer import Closer
from orchestrator.copywriter import Copywriter
from orchestrator.drafter import DraftingConsumer, default_writer_factory
from orchestrator.guard import ComplianceGuard
from orchestrator.guard_consumer import GuardConsumer
from orchestrator.guard_rules import GuardConfig
from orchestrator.lead_intake import ConsentedLeadIntake, intake_audit_hook
from orchestrator.lead_store import PgLeadRepo
from orchestrator.llm import FakeLlmClient, LlmResponse, NotConfiguredLlmClient
from orchestrator.proposals import ProposalWriter, copy_audit_hook
from orchestrator.qualifier import Qualifier
from orchestrator.review_service import NOT_APPROVABLE, ReviewError, ReviewQueue, ShadowStats, review_audit_hook
from orchestrator.scoring import IcpDefinition, RuleScorer
from orchestrator.wiring import register_pipeline
from shared.audit import AuditWriter
from shared.bus.publisher import publish
from shared.bus.store import PgOutboxStore
from shared.bus.worker import Worker
from shared.consent import ConsentService
from shared.cost import CostTracker, ModelPrice, PriceBook, RecordingNotifier, RoutingPolicy

URL = "https://shop.example.test/l/gr-1"
HEAD = "email,name,consented_at,source_url,product_id,kind"
PASSING = ShadowStats(85, 15, 10, 100, 2, datetime.now(timezone.utc) - timedelta(days=15))
PRICES = PriceBook({"small-model": ModelPrice(1000, 2000), "strong-model": ModelPrice(3000, 15000)})
ROUTES = {"SCOUT": None, "QUALIFIER": None, "COPYWRITER": "strong-model", "CLOSER": None, "COMPLIANCE_GUARD": None, "ANALYST": None}
NO_MODEL = {**ROUTES, "COPYWRITER": None}


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def csv_row(email="jane.doe@example.test", kind="SALE", product="gr-1"):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    return f"{HEAD}\n{email},Jane Doe,{yesterday},https://example.test/optin,{product},{kind}\n"


class Pipeline:
    def __init__(self, db, *, earned_tier_1=False, llm=None, routes=NO_MODEL):
        self.db = db
        gumroad = FakeGumroadTransport()
        gumroad.add_product("gr-1", name="Budget Planner", price=1900, short_url=URL)
        self.products = GumroadAdapter(gumroad)
        self.email = EmailChannelAdapter(FakeEmailTransport(), EmailConfig("1 Example Street, Paris 75001, France", "https://example.test/unsubscribe"))
        self.guard = ComplianceGuard({"EMAIL": self.email}, GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20")))
        copywriter = Copywriter(RoutingPolicy(routes, PRICES), CostTracker(db.conn, PRICES, RecordingNotifier()), llm or NotConfiguredLlmClient())
        if earned_tier_1:
            def factory(conn):
                w = AuditWriter(conn)
                return ProposalWriter(conn, copywriter, hasher=w.hash_payload, stats_provider=lambda mp: PASSING, audit=copy_audit_hook(w))
        else:
            factory = default_writer_factory(copywriter)

        @contextmanager
        def connect():
            yield db.conn

        self.closer = Closer(connect, self.products, {"EMAIL": self.email}, self.guard)
        self.worker = Worker(PgOutboxStore(db.conn), "w1")
        register_pipeline(self.worker, qualifier=Qualifier(RuleScorer(IcpDefinition(frozenset({"gr-1"}))), PgLeadRepo()),
                          drafter=DraftingConsumer(factory, self.products), guard_consumer=GuardConsumer(self.guard), closer=self.closer)
        w = AuditWriter(db.conn)
        self.queue = ReviewQueue(db.conn, audit=review_audit_hook(w), hasher=w.hash_payload, publish=publish)
        self.writer = w

    def import_csv(self, text):
        w = self.writer
        intake = ConsentedLeadIntake(self.db.conn, ConsentService(self.db.conn, lambda a, d: w.record(d["actor"], a, marketplace=d["marketplace"])), audit=intake_audit_hook(w))
        return [r.outcome for r in intake.import_csv(text, actor="ops-1").results]

    def pending(self):
        return self.queue.list_pending()


def proposals(db):
    return db.conn.execute("SELECT id, status::text, autonomy_tier, compliance_verdict::text FROM core.action_proposal").fetchall()


def events(db, etype):
    return db.conn.execute("SELECT payload, lead_id, trace_id FROM core.outbox_event WHERE event_type=%s", (etype,)).fetchall()


def audit_actions(db):
    return [a for (a,) in db.conn.execute("SELECT action_type FROM audit.event ORDER BY seq").fetchall()]


def test_E2E1_an_imported_contact_becomes_a_reviewed_draft_and_an_approved_one_is_sent(db):
    p = Pipeline(db, earned_tier_1=True)
    assert p.import_csv(csv_row()) == ["IMPORTED"]
    assert p.worker.drain().retried == 0
    lead = one(db, "SELECT id FROM core.lead")
    assert db.conn.execute("SELECT status::text, composite_score, risk_flags FROM core.lead").fetchone() == ("QUALIFIED", 75, [])
    (pid, status, tier, verdict), = proposals(db)
    assert (status, tier, verdict) == ("PROPOSED", 1, "PASS") and [a.proposal_id for a in p.pending()] == [pid]       # waiting for a person
    assert p.email.transport.sent == []                                                                              # nothing leaves unreviewed

    p.queue.approve(pid, "alice")
    assert p.worker.drain().retried == 0
    assert proposals(db)[0][1] == "SENT"
    (sent,) = p.email.transport.sent
    assert f"{URL}?ref={lead}" in sent.text and "To stop receiving these emails" in sent.text and "Budget Planner" in sent.text
    chain = ["signal.captured", "lead.scored", "lead.qualified", "action.proposed", "compliance.passed", "action.approved", "action.sent"]
    for etype in chain:
        ((payload, lead_id, trace),) = events(db, etype)[:1]
        assert lead_id == trace == lead, etype                                                                      # one trace for the whole lead
    assert one(db, "SELECT count(*) FROM core.outbox_event WHERE status <> 'DONE'") == 0
    actions = audit_actions(db)
    for expected in ("CONSENT_CAPTURED", "CONSENT_IMPORT_COMPLETED", "COPY_DRAFTED", "COMPLIANCE_EVALUATED", "REVIEW_APPROVED", "CLOSER_SENT"):
        assert expected in actions
    assert actions.index("COPY_DRAFTED") < actions.index("COMPLIANCE_EVALUATED") < actions.index("REVIEW_APPROVED") < actions.index("CLOSER_SENT")


def test_E2E2_in_shadow_mode_the_whole_chain_runs_but_nothing_is_ever_sent(db):
    p = Pipeline(db)                                                    # no tier earned yet: tier 0
    p.import_csv(csv_row())
    p.worker.drain()
    (pid, status, tier, verdict), = proposals(db)
    assert (status, tier, verdict) == ("PROPOSED", 0, "PASS")
    p.queue.approve(pid, "alice")
    p.worker.drain()
    assert proposals(db)[0][1] == "APPROVED" and p.email.transport.sent == [] and "CLOSER_SHADOW" in audit_actions(db)


def test_E2E3_a_model_that_ignores_its_instructions_is_stopped_by_the_guard_before_a_person_could_approve(db):
    bad = json.dumps({"variants": [{"subject": "Your guide", "body": "Hi,\n\nBuy at https://evil.test/x"}], "confidence": 0.9})
    p = Pipeline(db, earned_tier_1=True, llm=FakeLlmClient(LlmResponse(bad, 300, 100)), routes=ROUTES)
    p.import_csv(csv_row())
    p.worker.drain()
    (pid, status, tier, verdict), = proposals(db)
    assert (status, verdict) == ("PROPOSED", "BLOCK") and events(db, "compliance.passed") == []
    with pytest.raises(ReviewError) as e:
        p.queue.approve(pid, "alice")
    assert e.value.code == NOT_APPROVABLE and events(db, "action.approved") == [] and p.email.transport.sent == []


def test_E2E4_importing_the_same_file_again_creates_no_second_draft_and_no_second_email(db):
    p = Pipeline(db, earned_tier_1=True)
    p.import_csv(csv_row())
    p.worker.drain()
    (pid, *_), = proposals(db)
    p.queue.approve(pid, "alice")
    p.worker.drain()
    assert p.import_csv(csv_row()) == ["ALREADY_IMPORTED"]
    p.worker.drain()
    assert len(proposals(db)) == 1 and len(p.email.transport.sent) == 1


def test_E2E5_a_qualified_lead_that_cannot_be_drafted_is_skipped_with_a_visible_reason(db):
    p = Pipeline(db, earned_tier_1=True)
    perm = ConsentService(db.conn).capture("kim.roe@example.test", "EMAIL", "https://example.test/optin")
    contact = one(db, "SELECT contact_id FROM pii.contact_permission WHERE id=%s", perm)
    lead = one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id) VALUES ('INBOUND','x',repeat('a',64),%s) RETURNING id", contact)   # no product
    publish(db.conn, "lead.qualified", marketplace="EMAIL", lead_id=lead, payload={"composite": 80}, trace_id=lead)
    assert p.worker.drain().retried == 0
    assert proposals(db) == [] and db.conn.execute("SELECT outcome FROM audit.event WHERE action_type='DRAFT_SKIPPED'").fetchall() == [("NO_PRODUCT",)]


def test_E2E6_a_kill_switch_during_the_approval_holds_the_send_until_it_is_released(db):
    p = Pipeline(db, earned_tier_1=True)
    p.import_csv(csv_row())
    p.worker.drain()
    (pid, *_), = proposals(db)
    db.conn.execute("SELECT core.set_kill_switch('GLOBAL', true, 'ops-1', 'TEST_REASON')")
    p.queue.approve(pid, "alice")
    p.worker.drain()
    assert proposals(db)[0][1] == "APPROVED" and p.email.transport.sent == []
    db.conn.execute("SELECT core.set_kill_switch('GLOBAL', false, 'ops-1')")
    assert [r.outcome for r in p.closer.sweep()] == ["SENT"] and len(p.email.transport.sent) == 1


def test_E2E7_nothing_personal_reaches_an_event_the_audit_trail_or_a_lead_row(db):
    p = Pipeline(db, earned_tier_1=True)
    p.import_csv(csv_row())
    p.worker.drain()
    (pid, *_), = proposals(db)
    p.queue.approve(pid, "alice")
    p.worker.drain()
    blob = "".join(json.dumps(db.conn.execute(q).fetchall(), default=str) for q in (
        "SELECT row_to_json(l) FROM core.lead l", "SELECT row_to_json(e) FROM core.outbox_event e", "SELECT row_to_json(a) FROM audit.event a"))
    assert "example.test" not in blob and "Jane" not in blob
