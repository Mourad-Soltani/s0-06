-- 0010_provider_ref.sql | Alembic revision 0010 | BL-04 | DRAFT v0, UNTESTED. No transaction control.
-- The provider's message id for a sent proposal, hashed to 32 hex characters (provider ids can contain an address), so a send can be
-- matched to the provider's records later. And an index so the reconciliation job finds stuck sends without scanning the table.
ALTER TABLE core.action_proposal ADD COLUMN provider_ref text CHECK (provider_ref IS NULL OR provider_ref ~ '^[0-9a-f]{32}$');
CREATE INDEX action_proposal_sending_idx ON core.action_proposal (updated_at, id) WHERE status = 'SENDING';
