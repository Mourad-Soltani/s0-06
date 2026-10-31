-- 0008_compliance_finding.sql | Alembic revision 0008 | S3-02 | DRAFT v0, UNTESTED. No transaction control.
-- Why the Compliance Guard gave a verdict. Append-only: every evaluation adds rows with its own run_id, so a
-- re-evaluation after an edit keeps the earlier findings. A PASS run has no rows (the audit trail records it).
-- Rule codes and severities only: never message text, so no PII.
CREATE TABLE core.compliance_finding (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  proposal_id  uuid NOT NULL REFERENCES core.action_proposal(id),
  run_id       uuid NOT NULL,
  rule_code    text NOT NULL CHECK (rule_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  severity     text NOT NULL CHECK (severity IN ('BLOCK', 'ESCALATE')),
  evaluated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX compliance_finding_proposal_idx ON core.compliance_finding (proposal_id, evaluated_at DESC);
GRANT SELECT, INSERT ON core.compliance_finding TO app_rw;       -- no UPDATE, no DELETE
