# ADR-18: Consent capture and the review queue

Status: proposed
Source: S2-04, S2-05

## Context
Nothing may be sent without a current ContactPermission (ADR-02, ADR-11), and nothing is sent without a human decision
in the shadow-mode period (spec 9.1). Something has to create permissions and let a reviewer decide.

## Decision
- **Consent capture** (`shared/consent.py`) records an explicit consent: an https source URL, a capture time and an expiry.
  It releases the contact's quarantine, since ADR-02 frees a contact only on fresh consent. Re-consent adds a row and
  never edits one. Withdrawal stamps `withdrawn_at` and never deletes. Unverified imports stay quarantined with no
  permission. The 365-day expiry is a placeholder pending legal review (cutover C9).
- **Review decisions** are one per proposal, final, and stored in `core.review_decision` with an opaque reviewer id and,
  for a rejection, an UPPER_SNAKE reason code. No free text, so no PII. Edits are counted in `core.review_edit`.
- **Approve only a PASS.** A BLOCK, an ESCALATE or a proposal with no verdict can be rejected or edited, never approved.
  Approving a Tier 0 draft counts toward the shadow gate but can never be sent.
- **An edit clears the verdict.** It replaces the body and its salted hash and sets the compliance verdict to NULL,
  because the Guard judged the old text. The proposal cannot be approved until it is re-evaluated.
- **Decision and audit event commit together** in one transaction. If the audit fails, the decision does not happen.
- **The review API** (`orchestrator/review_api.py`) is a plain WSGI app, standard library only: no framework decision.
  A static bearer token per reviewer, compared in constant time, decides the reviewer id; with no tokens it refuses to
  start; errors are fixed codes. It connects as `review_svc`, which is `app_rw` plus `pii_svc`, so an edit changes the
  proposal, the body and the audit trail atomically.
- **Shadow statistics** (`ShadowStats`) implement the spec 9.1 gate in exact integer arithmetic: at least 100 actions and
  14 days, approval at least 80% of decided proposals, block rate strictly under 5%.

## Consequences
- Static tokens, TLS and identity are deployment concerns (cutover checklist): bind to localhost behind a TLS proxy.
- ADR-07 says a seller may override a discount cap through the review queue. With approve-only-PASS there is no override
  path yet; it needs its own ticket (approve with a recorded override reason).
- Reviewer bodies are personal data: `GET /review/actions/{id}` returns them, so the API must sit behind authentication.

## Revisit when
SSO replaces static tokens, the override path is built, or the first production reviewer is onboarded.
