"""audit choke point (S1-07)

Revision ID: 0004
Revises: 0003
"""
from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0004_audit_choke_point.sql"

# Kept in Python, not a .sql file (conftest applies every NNNN_*.sql).
DOWNGRADE_SQL = """
REVOKE USAGE ON SCHEMA core FROM audit_writer;
GRANT INSERT ON audit.event TO app_rw, audit_writer;
DROP FUNCTION IF EXISTS audit.record_event(uuid, text, core.marketplace, uuid, uuid, text, bytea, text,
                                           smallint, text, bytea, uuid);
DROP FUNCTION IF EXISTS audit.hash_for(uuid, text, bytea, bytea);
ALTER TABLE audit.event
  DROP CONSTRAINT IF EXISTS audit_event_actor_shape,
  DROP CONSTRAINT IF EXISTS audit_event_action_shape,
  DROP CONSTRAINT IF EXISTS audit_event_outcome_shape,
  DROP CONSTRAINT IF EXISTS audit_event_verdict_values,
  DROP CONSTRAINT IF EXISTS audit_event_tier_range,
  DROP CONSTRAINT IF EXISTS audit_event_hash_length;
"""


def upgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)
