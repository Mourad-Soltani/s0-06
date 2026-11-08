"""A real sale, end to end, on Postgres: a signed Lemon Squeezy webhook reaches the receiver, lands in the inbox, the Scout loop turns it into a lead
and a signal.captured event, the worker's Qualifier scores it, and the Drafter then finds NO consented contact and drafts nothing. That last step is
the design (ADR-21): a marketplace sale carries no consent to be emailed, so the sale becomes a scored lead, not a message. The provider is a fake
transport. Needs TEST_ADMIN_DSN. Names SP*."""
from contextlib import contextmanager

from adapters.lemon_squeezy.adapter import LemonSqueezyAdapter
from adapters.lemon_squeezy.fake_transport import FakeLemonSqueezyTransport
from orchestrator.drafter import DraftingConsumer
from orchestrator.inbox_access import DsnSignalInbox
from orchestrator.lead_store import PgLeadRepo
from orchestrator.qualifier import Qualifier
from orchestrator.scoring import IcpDefinition, RuleScorer
from orchestrator.tests.test_pipeline_e2e_pg import audit_actions, one
from orchestrator.tests.test_webhook_pg import LS_SECRET, LS_TOKEN, ls_body, post, send_ls
from orchestrator.webhook_app import WebhookApp
from orchestrator.worker_main import ScoutLoop, scout_serve
from adapters.lemon_squeezy.webhook import LemonSqueezyWebhook
from shared.bus.store import PgOutboxStore
from shared.bus.worker import Worker
from shared.config import load_settings

SETTINGS = load_settings({
    "DATABASE_URL": "postgresql://u:pw-0123456789abcdefgh@db:5432/sap", "POSTMARK_SERVER_TOKEN": "t", "EMAIL_FROM": "hello@seller.example.com",
    "EMAIL_POSTAL_ADDRESS": "1 Example Street, Paris 75001, France", "EMAIL_UNSUBSCRIBE_URL": "https://seller.example.com/u", "ALLOWED_LINK_HOSTS": "gumroad.com",
    "ICP_PRODUCT_IDS": "456", "LEMON_SQUEEZY_API_KEY": "k", "LEMON_SQUEEZY_STORE_ID": "1"})


class NeverWrites:
    def create(self, *a, **k):
        raise AssertionError("a draft must not be written for a lead without a consented contact")


class NeverAsked:
    def get_product(self, *a, **k):
        raise AssertionError("no product lookup without a contact")


def pipeline(db):
    inbox = DsnSignalInbox(db.dsn)                                                       # the per-call inbox: a fresh connection for every write and read
    app = WebhookApp(inbox, {"lemonsqueezy": (LemonSqueezyWebhook(LS_SECRET, 1), LS_TOKEN)})

    @contextmanager
    def connect():
        yield db.conn
    loop = ScoutLoop(SETTINGS, connect, inbox, adapters={"LEMON_SQUEEZY": LemonSqueezyAdapter(FakeLemonSqueezyTransport(), inbox)})
    worker = Worker(PgOutboxStore(db.conn), "w1")
    Qualifier(RuleScorer(IcpDefinition(frozenset({"456"}))), PgLeadRepo()).register(worker)
    DraftingConsumer(lambda conn: NeverWrites(), NeverAsked()).register(worker)
    return app, loop, worker


def test_SP1_a_signed_sale_becomes_a_scored_lead_and_no_message(db):
    app, loop, worker = pipeline(db)
    assert send_ls(app) == (200, {"ok": True, "accepted": 1, "duplicates": 0})
    (r,) = loop.run_once()
    assert (r.marketplace, r.status, r.new_leads) == ("LEMON_SQUEEZY", "ok", 1)
    assert worker.drain().retried == 0
    lead = db.conn.execute("SELECT source_type, source_marketplace::text, signal_ref, status::text, contact_id FROM core.lead").fetchone()
    assert lead[:3] == ("MARKETPLACE_SIGNAL", "LEMON_SQUEEZY", "ls_order_123") and lead[3] != "SIGNAL_CAPTURED" and lead[4] is None      # scored, no contact
    assert one(db, "SELECT count(*) FROM core.outbox_event WHERE event_type = 'signal.captured'") == 1
    assert one(db, "SELECT count(*) FROM core.action_proposal") == 0 and one(db, "SELECT count(*) FROM core.outbox_event WHERE event_type = 'action.proposed'") == 0
    if one(db, "SELECT count(*) FROM core.outbox_event WHERE event_type = 'lead.qualified'"):          # a qualified lead reaches the Drafter, which records why it stopped
        assert "DRAFT_SKIPPED" in audit_actions(db) and one(db, "SELECT outcome FROM audit.event WHERE action_type = 'DRAFT_SKIPPED'") == "NO_CONTACT"


def test_SP2_a_provider_retry_or_a_second_cycle_creates_nothing_new(db):
    app, loop, worker = pipeline(db)
    send_ls(app)
    loop.run_once()
    assert send_ls(app)[1] == {"ok": True, "accepted": 0, "duplicates": 1}
    assert loop.run_once()[0].new_leads == 0 and one(db, "SELECT count(*) FROM core.lead") == 1


def test_SP3_a_request_that_fails_a_check_never_reaches_the_scout(db):
    app, loop, _ = pipeline(db)
    assert send_ls(app, signature="0" * 64)[0] == 401 and send_ls(app, token="wrong-token-0123456789")[0] == 404
    assert post(app, f"/webhooks/lemonsqueezy/{LS_TOKEN}", b"{}", {})[0] in (400, 401)
    assert loop.run_once()[0].new_leads == 0 and one(db, "SELECT count(*) FROM core.lead") == 0


def test_SP4_the_loop_function_runs_a_real_cycle_and_stops_when_told(db):
    app, loop, _ = pipeline(db)
    send_ls(app, ls_body(order="900"))

    class Stop:
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            return self.n > 1

        def wait(self, t):
            pass

    scout_serve(loop, Stop(), interval=0.0)
    assert one(db, "SELECT count(*) FROM core.lead WHERE signal_ref = 'ls_order_900'") == 1
