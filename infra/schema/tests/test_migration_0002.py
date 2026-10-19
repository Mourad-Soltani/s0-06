"""S1-09 tests for migration 0002 (outbox, kill switch, cost ledger). Requires TEST_ADMIN_DSN (superuser).

Naming: O* outbox, K* kill switch, C* cost, M* migration. None of these names may start with a prefix the
CI gate requires exactly once (test_T15, test_clean_db_up_down_up, test_db_one_migration_behind) and none
may contain "T10", which the CI mutation step selects with -k.
"""
import uuid
from decimal import Decimal

import psycopg
import pytest
from alembic import command
from psycopg import errors
from psycopg.types.json import Json

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)
from test_schema_v0 import W, _bg, _wait_for_lock_waiter, as_role, sqlstate_of


@pytest.fixture()
def w(db):
    return W(db.conn)


def ev(w, event_type="signal.captured", payload=None, lead=None, payload_ref=None, event_id=None):
    return w.one(
        "INSERT INTO core.outbox_event(event_id,event_type,payload,lead_id,payload_ref) "
        "VALUES (COALESCE(%s, gen_random_uuid()),%s,%s,%s,%s) RETURNING id",
        event_id, event_type, Json(payload or {}), lead, payload_ref)


def row(w, q, *a):
    return w.c.execute(q, a).fetchone()


def kill(w, scope="GLOBAL", killed=True, actor="tester", reason="TEST"):
    w.c.execute("SELECT core.set_kill_switch(%s,%s,%s,%s)", (scope, killed, actor, reason))


def ledger_llm(w, cost=1000, ts_sql="now()", call_id=None):
    w.c.execute(
        "INSERT INTO core.cost_ledger(call_id,ts,category,role,model,input_tokens,output_tokens,cost_micro_usd) "
        f"VALUES (COALESCE(%s, gen_random_uuid()), {ts_sql}, 'LLM','COPYWRITER','model-a',100,50,%s)",
        (call_id, cost))


# ======================================================================= outbox
def test_O1_claim_marks_claimed(w):
    i = ev(w)
    rows = w.c.execute("SELECT id, status, attempts, claimed_by FROM core.claim_outbox_events('w1', 10)").fetchall()
    assert rows == [(i, "CLAIMED", 1, "w1")]
    assert w.c.execute("SELECT count(*) FROM core.claim_outbox_events('w2', 10)").fetchone()[0] == 0


def test_O2_concurrent_claims_do_not_overlap(w, db):
    ev(w)
    ev(w)
    A, B = psycopg.connect(db.dsn), psycopg.connect(db.dsn)
    try:
        ra = A.execute("SELECT id FROM core.claim_outbox_events('wA', 1)").fetchall()  # uncommitted, rows locked
        rb = B.execute("SELECT id FROM core.claim_outbox_events('wB', 1)").fetchall()  # SKIP LOCKED
        assert len(ra) == 1 and len(rb) == 1 and ra[0][0] != rb[0][0]
        A.commit()
        B.commit()
    finally:
        A.close()
        B.close()


def test_O3_expired_lease_is_reclaimable(w):
    i = ev(w)
    w.c.execute("SELECT * FROM core.claim_outbox_events('w1', 10, interval '-1 second')")  # lease already over
    rows = w.c.execute("SELECT id, attempts, claimed_by FROM core.claim_outbox_events('w2', 10)").fetchall()
    assert rows == [(i, 2, "w2")]


def test_O4_ack_only_by_current_claimant(w):
    i = ev(w)
    w.c.execute("SELECT * FROM core.claim_outbox_events('w1', 10)")
    assert w.one("SELECT core.outbox_ack(%s,'someone-else')", i) is False
    assert w.one("SELECT core.outbox_ack(%s,'w1')", i) is True
    assert w.one("SELECT status FROM core.outbox_event WHERE id=%s", i) == "DONE"
    assert w.one("SELECT core.outbox_ack(%s,'w1')", i) is False  # already done


def test_O5_fail_backs_off_then_dead_letters(w):
    i = ev(w)
    w.c.execute("SELECT * FROM core.claim_outbox_events('w1', 10)")
    assert w.one("SELECT core.outbox_fail(%s,'w1','HANDLER_ERROR')", i) == "PENDING"
    assert w.one("SELECT available_at > now() FROM core.outbox_event WHERE id=%s", i) is True
    assert w.c.execute("SELECT count(*) FROM core.claim_outbox_events('w1', 10)").fetchone()[0] == 0  # backing off
    w.c.execute("UPDATE core.outbox_event SET available_at = now() WHERE id=%s", (i,))
    w.c.execute("SELECT * FROM core.claim_outbox_events('w1', 10, interval '60 seconds', 2)")
    assert w.one("SELECT core.outbox_fail(%s,'w1','HANDLER_ERROR', 2)", i) == "DEAD"
    assert w.one("SELECT status FROM core.outbox_event WHERE id=%s", i) == "DEAD"
    assert w.one("SELECT core.outbox_fail(%s,'w1','HANDLER_ERROR', 2)", i) is None  # no longer the claimant


def test_O5b_reaper_dead_letters_crashed_claims(w):
    i = ev(w)
    w.c.execute("SELECT * FROM core.claim_outbox_events('w1', 10, interval '-1 second', 1)")
    assert w.one("SELECT core.outbox_reap_dead(1)") == 1
    assert row(w, "SELECT status, last_error_code FROM core.outbox_event WHERE id=%s", i) == ("DEAD", "LEASE_EXPIRED")


@pytest.mark.parametrize("sql", ["UPDATE core.outbox_event SET payload = '{\"x\":1}'::jsonb",
                                 "UPDATE core.outbox_event SET event_type = 'lead.scored'",
                                 "UPDATE core.outbox_event SET event_id = gen_random_uuid()"])
def test_O6_content_is_immutable(w, sql):
    ev(w)
    assert sqlstate_of(lambda: w.c.execute(sql)) == "OB001"


def test_O6b_delivery_columns_are_updatable(w):
    i = ev(w)
    w.c.execute("UPDATE core.outbox_event SET status='DONE' WHERE id=%s", (i,))
    assert w.one("SELECT status FROM core.outbox_event WHERE id=%s", i) == "DONE"


@pytest.mark.parametrize("payload", [{"k": "a@b.co"}, {"k": "x" * 9000}])
def test_O7_payload_rejects_email_shaped_and_oversized(w, payload):
    with pytest.raises(errors.CheckViolation):
        ev(w, payload=payload)


def test_O7b_payload_must_be_an_object(w):
    with pytest.raises(errors.CheckViolation):
        w.c.execute("INSERT INTO core.outbox_event(event_id,event_type,payload) "
                    "VALUES (gen_random_uuid(),'signal.captured','[1]'::jsonb)")


@pytest.mark.parametrize("etype", ["signal", "Signal.captured", "signal.captured.extra", "signal.captured!", ""])
def test_O7c_event_type_shape(w, etype):
    with pytest.raises(errors.CheckViolation):
        ev(w, event_type=etype)


def test_O8_duplicate_event_id_is_idempotent_publish(w):
    eid = uuid.uuid4()
    ev(w, event_id=eid)
    with pytest.raises(errors.UniqueViolation):
        ev(w, event_id=eid)
    w.c.execute("INSERT INTO core.outbox_event(event_id,event_type) VALUES (%s,'signal.captured') "
                "ON CONFLICT (event_id) DO NOTHING", (eid,))
    assert w.one("SELECT count(*) FROM core.outbox_event WHERE event_id=%s", eid) == 1


def test_O9_erasure_nulls_payload_ref_and_keeps_event(w):
    s = w.erasable()
    i = ev(w, lead=s["lead"], payload_ref=s["payload"])
    w.c.execute("SELECT core.erase_lead(%s,'test')", (s["lead"],))
    assert row(w, "SELECT payload_ref, lead_id FROM core.outbox_event WHERE id=%s", i) == (None, s["lead"])


def test_O10_app_rw_privileges(w):
    i = ev(w)
    as_role(w.c, "app_rw", lambda: w.c.execute("UPDATE core.outbox_event SET status='DONE' WHERE id=%s", (i,)))
    with pytest.raises(errors.InsufficientPrivilege):
        as_role(w.c, "app_rw", lambda: w.c.execute("UPDATE core.outbox_event SET payload='{}'::jsonb"))
    with pytest.raises(errors.InsufficientPrivilege):
        as_role(w.c, "app_rw", lambda: w.c.execute("DELETE FROM core.outbox_event"))
    as_role(w.c, "app_rw", lambda: w.c.execute("SELECT * FROM core.claim_outbox_events('w1')").fetchall())


def test_O10b_functions_not_executable_by_public(w):
    with pytest.raises(errors.InsufficientPrivilege):  # pii_svc has schema USAGE but no EXECUTE grant
        as_role(w.c, "pii_svc", lambda: w.c.execute("SELECT * FROM core.claim_outbox_events('w')").fetchall())
    with pytest.raises(errors.InsufficientPrivilege):
        as_role(w.c, "pii_svc", lambda: w.c.execute("SELECT core.set_kill_switch('GLOBAL', true, 'a', 'R')"))


def test_O11_consumer_receipt_prevents_double_processing(w):
    eid = uuid.uuid4()
    ev(w, event_id=eid)
    w.c.execute("INSERT INTO core.consumer_receipt(consumer,event_id) VALUES ('audit',%s)", (eid,))
    with pytest.raises(errors.UniqueViolation):
        w.c.execute("INSERT INTO core.consumer_receipt(consumer,event_id) VALUES ('audit',%s)", (eid,))
    w.c.execute("INSERT INTO core.consumer_receipt(consumer,event_id) VALUES ('analytics',%s)", (eid,))  # other consumer ok
    with pytest.raises(errors.ForeignKeyViolation):
        w.c.execute("INSERT INTO core.consumer_receipt(consumer,event_id) VALUES ('audit',gen_random_uuid())")


# ======================================================================= kill switch
def test_K1_seeded_rows(w):
    assert w.one("SELECT count(*) FROM core.kill_switch WHERE is_global") == 1
    n_enum = w.one("SELECT count(*) FROM unnest(enum_range(NULL::core.marketplace))")
    assert w.one("SELECT count(*) FROM core.kill_switch WHERE NOT is_global") == n_enum
    assert w.one("SELECT count(*) FROM core.kill_switch WHERE killed") == 0


def test_K2_global_kill_emits_exactly_one_event(w):
    kill(w, "GLOBAL")
    assert w.one("SELECT core.is_killed('GUMROAD')") is True
    kill(w, "GLOBAL", actor="someone-else", reason="OTHER")  # repeat: no-op, must not overwrite or re-emit
    assert row(w, "SELECT killed_by, reason_code FROM core.kill_switch WHERE is_global") == ("tester", "TEST")
    n, scope, reason = row(w, "SELECT count(*), max(payload->>'scope'), max(payload->>'reason_code') "
                              "FROM core.outbox_event WHERE event_type='adapter.killed'")
    assert (n, scope, reason) == (1, "GLOBAL", "TEST")


def test_K3_marketplace_kill_is_scoped(w):
    kill(w, "GUMROAD")
    assert w.one("SELECT core.is_killed('GUMROAD')") is True
    assert w.one("SELECT core.is_killed('PAYHIP')") is False
    mk, scope = row(w, "SELECT marketplace::text, payload->>'scope' FROM core.outbox_event WHERE event_type='adapter.killed'")
    assert (mk, scope) == ("GUMROAD", "GUMROAD")


@pytest.mark.parametrize("killed_scope,blocked", [("GLOBAL", True), ("EMAIL", True), ("GUMROAD", False)])
def test_K4_kill_blocks_sending(w, killed_scope, blocked):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c))  # marketplace and channel are EMAIL, permission is valid
    kill(w, killed_scope)
    if blocked:
        assert sqlstate_of(lambda: w.send(p)) == "CP003"
        assert w.one("SELECT status FROM core.action_proposal WHERE id=%s", p) == "APPROVED"
    else:
        w.send(p)
        assert w.one("SELECT status FROM core.action_proposal WHERE id=%s", p) == "SENDING"


def test_K4b_unkill_restores_sending(w):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c))
    kill(w, "GLOBAL")
    assert sqlstate_of(lambda: w.send(p)) == "CP003"
    kill(w, "GLOBAL", killed=False, actor="operator")
    assert row(w, "SELECT killed, reason_code, killed_by FROM core.kill_switch WHERE is_global") == (False, None, "operator")
    w.send(p)


def test_K4c_insert_as_sending_is_blocked_while_killed(w):
    c = w.contact()
    lead, perm = w.lead(c), w.perm(c)
    kill(w, "GLOBAL")
    assert sqlstate_of(lambda: w.proposal(lead, perm, status="SENDING")) == "CP003"


def test_K5_input_validation(w):
    assert sqlstate_of(lambda: kill(w, actor="  ")) == "CP004"
    assert sqlstate_of(lambda: kill(w, reason=None)) == "CP004"
    with pytest.raises(errors.CheckViolation):
        kill(w, actor="ops@example.test")  # email-shaped actor
    with pytest.raises(errors.CheckViolation):
        kill(w, reason="not a code")
    with pytest.raises(errors.InvalidTextRepresentation):
        kill(w, scope="NOT_A_MARKETPLACE")
    assert w.one("SELECT count(*) FROM core.kill_switch WHERE killed") == 0


def test_K6_kill_switch_privileges(w):
    with pytest.raises(errors.InsufficientPrivilege):
        as_role(w.c, "app_rw", lambda: w.c.execute("UPDATE core.kill_switch SET killed=false"))
    as_role(w.c, "app_rw", lambda: kill(w, "GLOBAL"))
    assert w.one("SELECT core.is_killed('PAYHIP')") is True
    assert as_role(w.c, "app_rw", lambda: w.c.execute("SELECT core.is_killed('PAYHIP')").fetchone()[0]) is True


def test_K7a_kill_waits_for_in_flight_send(w, db):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c))
    A, B = psycopg.connect(db.dsn), psycopg.connect(db.dsn)
    try:
        A.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (p,))  # holds FOR SHARE, uncommitted

        def do_kill():
            B.execute("SET lock_timeout='15s'")
            B.execute("SELECT core.set_kill_switch('GLOBAL', true, 'tester', 'TEST')")
            B.commit()

        t, out = _bg(do_kill)
        _wait_for_lock_waiter(w, out, "set_kill_switch")   # the kill is provably blocked behind the send
        assert t.is_alive() and not out
        A.commit()
        t.join(20)
        assert "err" not in out
        assert w.one("SELECT status FROM core.action_proposal WHERE id=%s", p) == "SENDING"  # valid at send time
        assert w.one("SELECT core.is_killed('EMAIL')") is True
    finally:
        A.close()
        B.close()


def test_K7b_send_waits_for_in_flight_kill_then_fails(w, db):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c))
    A, B = psycopg.connect(db.dsn), psycopg.connect(db.dsn)  # READ COMMITTED (default)
    try:
        B.execute("SELECT core.set_kill_switch('GLOBAL', true, 'tester', 'TEST')")  # uncommitted

        def send():
            A.execute("SET lock_timeout='15s'")
            A.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (p,))
            A.commit()

        t, out = _bg(send)
        _wait_for_lock_waiter(w, out, "action_proposal")   # the send is provably blocked behind the kill
        assert t.is_alive() and not out
        B.commit()
        t.join(20)
        assert out["err"].sqlstate == "CP003"
    finally:
        A.close()
        B.close()


# ======================================================================= cost ledger
def test_C1_config_seeded(w):
    assert row(w, "SELECT monthly_ceiling_micro_usd, alert_pct FROM core.cost_config") == (300_000_000, 80)
    with pytest.raises(errors.UniqueViolation):
        w.c.execute("INSERT INTO core.cost_config(monthly_ceiling_micro_usd) VALUES (1)")


def test_C2_status_sums_one_utc_month(w):
    month_start = "(date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
    ledger_llm(w, 50_000_000, f"{month_start} + interval '1 hour'")
    w.c.execute(
        "INSERT INTO core.cost_ledger(call_id,ts,category,item_code,cost_micro_usd) "
        f"VALUES (gen_random_uuid(), {month_start} + interval '2 hours', 'INFRA','HOSTING',100000000)")
    ledger_llm(w, 999_000_000, f"{month_start} - interval '1 day'")   # previous month, must not count
    period, spent, ceiling, pct = row(w, "SELECT * FROM core.cost_status()")
    assert (spent, ceiling, pct) == (150_000_000, 300_000_000, Decimal("50.00"))


def test_C3_duplicate_call_id_is_rejected(w):
    cid = uuid.uuid4()
    ledger_llm(w, call_id=cid)
    with pytest.raises(errors.UniqueViolation):
        ledger_llm(w, call_id=cid)


@pytest.mark.parametrize("stmt", ["UPDATE core.cost_ledger SET cost_micro_usd = 0", "DELETE FROM core.cost_ledger",
                                  "TRUNCATE core.cost_ledger"])
def test_C4_ledger_append_only(w, stmt):
    ledger_llm(w)
    assert sqlstate_of(lambda: w.c.execute(stmt)) == "CL001"          # owner: trigger blocks
    with pytest.raises(errors.InsufficientPrivilege):                  # app_rw: grants block
        as_role(w.c, "app_rw", lambda: w.c.execute(stmt))


@pytest.mark.parametrize("bad", [
    "'LLM','COPYWRITER','m',-1,5,10,NULL",          # negative tokens
    "'LLM','COPYWRITER','m',1,5,-10,NULL",          # negative cost
    "'LLM','COPYWRITER','has space',1,5,10,NULL",   # model alphabet
    "'LLM','COPYWRITER','a@b',1,5,10,NULL",         # email-shaped model
    "'LLM',NULL,'m',1,5,10,NULL",                   # LLM row without role
    "'INFRA',NULL,NULL,NULL,NULL,10,NULL",          # INFRA row without item_code
    "'INFRA','COPYWRITER',NULL,NULL,NULL,10,'X'",   # INFRA row carrying LLM fields
    "'LLM','COPYWRITER','m',1,5,10,'X'",            # LLM row carrying item_code
    "'OTHER',NULL,NULL,NULL,NULL,10,'X'"])
def test_C5_row_shape_checks(w, bad):
    with pytest.raises(errors.CheckViolation):
        w.c.execute("INSERT INTO core.cost_ledger(call_id,category,role,model,input_tokens,output_tokens,"
                    "cost_micro_usd,item_code) VALUES (gen_random_uuid(), " + bad + ")")


def test_C6_alert_fires_once_per_month_and_threshold(w):
    assert w.one("SELECT core.try_fire_cost_alert('2026-10-01', 80::smallint)") is True
    assert w.one("SELECT core.try_fire_cost_alert('2026-10-01', 80::smallint)") is False
    assert w.one("SELECT core.try_fire_cost_alert('2026-10-01', 100::smallint)") is True
    assert w.one("SELECT core.try_fire_cost_alert('2026-11-01', 80::smallint)") is True


def test_C7_cost_privileges(w):
    as_role(w.c, "app_rw", lambda: ledger_llm(w))
    with pytest.raises(errors.InsufficientPrivilege):
        as_role(w.c, "app_rw", lambda: w.c.execute("UPDATE core.cost_config SET alert_pct = 1"))
    assert as_role(w.c, "app_rw", lambda: w.c.execute("SELECT spent_micro_usd FROM core.cost_status()").fetchone()[0]) == 1000


# ======================================================================= migration
def test_M1_downgrade_to_0001_removes_only_0002(fresh_db):
    name = fresh_db
    cfg = _cfg(name)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        for t in ("outbox_event", "consumer_receipt", "kill_switch", "cost_ledger", "cost_config", "cost_alert"):
            assert q(f"SELECT to_regclass('core.{t}') IS NULL") is True, t
        assert q("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                 "WHERE n.nspname='core' AND p.proname IN ('claim_outbox_events','outbox_ack','outbox_fail',"
                 "'outbox_reap_dead','is_killed','set_kill_switch','emit_adapter_killed','enforce_not_killed',"
                 "'outbox_guard','cost_ledger_block','cost_status','try_fire_cost_alert')") == 0
        assert q("SELECT count(*) FROM pg_trigger WHERE tgname='action_proposal_kill_gate' AND NOT tgisinternal") == 0
        assert q("SELECT count(*) FROM pg_trigger WHERE tgname='action_proposal_send_gate' AND NOT tgisinternal") == 1
        assert q("SELECT to_regclass('core.action_proposal') IS NOT NULL") is True
        assert q("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                 "WHERE n.nspname='core' AND p.proname='erase_lead'") == 1
    command.upgrade(cfg, "head")                                    # and it re-applies cleanly
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM core.kill_switch WHERE is_global").fetchone()[0] == 1
