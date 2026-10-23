-- =====================================================================
-- 0004_audit_choke_point.sql | Alembic revision 0004 | S1-07 | DRAFT v0, UNTESTED
-- Transaction control: NONE (Alembic owns it).
-- Makes the audit writer the ONLY way into audit.event and makes audit rows PII-free by construction:
--   1. CHECK constraints on audit.event: opaque actor, code-shaped action/outcome, fixed verdict set, tier range,
--      32-byte hashes. They bind every inserter, the table owner included.
--   2. audit.hash_for(): salted hashing INSIDE the database. Salts (pii.lead_salt) never leave it. It refuses
--      erased and unknown leads, so no salt can be created after erasure.
--   3. audit.record_event(): the only insert path. INSERT is revoked from app_rw and audit_writer.
-- SQLSTATEs introduced: AU002 hash_for refused (erased or unknown lead, or bad label).
-- Lock order in hash_for: core.lead row (FOR SHARE), then pii.lead_salt. erase_lead takes the lead row FOR UPDATE
-- first and then deletes the salt, so the two serialize and cannot deadlock.
-- =====================================================================

ALTER TABLE audit.event
  ADD CONSTRAINT audit_event_actor_shape    CHECK (actor ~ '^[A-Za-z0-9_:.-]{1,128}$'),
  ADD CONSTRAINT audit_event_action_shape   CHECK (action_type ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  ADD CONSTRAINT audit_event_outcome_shape  CHECK (outcome IS NULL OR outcome ~ '^[A-Z0-9_]{1,64}$'),
  ADD CONSTRAINT audit_event_verdict_values CHECK (compliance_verdict IS NULL
                                                    OR compliance_verdict IN ('PASS','BLOCK','ESCALATE')),
  ADD CONSTRAINT audit_event_tier_range     CHECK (autonomy_tier IS NULL OR autonomy_tier BETWEEN 0 AND 2),
  ADD CONSTRAINT audit_event_hash_length    CHECK ((payload_hash IS NULL OR octet_length(payload_hash) = 32)
                                                    AND (external_response_hash IS NULL
                                                         OR octet_length(external_response_hash) = 32));

-- sha256(salt || label || 0x00 || data), salt created on first use from caller-supplied random bytes
-- (the Python writer generates them with secrets.token_bytes(32): checklist A1).
CREATE FUNCTION audit.hash_for(p_lead_id uuid, p_label text, p_data bytea, p_new_salt bytea) RETURNS bytea
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, core, pii AS $$
DECLARE
  v_salt bytea;
BEGIN
  IF p_label NOT IN ('payload', 'response') THEN
    RAISE EXCEPTION 'hash_for: unknown label' USING ERRCODE = 'AU002';
  END IF;

  PERFORM 1 FROM core.lead l WHERE l.id = p_lead_id AND l.status <> 'ERASED' FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'hash_for: lead is erased or unknown' USING ERRCODE = 'AU002';
  END IF;

  INSERT INTO pii.lead_salt (lead_id, salt) VALUES (p_lead_id, p_new_salt) ON CONFLICT (lead_id) DO NOTHING;
  SELECT salt INTO v_salt FROM pii.lead_salt WHERE lead_id = p_lead_id;

  RETURN sha256(v_salt || convert_to(p_label, 'UTF8') || '\x00'::bytea || p_data);
END $$;

CREATE FUNCTION audit.record_event(
  p_event_id uuid, p_actor text, p_marketplace core.marketplace, p_lead_id uuid, p_product_id uuid,
  p_action_type text, p_payload_hash bytea, p_compliance_verdict text, p_autonomy_tier smallint,
  p_outcome text, p_external_response_hash bytea, p_trace_id uuid) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, audit AS $$
  INSERT INTO audit.event (event_id, actor, marketplace, lead_id, product_id, action_type, payload_hash,
                           compliance_verdict, autonomy_tier, outcome, external_response_hash, trace_id)
  VALUES (p_event_id, p_actor, p_marketplace, p_lead_id, p_product_id, p_action_type, p_payload_hash,
          p_compliance_verdict, p_autonomy_tier, p_outcome, p_external_response_hash, p_trace_id)
  ON CONFLICT (event_id) DO NOTHING
  RETURNING seq;
$$;

REVOKE EXECUTE ON FUNCTION audit.hash_for(uuid, text, bytea, bytea) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION audit.record_event(uuid, text, core.marketplace, uuid, uuid, text, bytea, text,
                                              smallint, text, bytea, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit.hash_for(uuid, text, bytea, bytea) TO app_rw, audit_writer;
GRANT EXECUTE ON FUNCTION audit.record_event(uuid, text, core.marketplace, uuid, uuid, text, bytea, text,
                                             smallint, text, bytea, uuid) TO app_rw, audit_writer;
REVOKE INSERT ON audit.event FROM app_rw, audit_writer;      -- the functions above are the only way in
