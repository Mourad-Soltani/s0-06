# ADR-09: Erasure: hard-delete PII, never rewrite audit

Status: accepted
Source: S0-06, S1-07

## Context
Spec 6.2 requires erasure to cascade to PII while preserving audit events.

## Decision
- Audit rows never contain PII, so erasure never rewrites them. Message bodies live in `pii.payload_store`; hashes use a per-lead salt (ADR-14).
- `core.erase_lead()` deletes payloads, the salt and the contact (cascading permissions), nulls `lead.contact_id`, sets the lead to `ERASED`, writes `core.erasure_ledger` and one `LEAD_ERASED` audit event. It is idempotent, and it erases every lead that shares the contact.
- `lead_id` remains in `checkout_url` values after erasure (spec 5.5 attribution). That is pseudonymous residue with no link back to a person once the PII rows are gone, accepted by design and not a defect.

## Consequences
- The erasure actor must be an opaque id: an email-shaped actor fails the audit CHECK and nothing is erased.
- Only `pii_svc` may execute `erase_lead`.

## Revisit when
The audit design or the hash scheme changes.
