-- =====================================================================
-- 0001_schema_v0.sql  |  Alembic revision 0001 (applied via op.execute)
-- Sales Agent Platform v1.1  |  Sprint 0 / S0-06 + S0-07  |  DRAFT v0.2, UNTESTED
-- Downgrade: DROP SCHEMA audit, core, pii CASCADE (dev only, never prod)
-- Transaction control: NONE in this file. Alembic owns the transaction.
-- Do not add BEGIN/COMMIT here (ADR-11 addendum / PO Alembic note).
--
-- Design rules baked in:
--   1. PII lives ONLY in schema pii. core and audit hold no PII.
--   2. Audit rows never contain PII, so erasure never rewrites audit.
--      Hashes are salted per lead; deleting the salt unlinks the hash.
--   3. Contact permission is enforced by a DB trigger on the SENDING
--      transition (last point before the external side effect).
--   4. audit.event is append-only: grants revoked AND triggers block
--      UPDATE / DELETE / TRUNCATE, even for the table owner.
-- =====================================================================

-- ---------- roles (infra creates real ones; NOLOGIN stubs for dev) ----
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw')       THEN CREATE ROLE app_rw NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audit_writer') THEN CREATE ROLE audit_writer NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pii_svc')      THEN CREATE ROLE pii_svc NOLOGIN; END IF;
END $$;

CREATE SCHEMA core;
CREATE SCHEMA pii;
CREATE SCHEMA audit;

REVOKE ALL ON SCHEMA core, pii, audit FROM PUBLIC;

-- ---------- enums -----------------------------------------------------
-- NOTE: EMAIL added vs spec 3.2 (needed for A4). Flagged for spec amendment.
CREATE TYPE core.marketplace AS ENUM
  ('GUMROAD','ETSY','SHOPIFY','AMAZON','FIVERR','PAYHIP','LEMON_SQUEEZY','ACP','UCP','EMAIL');

CREATE TYPE core.listing_status AS ENUM ('DRAFT','ACTIVE','PAUSED','ARCHIVED');

CREATE TYPE core.lead_status AS ENUM (
  'SIGNAL_CAPTURED','SCORED','QUALIFIED','DISQUALIFIED','NEEDS_REVIEW',
  'OUTREACH_DRAFTED','APPROVAL_PENDING','SENT','REJECTED','REPLIED',
  'NO_RESPONSE','NEEDS_HUMAN','CLOSED_WON','CLOSED_LOST','ERASED');

CREATE TYPE core.offer_type  AS ENUM ('CHECKOUT_LINK','COUPON_CODE','BUNDLE');
CREATE TYPE core.discount_type AS ENUM ('PERCENT','FIXED');
CREATE TYPE core.agent_role  AS ENUM ('SCOUT','QUALIFIER','COPYWRITER','CLOSER','COMPLIANCE_GUARD','ANALYST');
CREATE TYPE core.action_type AS ENUM ('SEND_MESSAGE','CREATE_OFFER','UPDATE_LISTING','REPLY');
CREATE TYPE core.verdict     AS ENUM ('PASS','BLOCK','ESCALATE');

-- NOTE: SENDING and REJECTED added vs spec 3.2 enum. Flagged for spec amendment.
CREATE TYPE core.proposal_status AS ENUM
  ('PROPOSED','APPROVED','SENDING','SENT','FAILED','ROLLED_BACK','REJECTED');

-- =====================================================================
-- PII schema (part 1: contact + permission)
-- =====================================================================
CREATE TABLE pii.contact (
  contact_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email           text,
  name            text,
  platform_handle text,
  quarantined     boolean NOT NULL DEFAULT true,   -- safe default; consent-capture tx clears it
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX contact_email_uq ON pii.contact (lower(email)) WHERE email IS NOT NULL;

CREATE TABLE pii.contact_permission (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id    uuid NOT NULL REFERENCES pii.contact(contact_id) ON DELETE CASCADE,
  channel       core.marketplace NOT NULL,
  basis         text NOT NULL CHECK (basis = 'consent'),  -- ADR-05: no legitimate-interest in v1.1
  captured_at   timestamptz NOT NULL,
  expires_at    timestamptz NOT NULL,
  source_url    text NOT NULL,
  withdrawn_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (expires_at > captured_at)
);
CREATE INDEX contact_permission_lookup ON pii.contact_permission (contact_id, channel);

-- =====================================================================
-- core schema
-- =====================================================================
CREATE TABLE core.product (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_title      text NOT NULL,
  canonical_description text NOT NULL,
  formats              text[] NOT NULL DEFAULT '{}',
  base_price_minor     bigint NOT NULL CHECK (base_price_minor >= 0),  -- minor units
  currency             char(3) NOT NULL,
  min_margin_pct       numeric(5,2) NOT NULL DEFAULT 0 CHECK (min_margin_pct BETWEEN 0 AND 100),
  max_discount_pct     numeric(5,2) NOT NULL DEFAULT 0 CHECK (max_discount_pct BETWEEN 0 AND 100), -- seller-set (A2)
  allowed_bundles      uuid[] NOT NULL DEFAULT '{}',
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.marketplace_listing_ref (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id     uuid NOT NULL REFERENCES core.product(id),
  marketplace    core.marketplace NOT NULL,
  external_id    text NOT NULL,
  status         core.listing_status NOT NULL DEFAULT 'DRAFT',
  checkout_url   text,
  last_synced_at timestamptz,
  UNIQUE (marketplace, external_id)
);

CREATE TABLE core.lead (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type          text NOT NULL CHECK (source_type IN ('MARKETPLACE_SIGNAL','COMMUNITY','INBOUND','REFERRAL')),
  source_marketplace   core.marketplace,
  signal_ref           text NOT NULL CHECK (signal_ref ~ '^[A-Za-z0-9_:.-]{1,128}$'),
                       -- opaque external id only; survives erasure, so the alphabet blocks emails, names, free text
  signal_id            text NOT NULL UNIQUE CHECK (signal_id ~ '^[0-9a-f]{64}$'),  -- app-computed sha256(source_type|source_marketplace|signal_ref); Scout upserts on it
  contact_id           uuid REFERENCES pii.contact(contact_id) ON DELETE SET NULL,
  icp_match_score      smallint CHECK (icp_match_score BETWEEN 0 AND 100),
  pain_intensity       smallint CHECK (pain_intensity BETWEEN 0 AND 100),
  authority_signal     smallint CHECK (authority_signal BETWEEN 0 AND 100),
  timeline_signal      smallint CHECK (timeline_signal BETWEEN 0 AND 100),
  budget_signal        smallint CHECK (budget_signal BETWEEN 0 AND 100),
  composite_score      smallint CHECK (composite_score BETWEEN 0 AND 100),
  confidence           numeric(3,2) CHECK (confidence BETWEEN 0 AND 1),
  risk_flags           text[] NOT NULL DEFAULT '{}',
  status               core.lead_status NOT NULL DEFAULT 'SIGNAL_CAPTURED',
  assigned_marketplace core.marketplace,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX lead_status_idx ON core.lead (status);
CREATE INDEX lead_contact_idx ON core.lead (contact_id);

-- ---------- PII schema (part 2: tables that reference core.lead) -------
CREATE TABLE pii.lead_salt (
  lead_id uuid PRIMARY KEY REFERENCES core.lead(id),
  salt    bytea NOT NULL CHECK (octet_length(salt) = 32)  -- payload_hash = sha256(salt || payload); delete salt on erasure.
                                                          -- Generation (secrets.token_bytes(32)) is a code-review item; the CHECK only enforces length.
);

CREATE TABLE pii.payload_store (
  payload_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id    uuid NOT NULL REFERENCES core.lead(id),
  body       jsonb NOT NULL,             -- message bodies, drafts: may contain PII
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX payload_store_lead_idx ON pii.payload_store (lead_id);

CREATE TABLE core.offer (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id         uuid NOT NULL REFERENCES core.lead(id),
  product_id      uuid NOT NULL REFERENCES core.product(id),
  marketplace     core.marketplace NOT NULL,
  offer_type      core.offer_type NOT NULL,
  code            text,
  discount_type        core.discount_type,
  discount_value_minor bigint CHECK (discount_value_minor >= 0),                    -- FIXED: minor units of the product's currency
  discount_value_pct   numeric(5,2) CHECK (discount_value_pct BETWEEN 0 AND 100),   -- PERCENT: 15.00 means 15%
  expires_at      timestamptz,
  checkout_url    text NOT NULL,
  idempotency_key uuid NOT NULL UNIQUE,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (
       (discount_type IS NULL       AND discount_value_minor IS NULL     AND discount_value_pct IS NULL)
    OR (discount_type = 'PERCENT'   AND discount_value_pct IS NOT NULL   AND discount_value_minor IS NULL)
    OR (discount_type = 'FIXED'     AND discount_value_minor IS NOT NULL AND discount_value_pct IS NULL)
  )
  -- max_discount cap is enforced by the Compliance Guard (A2); overrides audited.
);

CREATE TABLE core.action_proposal (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id                   uuid NOT NULL REFERENCES core.lead(id),
  role                      core.agent_role NOT NULL,
  marketplace               core.marketplace NOT NULL,
  channel                   core.marketplace NOT NULL,       -- channel the action goes out on
  action_type               core.action_type NOT NULL,
  payload_ref               uuid REFERENCES pii.payload_store(payload_id) ON DELETE SET NULL,
  payload_hash              bytea,                           -- salted; see pii.lead_salt
  autonomy_tier             smallint NOT NULL CHECK (autonomy_tier IN (0,1,2)),
  confidence                numeric(3,2) CHECK (confidence BETWEEN 0 AND 1),
  compliance_verdict        core.verdict,
  status                    core.proposal_status NOT NULL DEFAULT 'PROPOSED',
  requires_contact_permission boolean NOT NULL DEFAULT true,
  contact_permission_id     uuid REFERENCES pii.contact_permission(id) ON DELETE SET NULL,
  idempotency_key           uuid NOT NULL UNIQUE,
  audit_trace_id            uuid NOT NULL,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  -- Tier 0 = draft only: no attempted send in any form
  CHECK (NOT (autonomy_tier = 0 AND status IN ('SENDING','SENT','FAILED','ROLLED_BACK'))),
  -- the permission gate can only be bypassed for REPLY; new values need a reviewed migration
  CHECK (requires_contact_permission = true OR action_type = 'REPLY')
);
CREATE INDEX action_proposal_status_idx ON core.action_proposal (status);

CREATE TABLE core.erasure_ledger (          -- no PII; replayed after any backup restore
  lead_id   uuid PRIMARY KEY,
  erased_at timestamptz NOT NULL DEFAULT now(),
  actor     text NOT NULL
);

-- =====================================================================
-- S0-07: contact-permission enforcement (DB layer)
-- Fires on the transition INTO 'SENDING'. The app then performs the
-- external send and moves SENDING -> SENT | FAILED. FOR SHARE blocks a
-- concurrent withdrawal until this transaction commits, which closes the
-- check-then-send race.
-- =====================================================================
CREATE FUNCTION core.enforce_send_permission() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, core, pii AS $$
DECLARE
  v_contact uuid;
BEGIN
  IF NEW.status <> 'SENDING' THEN
    RETURN NEW;
  END IF;

  IF TG_OP = 'INSERT' THEN
    RAISE EXCEPTION 'send blocked: a proposal cannot be INSERTed directly as SENDING'
      USING ERRCODE = 'CP001';
  END IF;

  IF OLD.status <> 'APPROVED' THEN
    RAISE EXCEPTION 'send blocked: SENDING reachable only from APPROVED (was %)', OLD.status
      USING ERRCODE = 'CP001';
  END IF;

  IF NEW.compliance_verdict IS DISTINCT FROM 'PASS' THEN
    RAISE EXCEPTION 'send blocked: compliance verdict is %', NEW.compliance_verdict
      USING ERRCODE = 'CP001';
  END IF;

  IF NOT NEW.requires_contact_permission THEN
    RETURN NEW;
  END IF;

  SELECT l.contact_id INTO v_contact FROM core.lead l WHERE l.id = NEW.lead_id;

  IF v_contact IS NULL OR NEW.contact_permission_id IS NULL THEN
    RAISE EXCEPTION 'send blocked: no contact permission on record'
      USING ERRCODE = 'CP002';
  END IF;

  -- Lock order: contact, then permission. erase_lead deletes in the same
  -- order (contact -> cascade permission), so the two cannot deadlock.
  PERFORM 1
    FROM pii.contact c
   WHERE c.contact_id = v_contact
     AND NOT c.quarantined
     FOR SHARE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'send blocked: contact missing or quarantined'
      USING ERRCODE = 'CP002';
  END IF;

  PERFORM 1
    FROM pii.contact_permission p
   WHERE p.id           = NEW.contact_permission_id
     AND p.contact_id   = v_contact
     AND p.channel      = NEW.channel
     AND p.withdrawn_at IS NULL
     AND p.expires_at   > now()
     FOR SHARE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'send blocked: permission missing, expired, withdrawn, or wrong channel'
      USING ERRCODE = 'CP002';
  END IF;

  RETURN NEW;
END $$;

CREATE TRIGGER action_proposal_send_gate
  BEFORE INSERT OR UPDATE OF status ON core.action_proposal
  FOR EACH ROW EXECUTE FUNCTION core.enforce_send_permission();

-- =====================================================================
-- audit schema: append-only
-- =====================================================================
CREATE TABLE audit.event (
  seq                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id               uuid NOT NULL UNIQUE,
  ts                     timestamptz NOT NULL DEFAULT now(),
  actor                  text NOT NULL,             -- 'system' | human id
  marketplace            core.marketplace,
  lead_id                uuid,                      -- deliberately no FK: audit outlives leads
  product_id             uuid,
  action_type            text NOT NULL,
  payload_hash           bytea,                     -- salted hash, never raw payload
  compliance_verdict     text,
  autonomy_tier          smallint,
  outcome                text,
  external_response_hash bytea,
  trace_id               uuid,
  CHECK (action_type NOT IN ('SEND_MESSAGE','CREATE_OFFER','REPLY') OR marketplace IS NOT NULL)
);
CREATE INDEX audit_event_lead_idx ON audit.event (lead_id);

CREATE FUNCTION audit.block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit.event is append-only (% blocked)', TG_OP USING ERRCODE = 'AU001';
END $$;

CREATE TRIGGER audit_no_update_delete
  BEFORE UPDATE OR DELETE ON audit.event
  FOR EACH ROW EXECUTE FUNCTION audit.block_mutation();

CREATE TRIGGER audit_no_truncate
  BEFORE TRUNCATE ON audit.event
  FOR EACH STATEMENT EXECUTE FUNCTION audit.block_mutation();

-- =====================================================================
-- Erasure (GDPR 6.2): hard-delete PII, keep audit untouched.
-- Erases every lead that shares the contact, then logs to the ledger.
-- =====================================================================
CREATE FUNCTION core.erase_lead(p_lead_id uuid, p_actor text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, core, pii, audit AS $$
DECLARE
  v_contact uuid;
  v_leads   uuid[];
  v_id      uuid;
  v_new     uuid;
BEGIN
  SELECT contact_id INTO v_contact FROM core.lead WHERE id = p_lead_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'lead % not found', p_lead_id;
  END IF;

  -- idempotent: already erased => no-op, no second audit event
  IF EXISTS (SELECT 1 FROM core.erasure_ledger WHERE lead_id = p_lead_id) THEN
    RETURN;
  END IF;

  IF v_contact IS NULL THEN
    v_leads := ARRAY[p_lead_id];
  ELSE
    SELECT array_agg(id) INTO v_leads FROM core.lead WHERE contact_id = v_contact;
  END IF;

  DELETE FROM pii.payload_store WHERE lead_id = ANY (v_leads);
  DELETE FROM pii.lead_salt     WHERE lead_id = ANY (v_leads);
  IF v_contact IS NOT NULL THEN
    DELETE FROM pii.contact WHERE contact_id = v_contact;   -- cascades contact_permission
  END IF;

  UPDATE core.lead
     SET contact_id = NULL, status = 'ERASED', updated_at = now()
   WHERE id = ANY (v_leads);

  FOREACH v_id IN ARRAY v_leads LOOP
    v_new := NULL;
    INSERT INTO core.erasure_ledger (lead_id, actor) VALUES (v_id, p_actor)
      ON CONFLICT DO NOTHING
      RETURNING lead_id INTO v_new;
    IF v_new IS NOT NULL THEN                       -- audit only on first erasure
      INSERT INTO audit.event (event_id, actor, lead_id, action_type, outcome)
        VALUES (gen_random_uuid(), p_actor, v_id, 'LEAD_ERASED', 'OK');
    END IF;
  END LOOP;
END $$;

-- =====================================================================
-- Grants (least privilege)
-- =====================================================================
GRANT USAGE ON SCHEMA core  TO app_rw;
GRANT USAGE ON SCHEMA audit TO audit_writer, app_rw;
GRANT USAGE ON SCHEMA pii   TO pii_svc;
GRANT USAGE ON SCHEMA core  TO pii_svc;   -- required to resolve core.erase_lead (no table grants)

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core TO app_rw;
GRANT DELETE ON core.marketplace_listing_ref TO app_rw;

GRANT SELECT, INSERT ON audit.event TO audit_writer, app_rw;
REVOKE UPDATE, DELETE, TRUNCATE ON audit.event FROM PUBLIC, app_rw, audit_writer;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA pii TO pii_svc;
-- app_rw has NO direct access to pii; it reaches PII only through pii_svc APIs.
-- Triggers/functions above are SECURITY DEFINER and read pii on its behalf.

REVOKE EXECUTE ON FUNCTION core.erase_lead(uuid, text) FROM PUBLIC;   -- default is PUBLIC; app_rw must not erase
GRANT  EXECUTE ON FUNCTION core.erase_lead(uuid, text) TO pii_svc;

