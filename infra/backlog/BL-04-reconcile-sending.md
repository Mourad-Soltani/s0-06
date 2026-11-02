# BL-04: Reconcile proposals stuck in SENDING

Status: done in Sprint 4 (see ADR-24)
Raised by: Dev Team, while building S3-03
Depends on: S3-03 (the Closer), an email provider (cutover C3)

## Problem
The Closer commits its claim (SENDING) before it sends, so a message is never sent twice. The price is that a crash between the send and
the recording leaves a proposal in SENDING with the email possibly sent. Nothing finds it. The provider's message id is also not stored.

## Proposal
- A periodic job lists proposals in SENDING for longer than a threshold and asks the provider, by the proposal's idempotency key, whether
  the message left. Sent becomes SENT (with its event and audit), not sent becomes FAILED or goes back to APPROVED, unknown stays and alerts.
- A small migration stores the provider's message id (opaque, hashed or kept in the personal-data schema) on the proposal.
- An alert (cutover D2) when anything stays in SENDING past the threshold.

## Acceptance
Unit and Postgres tests for each outcome, the threshold, idempotency of the job itself, and that it never sends anything. ADR-23 updated.

## Priority
Required before the first real send. Not needed while only the fake transport exists.
