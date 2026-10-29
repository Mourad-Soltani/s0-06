-- 0007_review_edit_and_role.sql | Alembic revision 0007 | S2-05 | DRAFT v0, UNTESTED. No transaction control.
-- 1. core.review_edit: one row per edit of a proposal, so the shadow-mode gate (spec 9.1) can measure the edit rate.
-- 2. review_svc: the role the review API connects as. It is app_rw (core tables, audit functions) plus pii_svc
--    (message bodies), so an edit changes the proposal, the body and the audit trail in ONE transaction.
CREATE TABLE core.review_edit (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  proposal_id uuid NOT NULL REFERENCES core.action_proposal(id),
  reviewer    text NOT NULL CHECK (reviewer ~ '^[A-Za-z0-9_:.-]{1,128}$'),
  edited_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX review_edit_proposal_idx ON core.review_edit (proposal_id);
GRANT SELECT, INSERT ON core.review_edit TO app_rw;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'review_svc') THEN CREATE ROLE review_svc NOLOGIN; END IF;
END $$;
GRANT app_rw TO review_svc;
GRANT pii_svc TO review_svc;
