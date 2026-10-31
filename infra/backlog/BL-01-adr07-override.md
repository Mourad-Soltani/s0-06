# BL-01: Discount-cap override through the review queue (ADR-07)

Status: backlog
Raised by: Dev Team, while building S2-05; confirmed by the Project Owner
Depends on: S2-05 (review service), S3-02 (compliance findings)

## Problem
ADR-07 says the Compliance Guard rejects any proposal above the seller's discount cap, and that the seller may override
per action through the review queue, logged with the actor id. The review service approves only a proposal whose
verdict is PASS (ADR-18), so there is no way to approve an over-cap proposal. The override cannot happen today.

## Proposal
- A new service call and endpoint: `approve_override(proposal_id, reviewer, reason_code)`, `POST /review/actions/{id}/override`.
- It applies ONLY when the single blocking finding is `DISCOUNT_ABOVE_CAP`. Every other blocking finding
  (no permission, prohibited claim, bad link, adapter refusal, placeholders, deceptive subject) is NEVER overridable.
- The decision is recorded as an approval with the override reason, and a distinct audit event `REVIEW_OVERRIDE` carries
  the reviewer, the cap and the granted percentage as ids and numbers only.
- Overrides need a separate reviewer permission, not the default review token.
- The verdict stays BLOCK in `core.action_proposal`. The send gate gains an explicit, audited exception for this one case.

## Acceptance
- Unit and Postgres tests: an over-cap proposal can be overridden and approved; any proposal with another blocking finding
  cannot; every override has its audit event; an override without the permission is refused; the shadow-mode statistics
  count an override as an approval and as a block.
- ADR-18 and ADR-19 updated.

## Priority
Needed before a seller can ever exceed their own cap. Not a cutover blocker while the cap is never exceeded.
