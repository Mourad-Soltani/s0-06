"""Migration 0003: queues for a killed marketplace pause at claim time. Needs TEST_ADMIN_DSN. Names are KQ*."""
import pytest
from alembic import command
from psycopg import errors

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)
from test_migration_0002 import kill
from test_schema_v0 import W, as_role

import psycopg


@pytest.fixture()
def w(db):
    return W(db.conn)


def evm(w, marketplace, event_type="lead.scored"):
    return w.one("INSERT INTO core.outbox_event(event_id,event_type,marketplace) "
                 "VALUES (gen_random_uuid(),%s,%s) RETURNING id", event_type, marketplace)


def claim(w, worker="w1", event_type="lead.scored"):
    """Ids claimed by this worker. Filtered to one event type by default, because killing a marketplace itself
    emits an adapter.killed event for that marketplace, and adapter.* events are deliberately never paused."""
    q = "SELECT id FROM core.claim_outbox_events(%s, 50)"
    if event_type is None:
        return [r[0] for r in w.c.execute(q, (worker,)).fetchall()]
    return [r[0] for r in w.c.execute(q + " WHERE event_type = %s", (worker, event_type)).fetchall()]


def test_KQ1_paused_marketplace_is_not_claimed(w):
    g, p = evm(w, "GUMROAD"), evm(w, "PAYHIP")
    kill(w, "GUMROAD")
    assert claim(w) == [p]                                                     # only the live marketplace
    assert w.c.execute("SELECT status, attempts FROM core.outbox_event WHERE id=%s", (g,)).fetchone() == ("PENDING", 0)
    kill(w, "GUMROAD", killed=False, actor="ops-1")
    assert claim(w, "w2") == [g]                                               # resumes, first attempt


def test_KQ2_global_kill_pauses_tagged_events_only(w):
    tagged = evm(w, "GUMROAD")
    untagged = evm(w, None)                                                    # no marketplace: never paused
    notice = evm(w, "GUMROAD", "adapter.rate_limited")                         # adapter.*: never paused
    kill(w, "GLOBAL")
    claimed = claim(w, event_type=None)
    assert tagged not in claimed
    assert {untagged, notice} <= set(claimed)
    kinds = {r[0] for r in w.c.execute("SELECT event_type FROM core.outbox_event WHERE id = ANY(%s)", (claimed,)).fetchall()}
    assert "adapter.killed" in kinds                                           # the kill notice itself flows


@pytest.mark.parametrize("killed,marketplace,event_type,paused", [
    (None, "GUMROAD", "lead.scored", False),
    ("GUMROAD", "GUMROAD", "lead.scored", True),
    ("GUMROAD", "PAYHIP", "lead.scored", False),
    ("GLOBAL", "PAYHIP", "lead.scored", True),
    ("GLOBAL", None, "lead.scored", False),
    ("GLOBAL", "PAYHIP", "adapter.killed", False),
    ("GUMROAD", "GUMROAD", "adapter.rate_limited", False)])
def test_KQ3_pause_truth_table(w, killed, marketplace, event_type, paused):
    if killed:
        kill(w, killed)
    assert w.one("SELECT core.outbox_paused(%s::core.marketplace, %s)", marketplace, event_type) is paused


def test_KQ4_outbox_paused_is_not_executable_by_public(w):
    assert as_role(w.c, "app_rw", lambda: w.c.execute("SELECT core.outbox_paused('GUMROAD','lead.scored')").fetchone()[0]) is False
    with pytest.raises(errors.InsufficientPrivilege):
        as_role(w.c, "pii_svc", lambda: w.c.execute("SELECT core.outbox_paused('GUMROAD','lead.scored')").fetchone())


def test_KQ5_downgrade_restores_the_0002_claim(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        sig = "'core.claim_outbox_events(text,integer,interval,integer)'::regprocedure"
        assert "outbox_paused" not in q(f"SELECT pg_get_functiondef({sig})")
        assert q("SELECT to_regprocedure('core.outbox_paused(core.marketplace,text)') IS NULL") is True
        assert q("SELECT to_regclass('core.outbox_event') IS NOT NULL") is True      # 0002 intact
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert "outbox_paused" in c.execute("SELECT pg_get_functiondef("
            "'core.claim_outbox_events(text,integer,interval,integer)'::regprocedure)").fetchone()[0]
