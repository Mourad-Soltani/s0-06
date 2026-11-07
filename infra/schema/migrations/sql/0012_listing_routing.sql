-- 0012_listing_routing.sql | Alembic revision 0012 | S4-03 | DRAFT v0, UNTESTED. No transaction control.
-- What the router needs and the schema did not have. (1) What each marketplace keeps from a sale, so "most profitable" is computed from the
-- seller's own numbers and never from figures written into code: a percentage in basis points (1% = 100) plus a fixed amount in minor units of
-- `currency`. There are deliberately NO seed rows: a marketplace without a row is "fee unknown", and the router says so rather than guessing.
-- The application role can only read it; fees change by a reviewed migration or an administrator, like core.cost_config. (2) Two indexes for the
-- router's lookups: a lead names its product by the marketplace's external id alone (the unique index leads with the marketplace, so it cannot
-- serve that), and the active listings of one product.
CREATE TABLE core.marketplace_fee (
  marketplace core.marketplace PRIMARY KEY,
  percent_bps integer NOT NULL CHECK (percent_bps BETWEEN 0 AND 10000),
  fixed_minor bigint  NOT NULL DEFAULT 0 CHECK (fixed_minor >= 0),
  currency    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON core.marketplace_fee TO app_rw;
CREATE INDEX listing_ref_external_idx ON core.marketplace_listing_ref (external_id);
CREATE INDEX listing_ref_active_product_idx ON core.marketplace_listing_ref (product_id) WHERE status = 'ACTIVE';
