"""Cost tracker against Postgres. Needs TEST_ADMIN_DSN. Names are CX*."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg import errors

from shared.cost import (BudgetExhausted, CostTracker, ModelPrice, PriceBook, RecordingNotifier, ROLES,
                         UnpricedModel)

BOOK = PriceBook({"small": ModelPrice(300_000, 1_500_000), "strong": ModelPrice(3_000_000, 15_000_000)})
CEILING = 300_000_000


class FlakyNotifier:
    def __init__(self):
        self.fail, self.delivered = True, []

    def notify(self, alert):
        if self.fail:
            raise OSError("channel down")
        self.delivered.append(alert)


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def tracker(db, notifier=None):
    return CostTracker(db.conn, BOOK, notifier or RecordingNotifier())


def infra(t, micro, **kw):
    return t.record_infra(uuid.uuid4(), "HOSTING", micro, **kw)


def test_CX1_records_a_priced_call_once(db):
    t, cid = tracker(db), uuid.uuid4()
    r = t.record_llm_call(cid, "COPYWRITER", "strong", 2_000, 500)
    assert (r.cost_micro_usd, r.inserted) == (13_500, True)
    again = t.record_llm_call(cid, "COPYWRITER", "strong", 2_000, 500)
    assert again.inserted is False and t.status().spent_micro_usd == 13_500        # a retry does not double count
    assert db.conn.execute("SELECT role::text, model, input_tokens, output_tokens, cost_micro_usd, category "
                           "FROM core.cost_ledger").fetchone() == ("COPYWRITER", "strong", 2_000, 500, 13_500, "LLM")


def test_CX2_status_sums_one_utc_month(db):
    t = tracker(db)
    infra(t, 150_000_000)
    last_month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    infra(t, 999_000_000, ts=last_month)                                            # previous month: must not count
    st = t.status()
    assert (st.spent_micro_usd, st.ceiling_micro_usd, st.pct_used) == (150_000_000, CEILING, Decimal("50.00"))


def test_CX3_the_80_percent_alert_fires_once(db):
    n = RecordingNotifier()
    t = tracker(db, n)
    assert infra(t, 239_999_999).alerts == () and n.alerts == []                    # 79.99%: nothing yet
    r = infra(t, 1)                                                                  # now exactly 80%
    assert [(a.threshold_pct, a.delivered) for a in r.alerts] == [(80, True)]
    assert len(n.alerts) == 1 and (n.alerts[0].threshold_pct, n.alerts[0].spent_micro_usd) == (80, 240_000_000)
    assert infra(t, 5).alerts == () and len(n.alerts) == 1                          # not again this month
    assert one(db, "SELECT count(*) FROM core.cost_alert") == 1


def test_CX4_the_ceiling_alert_and_a_jump_past_both_thresholds(db):
    n = RecordingNotifier()
    r = infra(tracker(db, n), CEILING)                                               # 0% to 100% in one record
    assert [a.threshold_pct for a in r.alerts] == [80, 100] and [a.threshold_pct for a in n.alerts] == [80, 100]


def test_CX5_a_failed_delivery_is_retried_not_lost(db):
    n = FlakyNotifier()
    t = tracker(db, n)
    r = infra(t, 240_000_000)
    assert [(a.threshold_pct, a.delivered, a.error_code) for a in r.alerts] == [(80, False, "OSERROR")]
    assert one(db, "SELECT count(*) FROM core.cost_alert") == 0                      # NOT marked fired
    n.fail = False
    r2 = infra(t, 1)                                                                 # the next record retries it
    assert [(a.threshold_pct, a.delivered) for a in r2.alerts] == [(80, True)] and len(n.delivered) == 1
    assert one(db, "SELECT count(*) FROM core.cost_alert") == 1


def test_CX6_headroom_allows_the_ceiling_and_refuses_one_micro_dollar_more(db):
    t = tracker(db)
    infra(t, CEILING - 1_000)
    t.require_headroom(1_000)
    with pytest.raises(BudgetExhausted):
        t.require_headroom(1_001)


def test_CX7_inputs_are_validated_and_unpriced_models_write_nothing(db):
    t = tracker(db)
    with pytest.raises(UnpricedModel):
        t.record_llm_call(uuid.uuid4(), "SCOUT", "mystery", 1, 1)
    for call in (lambda: t.record_llm_call(uuid.uuid4(), "INTERN", "small", 1, 1),
                 lambda: t.record_infra(uuid.uuid4(), "not a code", 1), lambda: t.record_infra(uuid.uuid4(), "X", -1)):
        with pytest.raises(ValueError):
            call()
    assert one(db, "SELECT count(*) FROM core.cost_ledger") == 0


def test_CX8_works_as_app_rw(db):
    db.conn.execute("SET ROLE app_rw")
    try:
        t = tracker(db)
        t.record_llm_call(uuid.uuid4(), "SCOUT", "small", 1_000, 100)
        assert t.status().spent_micro_usd > 0 and t.check_alerts() == []
        with pytest.raises(errors.InsufficientPrivilege):
            db.conn.execute("UPDATE core.cost_ledger SET cost_micro_usd = 0")
    finally:
        db.conn.execute("RESET ROLE")


def test_CX9_roles_match_the_database_enum(db):
    rows = db.conn.execute("SELECT unnest(enum_range(NULL::core.agent_role))::text").fetchall()
    assert tuple(r[0] for r in rows) == ROLES
