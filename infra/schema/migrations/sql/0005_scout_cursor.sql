-- 0005_scout_cursor.sql | Alembic revision 0005 | S2-03 | DRAFT v0, UNTESTED. No transaction control (Alembic owns it).
-- Where the Scout stopped reading each marketplace account, so a restart resumes instead of re-reading history.
CREATE TABLE core.scout_cursor (
  marketplace core.marketplace NOT NULL,
  account_id  text NOT NULL CHECK (account_id ~ '^[A-Za-z0-9_:.-]{1,128}$'),
  since       timestamptz NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (marketplace, account_id)
);
GRANT SELECT, INSERT, UPDATE ON core.scout_cursor TO app_rw;
