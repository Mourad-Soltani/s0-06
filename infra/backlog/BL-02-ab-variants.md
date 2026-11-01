# BL-02: A/B message variants

Status: backlog (deferred by the Head Chief, 2026-11-01)
Depends on: S3-01 (Copywriter), S2-05 (review statistics)

## Problem
The spec has the Copywriter produce several variants for A/B testing. The first release uses only the first variant, so
every proposal costs one reviewer decision and one model call.

## Proposal
- Ask the model for 2 or 3 variants (the schema already accepts up to 3) and store one proposal per variant, or one proposal
  with a variant list, whichever the reviewer finds quicker to decide.
- Record which variant a reviewer approved, so approval and edit rates can be compared by variant.
- Cap the extra review load: variants count against the reviewer's daily capacity (ADR-03).

## Acceptance
Unit and Postgres tests for variant storage, per-variant statistics and the capacity cap; ADR-20 updated.

## Priority
After the first real reviewer data exists. Not a cutover blocker.
