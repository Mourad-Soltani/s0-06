-- 0006_review_decision.sql | Alembic revision 0006 | S2-04 | DRAFT v0, UNTESTED. No transaction control (Alembic owns it).
-- One human decision per action proposal. The reasons feed Copywriter and Qualifier calibration (spec 5.3) and the
-- shadow-mode gate (spec 9.1). Opaque reviewer ids and UPPER_SNAKE reason codes only: no free text, so no PII.
CREATE TABLE core.review_decision (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  proposal_id uuid NOT NULL UNIQUE REFERENCES core.action_proposal(id),
  decision    text NOT NULL CHECK (decision IN ('APPROVE', 'REJECT')),
  reason_code text CHECK (reason_code IS NULL OR reason_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  reviewer    text NOT NULL CHECK (reviewer ~ '^[A-Za-z0-9_:.-]{1,128}$'),
  decided_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (decision <> 'REJECT' OR reason_code IS NOT NULL)
);
GRANT SELECT, INSERT ON core.review_decision TO app_rw;     -- decisions are final: no UPDATE, no DELETE
