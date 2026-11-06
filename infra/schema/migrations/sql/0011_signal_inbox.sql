-- 0011_signal_inbox.sql | Alembic revision 0011 | S4-01, S4-02 | DRAFT v0, UNTESTED. No transaction control.
-- Signals that arrive by webhook (Lemon Squeezy, Payhip), kept until the Scout reads them: the adapters' fetch_signals reads this table, so the
-- Scout's pull model does not change. Ids, a kind and a time only: no buyer address, no name. attributed_lead_id is the lead id that the checkout
-- link carried, when the platform hands it back; it is an opaque id, and there is deliberately no foreign key (a lead can be erased).
CREATE TABLE core.signal_inbox (
  marketplace         core.marketplace NOT NULL,
  signal_ref          text NOT NULL CHECK (signal_ref ~ '^[A-Za-z0-9_:.-]{1,128}$'),
  kind                text NOT NULL CHECK (kind IN ('SALE', 'REVIEW', 'QA', 'CART', 'SUBSCRIBER', 'OFFER_CODE_USE')),
  product_external_id text CHECK (product_external_id IS NULL OR product_external_id ~ '^[A-Za-z0-9_:.=-]{1,128}$'),
  occurred_at         timestamptz NOT NULL,
  received_at         timestamptz NOT NULL DEFAULT now(),
  attributed_lead_id  uuid,
  PRIMARY KEY (marketplace, signal_ref)
);
CREATE INDEX signal_inbox_read_idx ON core.signal_inbox (marketplace, occurred_at);
GRANT SELECT, INSERT ON core.signal_inbox TO app_rw;                    -- append-only
