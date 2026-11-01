# ADR-21: Consented lead intake

Status: proposed
Source: S3-05

## Context
Marketplace signals carry no personal data by design (ADR-02, ADR-08), so a lead from a sale has no address and nothing can be
sent to it. The only way a contact enters the system is a creator importing people who opted in.

## Decision
- **A CSV import is the way in.** Columns are an allowlist (email, name, consented_at, source_url, product_id, kind). An
  unknown column fails the whole file, so a stray phone number cannot be ingested by accident.
- **Consent evidence is a date and an https source URL** (on the row or as the import's default). A row without both is kept
  quarantined and unreachable, with no lead (ADR-02). A consent older than the expiry window is quarantined too. A consent
  dated in the future, or any date or URL that is present but malformed, is rejected.
- **An opt-out outranks an old list.** A contact who withdrew at or after the row's consent date is skipped. Only a consent dated
  after the withdrawal restores them, and the report says so (`CONSENT_REFRESHED`).
- **Idempotent.** Identical consent is reused, there is one lead per contact and product, and re-running a file changes
  nothing. Duplicate rows in a file are skipped.
- **Each row commits on its own**: the consent, the lead and its `signal.captured` event together. A failure midway leaves
  earlier rows committed and the failed row untouched, and a re-run finishes the job.
- **The lead enters the normal pipeline** as SUBSCRIBER or SALE with a contact on record, so the Qualifier scores it without
  the CONSENT_RISK flag. It carries the product it is about (new column `core.lead.product_external_id`, an opaque id).
- **The report names line numbers and outcome codes, never an address or a name.** A dry run classifies without writing.
- **Role.** The import runs as `review_svc`: it needs the personal-data schema and the core tables together.

## Consequences
- Nothing drafts a message when a lead qualifies yet: that consumer is backlog item BL-03.
- A consent date the creator types is the creator's claim. The source URL and date are stored, and the creator is the audit
  actor, but the system cannot verify the opt-in happened. Legal review (cutover C9) should decide what evidence to require.
- The 365-day expiry is the same placeholder as in ADR-18.
- Files are limited to 1 MB and 5,000 rows.

## Revisit when
Legal review sets the evidence requirements, a second import channel (a form or an ESP webhook) is added, or the first real
import shows which columns creators actually have.
