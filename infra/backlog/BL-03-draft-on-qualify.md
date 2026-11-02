# BL-03: Draft when a lead qualifies

Status: done in S3-03 (see ADR-23)
Raised by: Dev Team, while building S3-05
Depends on: S3-01 (Copywriter, proposal writer), S3-05 (leads with a contact and a product)

## Problem
Leads now arrive with a contact, a consent and a product, and the Qualifier scores them, but nothing reacts to
`lead.qualified`. No draft is ever written, so the loop stops at the qualified lead.

## Proposal
A bus consumer of `lead.qualified`:
- load the lead, its contact's current permission and its product id; skip with a recorded reason if any is missing;
- read the product from the marketplace adapter (title, price, checkout URL), never from the event;
- build the brief and call the proposal writer (one proposal per lead, product and context, so a replay is harmless);
- configure the Guard's allowed link hosts from the same product data, so a legitimate checkout link is never blocked.

## Acceptance
Unit and Postgres tests for the happy path, each skip reason, idempotent replay, and the full chain import, score, draft,
guard, review. ADR-20 and ADR-21 updated.

## Priority
Needed before the pipeline shows a draft to a reviewer. Not a cutover blocker, but nothing works end to end without it.
