# ADR-14: Audit writer as the single choke point

Status: proposed
Source: S1-07

## Context
Audit rows must be trustworthy and free of personal data, whichever code path writes them.

## Decision
- `audit.record_event()` is the only way to insert into `audit.event`; INSERT is revoked from the application roles. CHECK constraints bind every inserter, the owner included: opaque actor, code-shaped action and outcome, a fixed verdict set, a tier range, 32-byte hashes.
- Payloads and external responses are accepted only to be hashed, as sha256(salt, label, 0x00, data), inside the database with a per-lead salt that never leaves it. The Python writer generates candidate salts with `secrets.token_bytes(32)`.
- `audit.hash_for` refuses erased and unknown leads, so no salt can be created after erasure. An event with no lead cannot carry a payload.

## Consequences
- `audit_writer` has schema USAGE on `core` (needed to name a type) and no table rights there.
- Erasure unlinks every hash a lead produced by deleting its salt.

## Revisit when
The hash scheme changes. Existing hashes would need a migration plan.
