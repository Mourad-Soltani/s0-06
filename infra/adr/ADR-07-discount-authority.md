# ADR-07: Discount authority

Status: accepted
Source: S0-02

## Context
The Closer can create discount codes, but its role is template selection only.

## Decision
- The cap `max_discount` lives on the product, set by the seller and never inferred by an agent. The Compliance Guard rejects any proposal above it.
- A per-action override goes through the review queue and is recorded as an audit event with the actor id.

## Consequences
- Anything above the cap is Tier 1 at most. See ADR-12 for the units.

## Revisit when
Never without a spec amendment.
