-- =====================================================================
-- 0003_kill_aware_claim.sql | Alembic revision 0003 | S1-08 | DRAFT v0, UNTESTED
-- Transaction control: NONE (Alembic owns it). Touches nothing from 0001; replaces ONE function from 0002.
-- Queues for a killed marketplace pause at CLAIM time: their events are simply not claimed, so no attempt is
-- consumed and nothing is lost. Reviving makes them claimable again. adapter.* events and events with no
-- marketplace are never paused, so the kill notification itself still flows.
-- =====================================================================

CREATE FUNCTION core.outbox_paused(p_marketplace core.marketplace, p_event_type text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT p_marketplace IS NOT NULL
     AND p_event_type NOT LIKE 'adapter.%'
     AND core.is_killed(p_marketplace);
$$;

CREATE OR REPLACE FUNCTION core.claim_outbox_events(
  p_worker text, p_limit integer DEFAULT 10, p_lease interval DEFAULT interval '60 seconds',
  p_max_attempts integer DEFAULT 5)
RETURNS SETOF core.outbox_event
LANGUAGE sql AS $$
  UPDATE core.outbox_event o
     SET status = 'CLAIMED', claimed_by = p_worker, claimed_until = now() + p_lease, attempts = o.attempts + 1
   WHERE o.id IN (
     SELECT e.id FROM core.outbox_event e
      WHERE e.attempts < p_max_attempts
        AND NOT core.outbox_paused(e.marketplace, e.event_type)
        AND ((e.status = 'PENDING' AND e.available_at <= now())
          OR (e.status = 'CLAIMED' AND e.claimed_until < now()))
      ORDER BY e.id
      FOR UPDATE SKIP LOCKED
      LIMIT p_limit)
  RETURNING o.*;
$$;

REVOKE EXECUTE ON FUNCTION core.outbox_paused(core.marketplace, text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.outbox_paused(core.marketplace, text) TO app_rw;
