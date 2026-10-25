# ADR-01: v1.1 catalog

Status: accepted
Source: S0-02

## Context
v1.1 needs a small catalog that still exercises the offer rules.

## Decision
- Three SKUs between $19 and $79: one ebook, one template pack, and one bundle, so that `allowed_bundles` logic is exercised.
- The full catalog is onboarded only after Phase 1 attribution is proven.

## Consequences
- Signal concentrates on three products instead of spreading thin.
- Product, price and bundle data are canonicalized in `core.product` before outreach starts.

## Revisit when
Attribution is proven on the first paid orders.
