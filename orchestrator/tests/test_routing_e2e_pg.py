"""Routing on the whole pipeline, on Postgres: one product on two marketplaces, an imported contact, a routed draft, a reviewer who can see
where it points, an approval, and the right marketplace's link in the email that leaves. Needs TEST_ADMIN_DSN. Names RE2E*."""
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

from adapters.email_channel.adapter import EmailChannelAdapter
from adapters.email_channel.fake_transport import FakeEmailTransport
from adapters.email_channel.message import EmailConfig
from adapters.gumroad.adapter import GumroadAdapter
from adapters.gumroad.fake_transport import FakeGumroadTransport
from adapters.lemon_squeezy.adapter import LemonSqueezyAdapter
from adapters.lemon_squeezy.fake_transport import FakeLemonSqueezyTransport
from orchestrator.closer import Closer
from orchestrator.copywriter import Copywriter
from orchestrator.drafter import DraftingConsumer
from orchestrator.guard import ComplianceGuard
from orchestrator.guard_consumer import GuardConsumer
from orchestrator.guard_rules import GuardConfig
from orchestrator.lead_intake import ConsentedLeadIntake, intake_audit_hook
from orchestrator.lead_store import PgLeadRepo
from orchestrator.llm import NotConfiguredLlmClient
from orchestrator.proposals import ProposalWriter, copy_audit_hook
from orchestrator.qualifier import Qualifier
from orchestrator.review_service import ReviewQueue, review_audit_hook
from orchestrator.router import Router
from orchestrator.scoring import IcpDefinition, RuleScorer
from orchestrator.tests.test_pipeline_e2e_pg import NO_MODEL, PASSING, PRICES, audit_actions, csv_row, one, proposals
from orchestrator.tests.test_router_pg import seed_fee, seed_listing, seed_product
from orchestrator.wiring import register_pipeline
from shared.audit import AuditWriter
from shared.bus.events import Event, EventType
from shared.bus.publisher import publish
from shared.bus.store import PgOutboxStore
from shared.bus.worker import Worker
from shared.consent import ConsentService
from shared.cost import CostTracker, RecordingNotifier, RoutingPolicy
from ui.view_model import detail_view

GR_URL, LS_URL = "https://shop.example.test/l/gr-1", "https://buy.example.test/ls-1"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class Shop:
    """Gumroad and Lemon Squeezy sell the same product; Lemon Squeezy leaves the seller more (5% + 50 against 10% + 50)."""

    def __init__(self, db):
        self.db = db
        gum, ls = FakeGumroadTransport(), FakeLemonSqueezyTransport()
        gum.add_product("gr-1", name="Budget Planner", price=1900, short_url=GR_URL)
        ls.add_product("ls-1", name="Budget Planner", price=1900, buy_now_url=LS_URL)
        self.gumroad, self.lemon = GumroadAdapter(gum), LemonSqueezyAdapter(ls, None, clock=lambda: NOW)
        self.email = EmailChannelAdapter(FakeEmailTransport(), EmailConfig("1 Example Street, Paris 75001, France", "https://example.test/unsubscribe"))
        adapters = {"GUMROAD": self.gumroad, "LEMON_SQUEEZY": self.lemon}
        guard = ComplianceGuard({"EMAIL": self.email}, GuardConfig(allowed_link_hosts=frozenset({"shop.example.test", "buy.example.test"}),
                                                                   max_discount_pct=Decimal("20")))
        copywriter = Copywriter(RoutingPolicy(NO_MODEL, PRICES), CostTracker(db.conn, PRICES, RecordingNotifier()), NotConfiguredLlmClient())

        def factory(conn):
            w = AuditWriter(conn)
            return ProposalWriter(conn, copywriter, hasher=w.hash_payload, stats_provider=lambda mp: PASSING, audit=copy_audit_hook(w))

        @contextmanager
        def connect():
            yield db.conn

        self.drafter = DraftingConsumer(factory, self.gumroad, router=Router(), product_adapters=adapters)
        self.worker = Worker(PgOutboxStore(db.conn), "w1")
        register_pipeline(self.worker, qualifier=Qualifier(RuleScorer(IcpDefinition(frozenset({"gr-1", "gr-old"}))), PgLeadRepo()), drafter=self.drafter,
                          guard_consumer=GuardConsumer(guard), closer=Closer(connect, self.gumroad, {"EMAIL": self.email}, guard, product_adapters=adapters))
        w = AuditWriter(db.conn)
        self.queue = ReviewQueue(db.conn, audit=review_audit_hook(w), hasher=w.hash_payload, publish=publish)
        self.writer = w

    def catalog(self):
        p = seed_product(self.db)
        seed_listing(self.db, p, "GUMROAD", "gr-1")
        seed_listing(self.db, p, "GUMROAD", "gr-old", status="ARCHIVED")
        seed_listing(self.db, p, "LEMON_SQUEEZY", "ls-1")
        seed_fee(self.db, "GUMROAD", 1000, 50)
        seed_fee(self.db, "LEMON_SQUEEZY", 500, 50)

    def import_csv(self, product):
        w = self.writer
        intake = ConsentedLeadIntake(self.db.conn, ConsentService(self.db.conn, lambda a, d: w.record(d["actor"], a, marketplace=d["marketplace"])), audit=intake_audit_hook(w))
        return [r.outcome for r in intake.import_csv(csv_row(product=product), actor="ops-1").results]


def test_RE2E1_a_lead_from_an_active_listing_is_drafted_approved_and_sent_with_that_marketplaces_link(db):
    s = Shop(db)
    s.catalog()
    assert s.import_csv("gr-1") == ["IMPORTED"]
    assert s.worker.drain().retried == 0
    (pid, status, tier, verdict), = proposals(db)
    assert (status, tier, verdict) == ("PROPOSED", 1, "PASS")
    detail = s.queue.get(pid)
    assert detail["payload"]["route_marketplace"] == "GUMROAD" and detail["payload"]["route_reason"] == "LEAD_ORIGIN"
    assert detail_view(detail).route_line == "Checkout on Gumroad: this is the marketplace the lead came from"              # what the reviewer reads
    s.queue.approve(pid, "alice")
    assert s.worker.drain().retried == 0 and proposals(db)[0][1] == "SENT"
    lead = one(db, "SELECT id FROM core.lead")
    (sent,) = s.email.transport.sent
    assert f"{GR_URL}?ref={lead}" in sent.text and LS_URL not in sent.text
    assert "ROUTE_DECIDED" in audit_actions(db)


def test_RE2E2_a_lead_whose_listing_is_gone_goes_to_the_most_profitable_marketplace_and_its_link_carries_the_lead(db):
    s = Shop(db)
    s.catalog()
    assert s.import_csv("gr-old") == ["IMPORTED"]
    s.worker.drain()
    (pid, status, tier, verdict), = proposals(db)
    detail = s.queue.get(pid)
    assert (detail["payload"]["route_marketplace"], detail["payload"]["route_reason"], detail["payload"]["product_external_id"]) == ("LEMON_SQUEEZY", "MOST_PROFITABLE", "ls-1")
    assert detail_view(detail).route_line == "Checkout on Lemon Squeezy: it leaves the most money after the marketplace's fees"
    s.queue.approve(pid, "alice")
    s.worker.drain()
    assert proposals(db)[0][1] == "SENT"
    lead = one(db, "SELECT id FROM core.lead")
    (sent,) = s.email.transport.sent
    assert f"{LS_URL}?{quote('checkout[custom][ref]', safe='')}={lead}" in sent.text and GR_URL not in sent.text
    assert one(db, "SELECT product_external_id FROM core.lead") == "gr-old"                                                  # the lead's own record is untouched


def test_RE2E3_a_fee_change_after_drafting_does_not_create_a_second_draft_for_the_same_lead(db):
    s = Shop(db)
    s.catalog()
    s.import_csv("gr-old")
    s.worker.drain()
    assert len(proposals(db)) == 1
    db.conn.execute("UPDATE core.marketplace_fee SET percent_bps = 0, fixed_minor = 0 WHERE marketplace = 'GUMROAD'")      # Gumroad is now the best listing
    lead = one(db, "SELECT id FROM core.lead")
    s.drafter.handle(db.conn, Event(id=1, event_id=uuid.uuid4(), event_type=EventType.LEAD_QUALIFIED, marketplace="EMAIL", lead_id=lead,
                                    payload_ref=None, payload={"composite": 75}, trace_id=lead, attempts=1))
    assert len(proposals(db)) == 1 and one(db, "SELECT count(*) FROM core.outbox_event WHERE event_type = 'action.proposed'") == 1


def test_RE2E4_a_product_the_catalog_does_not_know_is_skipped_with_a_recorded_reason_and_nothing_is_drafted(db):
    s = Shop(db)                                                                                                            # an empty catalog
    s.import_csv("gr-1")
    assert s.worker.drain().retried == 0 and proposals(db) == []
    assert "DRAFT_SKIPPED" in audit_actions(db) and one(db, "SELECT outcome FROM audit.event WHERE action_type = 'DRAFT_SKIPPED'") == "PRODUCT_NOT_FOUND"
