-- 0009_lead_product.sql | Alembic revision 0009 | S3-05 | DRAFT v0, UNTESTED. No transaction control.
-- The product a lead is about (the product a contact opted in for or bought), so the drafting step can build a brief
-- later without the event that first carried it. An opaque id only.
ALTER TABLE core.lead ADD COLUMN product_external_id text
  CHECK (product_external_id IS NULL OR product_external_id ~ '^[A-Za-z0-9_:.=-]{1,128}$');
