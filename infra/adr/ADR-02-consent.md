# ADR-02: Consent and the quarantine bucket

Status: accepted
Source: S0-02, 0001 schema

## Context
Email is the only outbound channel in v1.1 (ADR-08), and CAN-SPAM, PECR and GDPR all attach to it.

## Decision
- No outreach to any address that lacks a timestamped opt-in record. The starting list is small (under 500) and double opt-in; purchased lists are never used.
- Contacts of unclear provenance are quarantined (`pii.contact.quarantined` defaults to true) and excluded until re-consent is captured.
- Enforced at the adapter boundary and again by the database send gate (ADR-11).

## Consequences
- The Email adapter needs a `preflightCheck` stricter than the marketplace adapters.
- A contact is never reachable by accident: consent must be recorded, current and unwithdrawn.

## Revisit when
Never for the purchased-list rule. The list size assumption is revisited when the list grows.
