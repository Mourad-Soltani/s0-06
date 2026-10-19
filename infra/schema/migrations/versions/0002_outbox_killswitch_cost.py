"""outbox, kill switch, cost ledger (S1-09)

Revision ID: 0002
Revises: 0001
"""
from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0002_outbox_killswitch_cost.sql"

# Kept in Python, not in a .sql file: tests/conftest.py applies every NNNN_*.sql in order, and a down
# script matching that pattern would be applied as part of the schema.
DOWNGRADE_SQL = """
DROP TRIGGER IF EXISTS action_proposal_kill_gate ON core.action_proposal;
DROP FUNCTION IF EXISTS core.enforce_not_killed();
DROP FUNCTION IF EXISTS core.claim_outbox_events(text, integer, interval, integer);
DROP FUNCTION IF EXISTS core.outbox_ack(bigint, text);
DROP FUNCTION IF EXISTS core.outbox_fail(bigint, text, text, integer, interval);
DROP FUNCTION IF EXISTS core.outbox_reap_dead(integer);
DROP FUNCTION IF EXISTS core.is_killed(core.marketplace);
DROP FUNCTION IF EXISTS core.set_kill_switch(text, boolean, text, text);
DROP FUNCTION IF EXISTS core.cost_status(date);
DROP FUNCTION IF EXISTS core.try_fire_cost_alert(date, smallint);
DROP TABLE IF EXISTS core.cost_alert, core.cost_ledger, core.cost_config,
                     core.kill_switch, core.consumer_receipt, core.outbox_event CASCADE;
DROP FUNCTION IF EXISTS core.emit_adapter_killed();
DROP FUNCTION IF EXISTS core.outbox_guard();
DROP FUNCTION IF EXISTS core.cost_ledger_block();
"""


def upgrade():
    # Raw DBAPI cursor, no params (same reason as 0001): the SQL contains '%' and several statements.
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)
