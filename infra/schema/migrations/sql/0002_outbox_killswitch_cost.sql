-- =====================================================================
-- 0002_outbox_killswitch_cost.sql | Alembic revision 0002 | S1-09 | DRAFT v0, UNTESTED
-- Transaction control: NONE in this file (Alembic owns the transaction).
-- Adds: Postgres outbox (ADR-13), kill switch (spec 6.3), cost ledger (budget $300/mo).
-- Does NOT redefine anything from 0001. The send-gate trigger and its FOR SHARE locks are untouched,
-- so the 0001 mutation tripwire (count of 'FOR SHARE;' in 0001) is unaffected.
-- SQLSTATEs introduced: CP003 send blocked by kill switch | CP004 invalid kill-switch input
--                       OB001 outbox event content is immutable | CL001 cost ledger is append-only
-- Send lock order is now: kill_switch -> contact -> contact_permission. erase_lead and
-- set_kill_switch each take a prefix of that order, so none of them can deadlock with a send.
-- No PII in any table here: opaque ids, enums and codes only, enforced by CHECKs.
-- =====================================================================

-- ---------- outbox ---------------------------------------------------
CREATE TABLE core.outbox_event (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id        uuid NOT NULL UNIQUE,                      -- idempotency key for publish and consume
  event_type      text NOT NULL CHECK (event_type ~ '^[a-z][a-z_]*\.[a-z][a-z_]*$'),
  marketplace     core.marketplace,
  lead_id         uuid REFERENCES core.lead(id),
  payload_ref     uuid REFERENCES pii.payload_store(payload_id) ON DELETE SET NULL,
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,        -- ids and codes only, never bodies
  trace_id        uuid,
  created_at      timestamptz NOT NULL DEFAULT now(),
  status          text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','CLAIMED','DONE','DEAD')),
  available_at    timestamptz NOT NULL DEFAULT now(),
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  claimed_by      text CHECK (claimed_by IS NULL OR claimed_by ~ '^[A-Za-z0-9_:.-]{1,128}$'),
  claimed_until   timestamptz,
  last_error_code text CHECK (last_error_code IS NULL OR last_error_code ~ '^[A-Z0-9_]{1,64}$'),
  CHECK (jsonb_typeof(payload) = 'object'),
  CHECK (octet_length(payload::text) <= 8192),
  CHECK (payload::text !~ '@'),                              -- blocks email-shaped values
  CHECK (status <> 'CLAIMED' OR (claimed_by IS NOT NULL AND claimed_until IS NOT NULL))
);
CREATE INDEX outbox_event_claimable_idx ON core.outbox_event (available_at)
  WHERE status IN ('PENDING','CLAIMED');

CREATE TABLE core.consumer_receipt (                         -- consumers insert in the same tx as their effect
  consumer     text NOT NULL CHECK (consumer ~ '^[A-Za-z0-9_:.-]{1,128}$'),
  event_id     uuid NOT NULL REFERENCES core.outbox_event(event_id),
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);

CREATE FUNCTION core.outbox_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.event_id, NEW.event_type, NEW.marketplace, NEW.lead_id, NEW.payload, NEW.trace_id, NEW.created_at)
     IS DISTINCT FROM
     (OLD.event_id, OLD.event_type, OLD.marketplace, OLD.lead_id, OLD.payload, OLD.trace_id, OLD.created_at) THEN
    RAISE EXCEPTION 'outbox event content is immutable' USING ERRCODE = 'OB001';
  END IF;
  RETURN NEW;                                                -- payload_ref excluded: erasure nulls it via FK
END $$;

CREATE TRIGGER outbox_event_immutable
  BEFORE UPDATE ON core.outbox_event
  FOR EACH ROW EXECUTE FUNCTION core.outbox_guard();

-- Claim up to p_limit events. Skips rows another worker holds locked. A lease that has expired makes a
-- CLAIMED row claimable again (worker crashed). Rows at p_max_attempts are not claimed; reap them.
CREATE FUNCTION core.claim_outbox_events(
  p_worker text, p_limit integer DEFAULT 10, p_lease interval DEFAULT interval '60 seconds',
  p_max_attempts integer DEFAULT 5)
RETURNS SETOF core.outbox_event
LANGUAGE sql AS $$
  UPDATE core.outbox_event o
     SET status = 'CLAIMED', claimed_by = p_worker, claimed_until = now() + p_lease, attempts = o.attempts + 1
   WHERE o.id IN (
     SELECT e.id FROM core.outbox_event e
      WHERE e.attempts < p_max_attempts
        AND ((e.status = 'PENDING' AND e.available_at <= now())
          OR (e.status = 'CLAIMED' AND e.claimed_until < now()))
      ORDER BY e.id
      FOR UPDATE SKIP LOCKED
      LIMIT p_limit)
  RETURNING o.*;
$$;

CREATE FUNCTION core.outbox_ack(p_id bigint, p_worker text) RETURNS boolean
LANGUAGE sql AS $$
  WITH u AS (
    UPDATE core.outbox_event SET status = 'DONE', claimed_until = NULL
     WHERE id = p_id AND status = 'CLAIMED' AND claimed_by = p_worker
    RETURNING 1)
  SELECT EXISTS (SELECT 1 FROM u);
$$;

-- Returns the new status ('PENDING' or 'DEAD'), or NULL if the caller no longer holds the claim.
CREATE FUNCTION core.outbox_fail(
  p_id bigint, p_worker text, p_error_code text,
  p_max_attempts integer DEFAULT 5, p_backoff interval DEFAULT interval '30 seconds')
RETURNS text
LANGUAGE sql AS $$
  UPDATE core.outbox_event
     SET status = CASE WHEN attempts >= p_max_attempts THEN 'DEAD' ELSE 'PENDING' END,
         available_at = CASE WHEN attempts >= p_max_attempts THEN available_at ELSE now() + p_backoff * attempts END,
         claimed_by = NULL, claimed_until = NULL, last_error_code = p_error_code
   WHERE id = p_id AND status = 'CLAIMED' AND claimed_by = p_worker
  RETURNING status;
$$;

-- Dead-letters events that ran out of attempts without an explicit failure (crashed workers).
CREATE FUNCTION core.outbox_reap_dead(p_max_attempts integer DEFAULT 5) RETURNS integer
LANGUAGE sql AS $$
  WITH u AS (
    UPDATE core.outbox_event
       SET status = 'DEAD', claimed_by = NULL, claimed_until = NULL,
           last_error_code = COALESCE(last_error_code, 'LEASE_EXPIRED')
     WHERE attempts >= p_max_attempts
       AND (status = 'PENDING' OR (status = 'CLAIMED' AND claimed_until < now()))
    RETURNING 1)
  SELECT count(*)::integer FROM u;
$$;

-- ---------- kill switch ----------------------------------------------
CREATE TABLE core.kill_switch (
  id          smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  is_global   boolean NOT NULL DEFAULT false,
  marketplace core.marketplace,
  killed      boolean NOT NULL DEFAULT false,
  reason_code text CHECK (reason_code IS NULL OR reason_code ~ '^[A-Z0-9_]{1,64}$'),
  killed_by   text CHECK (killed_by IS NULL OR killed_by ~ '^[A-Za-z0-9_:.-]{1,128}$'),
  killed_at   timestamptz,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CHECK ((is_global AND marketplace IS NULL) OR (NOT is_global AND marketplace IS NOT NULL)),
  CHECK (NOT killed OR (reason_code IS NOT NULL AND killed_by IS NOT NULL AND killed_at IS NOT NULL))
);
CREATE UNIQUE INDEX kill_switch_global_uq      ON core.kill_switch (is_global) WHERE is_global;
CREATE UNIQUE INDEX kill_switch_marketplace_uq ON core.kill_switch (marketplace) WHERE marketplace IS NOT NULL;

INSERT INTO core.kill_switch (is_global) VALUES (true);
INSERT INTO core.kill_switch (marketplace) SELECT unnest(enum_range(NULL::core.marketplace));

CREATE FUNCTION core.is_killed(p_marketplace core.marketplace) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(bool_or(killed), false) FROM core.kill_switch WHERE is_global OR marketplace = p_marketplace;
$$;

-- The only way to change the switch (direct UPDATE is not granted). Repeating a state is a no-op, so a
-- second kill cannot overwrite who killed first, and emits no second event.
CREATE FUNCTION core.set_kill_switch(p_scope text, p_killed boolean, p_actor text, p_reason_code text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, core AS $$
DECLARE
  v_id  smallint;
  v_cur boolean;
BEGIN
  IF p_actor IS NULL OR length(btrim(p_actor)) = 0 THEN
    RAISE EXCEPTION 'kill switch change requires an actor' USING ERRCODE = 'CP004';
  END IF;
  IF p_killed AND p_reason_code IS NULL THEN
    RAISE EXCEPTION 'kill requires a reason_code' USING ERRCODE = 'CP004';
  END IF;

  IF p_scope = 'GLOBAL' THEN
    SELECT id, killed INTO v_id, v_cur FROM core.kill_switch WHERE is_global FOR UPDATE;
  ELSE
    SELECT id, killed INTO v_id, v_cur FROM core.kill_switch WHERE marketplace = p_scope::core.marketplace FOR UPDATE;
  END IF;
  IF v_id IS NULL THEN
    RAISE EXCEPTION 'unknown kill-switch scope %', p_scope USING ERRCODE = 'CP004';
  END IF;
  IF v_cur = p_killed THEN
    RETURN;
  END IF;

  UPDATE core.kill_switch
     SET killed      = p_killed,
         reason_code = CASE WHEN p_killed THEN p_reason_code END,
         killed_by   = p_actor,
         killed_at   = CASE WHEN p_killed THEN now() END,
         updated_at  = now()
   WHERE id = v_id;
END $$;

CREATE FUNCTION core.emit_adapter_killed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, core AS $$
BEGIN
  -- actor is deliberately not in the payload: it lives on the row and in the audit trail
  INSERT INTO core.outbox_event (event_id, event_type, marketplace, payload)
  VALUES (gen_random_uuid(), 'adapter.killed', NEW.marketplace,
          jsonb_build_object('scope', CASE WHEN NEW.is_global THEN 'GLOBAL' ELSE NEW.marketplace::text END,
                             'reason_code', NEW.reason_code));
  RETURN NEW;
END $$;

CREATE TRIGGER kill_switch_emit_event
  AFTER UPDATE OF killed ON core.kill_switch
  FOR EACH ROW WHEN (OLD.killed IS DISTINCT FROM NEW.killed AND NEW.killed)
  EXECUTE FUNCTION core.emit_adapter_killed();

-- Send gate for the kill switch. Separate from the 0001 permission gate. Trigger name sorts before
-- action_proposal_send_gate, so it fires first. FOR SHARE makes a concurrent kill wait for in-flight
-- sends, and makes a send that starts after a kill commits see the kill.
CREATE FUNCTION core.enforce_not_killed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, core AS $$
DECLARE
  v_killed boolean;
BEGIN
  IF NEW.status <> 'SENDING' THEN
    RETURN NEW;
  END IF;

  SELECT bool_or(k.killed) INTO v_killed
    FROM (SELECT s.killed FROM core.kill_switch s
           WHERE s.is_global OR s.marketplace IN (NEW.marketplace, NEW.channel)
           ORDER BY s.id
           FOR SHARE) k;

  IF COALESCE(v_killed, false) THEN
    RAISE EXCEPTION 'send blocked: kill switch active' USING ERRCODE = 'CP003';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER action_proposal_kill_gate
  BEFORE INSERT OR UPDATE OF status ON core.action_proposal
  FOR EACH ROW EXECUTE FUNCTION core.enforce_not_killed();

-- ---------- cost ledger ----------------------------------------------
CREATE TABLE core.cost_config (
  singleton                 boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  monthly_ceiling_micro_usd bigint   NOT NULL CHECK (monthly_ceiling_micro_usd > 0),
  alert_pct                 smallint NOT NULL DEFAULT 80 CHECK (alert_pct BETWEEN 1 AND 100),
  updated_at                timestamptz NOT NULL DEFAULT now()
);
INSERT INTO core.cost_config (monthly_ceiling_micro_usd, alert_pct) VALUES (300000000, 80);  -- $300, 80%

CREATE TABLE core.cost_ledger (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  call_id        uuid NOT NULL UNIQUE,                        -- one row per call, retries cannot double count
  ts             timestamptz NOT NULL DEFAULT now(),
  category       text NOT NULL CHECK (category IN ('LLM','INFRA')),
  role           core.agent_role,
  model          text CHECK (model IS NULL OR model ~ '^[A-Za-z0-9._:/-]{1,128}$'),
  input_tokens   integer CHECK (input_tokens >= 0),
  output_tokens  integer CHECK (output_tokens >= 0),
  item_code      text CHECK (item_code IS NULL OR item_code ~ '^[A-Z0-9_]{1,64}$'),
  cost_micro_usd bigint NOT NULL CHECK (cost_micro_usd >= 0),
  lead_id        uuid REFERENCES core.lead(id),
  trace_id       uuid,
  CHECK (
       (category = 'LLM'   AND role IS NOT NULL AND model IS NOT NULL AND input_tokens IS NOT NULL
                           AND output_tokens IS NOT NULL AND item_code IS NULL)
    OR (category = 'INFRA' AND item_code IS NOT NULL AND role IS NULL AND model IS NULL
                           AND input_tokens IS NULL AND output_tokens IS NULL)
  )
);
CREATE INDEX cost_ledger_ts_idx ON core.cost_ledger (ts);

CREATE FUNCTION core.cost_ledger_block() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'core.cost_ledger is append-only (% blocked); correct with a compensating row', TG_OP
    USING ERRCODE = 'CL001';
END $$;

CREATE TRIGGER cost_ledger_no_update_delete
  BEFORE UPDATE OR DELETE ON core.cost_ledger
  FOR EACH ROW EXECUTE FUNCTION core.cost_ledger_block();
CREATE TRIGGER cost_ledger_no_truncate
  BEFORE TRUNCATE ON core.cost_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION core.cost_ledger_block();

CREATE TABLE core.cost_alert (
  period_start date NOT NULL,
  pct          smallint NOT NULL CHECK (pct BETWEEN 1 AND 100),
  fired_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (period_start, pct)
);

-- Spend for one UTC calendar month against the ceiling.
CREATE FUNCTION core.cost_status(p_period date DEFAULT (date_trunc('month', now() AT TIME ZONE 'UTC'))::date)
RETURNS TABLE (period_start date, spent_micro_usd bigint, ceiling_micro_usd bigint, pct_used numeric)
LANGUAGE sql STABLE AS $$
  SELECT p_period, s.spent, c.monthly_ceiling_micro_usd,
         round(100.0 * s.spent / c.monthly_ceiling_micro_usd, 2)
    FROM core.cost_config c,
         LATERAL (SELECT COALESCE(sum(l.cost_micro_usd), 0)::bigint AS spent
                    FROM core.cost_ledger l
                   WHERE l.ts >= (p_period::timestamp AT TIME ZONE 'UTC')
                     AND l.ts <  ((p_period + interval '1 month')::timestamp AT TIME ZONE 'UTC')) s;
$$;

-- True exactly once per (month, threshold); callers send the alert only when this returns true.
CREATE FUNCTION core.try_fire_cost_alert(p_period date, p_pct smallint) RETURNS boolean
LANGUAGE sql AS $$
  WITH i AS (
    INSERT INTO core.cost_alert (period_start, pct) VALUES (p_period, p_pct)
    ON CONFLICT DO NOTHING
    RETURNING 1)
  SELECT EXISTS (SELECT 1 FROM i);
$$;

-- ---------- grants (least privilege; checklist F3) -------------------
REVOKE EXECUTE ON FUNCTION core.claim_outbox_events(text, integer, interval, integer)   FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.outbox_ack(bigint, text)                                FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.outbox_fail(bigint, text, text, integer, interval)      FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.outbox_reap_dead(integer)                               FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.is_killed(core.marketplace)                             FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.set_kill_switch(text, boolean, text, text)              FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.cost_status(date)                                       FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.try_fire_cost_alert(date, smallint)                     FROM PUBLIC;

GRANT EXECUTE ON FUNCTION core.claim_outbox_events(text, integer, interval, integer)    TO app_rw;
GRANT EXECUTE ON FUNCTION core.outbox_ack(bigint, text)                                 TO app_rw;
GRANT EXECUTE ON FUNCTION core.outbox_fail(bigint, text, text, integer, interval)       TO app_rw;
GRANT EXECUTE ON FUNCTION core.outbox_reap_dead(integer)                                TO app_rw;
GRANT EXECUTE ON FUNCTION core.is_killed(core.marketplace)                              TO app_rw;
GRANT EXECUTE ON FUNCTION core.set_kill_switch(text, boolean, text, text)               TO app_rw;
GRANT EXECUTE ON FUNCTION core.cost_status(date)                                        TO app_rw;
GRANT EXECUTE ON FUNCTION core.try_fire_cost_alert(date, smallint)                      TO app_rw;

GRANT SELECT, INSERT ON core.outbox_event TO app_rw;
GRANT UPDATE (status, claimed_by, claimed_until, attempts, available_at, last_error_code)
  ON core.outbox_event TO app_rw;                              -- content columns stay immutable
GRANT SELECT, INSERT ON core.consumer_receipt TO app_rw;
GRANT SELECT ON core.kill_switch TO app_rw;                    -- changes go through set_kill_switch only
GRANT SELECT, INSERT ON core.cost_ledger TO app_rw;
REVOKE UPDATE, DELETE, TRUNCATE ON core.cost_ledger FROM PUBLIC, app_rw;
GRANT SELECT ON core.cost_config TO app_rw;                    -- ceiling changes are reviewed migrations
GRANT SELECT, INSERT ON core.cost_alert TO app_rw;
