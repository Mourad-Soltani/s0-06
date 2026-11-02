# ADR-23: The pipeline on one bus, and the Closer

Status: proposed
Source: S3-03, BL-03

## Context
The parts existed separately: the Scout and intake, the Qualifier, the Copywriter, the Guard, the review queue. Nothing connected a
qualified lead to a draft, and nothing sent an approved one. The send is the one irreversible step, so it needs the most care.

## Decision
- **One chain.** `signal.captured` goes to the Qualifier, `lead.qualified` to the drafter, `action.proposed` to the Guard, and
  `action.approved` to the Closer (`orchestrator/wiring.py`). The review service publishes `action.approved` in the same transaction
  as the approval, only when it is given a publisher, so existing callers are unchanged. One trace id, the lead's id, runs through
  every event.
- **The drafter** skips a lead that cannot be drafted and records why (no contact, no product, no current consent, product not found,
  no checkout link, invalid product). It reads product details from the marketplace adapter, never from an event. Trouble at the
  marketplace is raised so the bus retries; it is not turned into a skip.
- **The Closer claims, then sends, then records.** It reads the approved proposal under a lock, checks the final text with the Guard
  again, and moves it to SENDING in a transaction that COMMITS before anything is sent. The database send gate fires on that move
  (consent, the Guard's verdict, the kill switch). Then it sends outside any transaction, with the proposal's idempotency key, and
  records the result in a second transaction.
- **Its own connection.** The Closer never uses the bus worker's transaction. A rollback there must not be able to undo a claim after
  an email has left, which would let a retry send twice.
- **At most once.** A crash after the claim leaves the proposal in SENDING and it is never sent again. A send that certainly did not
  happen (rate limited, authentication expired) goes back to APPROVED for the next sweep. A send whose outcome is unknown is FAILED and
  never retried blindly. A kill switch leaves the proposal APPROVED, and the sweep sends it after the release.
- **Shadow mode.** A tier 0 draft is never sent, however it was approved. The Closer records that it was held.
- **The link.** Every occurrence of the product's plain checkout link in the approved text becomes the same link with the lead's id
  as a query parameter (`ref`). If the draft has no link, one fixed line carrying it is appended. Nothing else in the approved text
  changes, and a test proves it. The final text is re-checked before it is claimed.
- **Nothing personal in the trail.** The audit events and `action.sent` carry ids and codes only. The provider's message id is hashed,
  because provider ids can contain an address.

## Consequences
- The lead's id is an opaque random UUID with no personal data, but it links a click to our record of the person, and the marketplace
  sees it. Legal review (cutover C9) should confirm it is acceptable; a keyed token instead of the raw id is a small change in the Closer.
- The drafter calls the model inside the bus worker's transaction. At the current volume that is fine; a slow provider holds that
  transaction open, and a failure after the spend makes the retry pay again. The budget ceiling stops a runaway.
- Proposals stuck in SENDING and the provider's message id (not stored yet) need a reconciliation job: backlog item BL-04.
- Only one product adapter and the email channel are wired. A second marketplace needs a product-to-marketplace mapping.

## Revisit when
An email provider exists, the first real send happens, or a second channel is added.
