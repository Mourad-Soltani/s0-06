"""kill-aware outbox claim (S1-08)

Revision ID: 0003
Revises: 0002
"""
from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0003_kill_aware_claim.sql"

# Kept in Python, not a .sql file (conftest applies every NNNN_*.sql). Restores the 0002 function exactly.
DOWNGRADE_SQL = r"""
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
        AND ((e.status = 'PENDING' AND e.available_at <= now())
          OR (e.status = 'CLAIMED' AND e.claimed_until < now()))
      ORDER BY e.id
      FOR UPDATE SKIP LOCKED
      LIMIT p_limit)
  RETURNING o.*;
$$;

DROP FUNCTION IF EXISTS core.outbox_paused(core.marketplace, text);
"""


def upgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)
