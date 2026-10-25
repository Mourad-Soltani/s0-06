# ADR-05: Jurisdiction and lawful basis

Status: accepted
Source: S0-02

## Context
Buyers and list members are in the US, EU and UK.

## Decision
- GDPR is in scope. Right-to-erasure is implemented per spec 6.2 (ADR-09).
- The lawful basis for outreach is consent, recorded in `ContactPermission`. No legitimate-interest outreach in v1.1.

## Consequences
- `pii.contact_permission.basis` is constrained to `consent`; widening it is a reviewed migration.

## Revisit when
Legal review recommends a different basis for a specific channel.
