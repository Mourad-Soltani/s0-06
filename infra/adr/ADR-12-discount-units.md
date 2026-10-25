# ADR-12: Discount units

Status: accepted
Source: S1-09

## Context
A single numeric `discount_value` with a type flag cannot say whether 15 means 15% or 15 dollars.

## Decision
- `core.offer` stores a FIXED discount in `discount_value_minor`, in minor units of the product currency, and a PERCENT discount in `discount_value_pct`, where 15.00 means 15%. A CHECK guarantees exactly the right column is set for the discount type.
- The Compliance Guard compares `discount_value_pct` directly to `max_discount_pct`; FIXED amounts are converted against `base_price_minor` first.

## Consequences
- The Guard's arithmetic is trivial and unit-safe.

## Revisit when
Multi-currency offers.
