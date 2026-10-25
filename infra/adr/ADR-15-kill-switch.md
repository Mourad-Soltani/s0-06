# ADR-15: Kill switch semantics

Status: proposed
Source: S1-08

## Context
Spec 6.3 requires a per-marketplace and global kill that pauses outbound queues, revokes credentials and emits `adapter.killed`.

## Decision
- The switch flips first and commits, so sends are refused at once (the DB gate, CP003). `adapter.killed` is emitted atomically by a trigger.
- Queues pause at claim time: events for a killed marketplace are simply not claimed, so no attempt is consumed and nothing is lost. `adapter.*` events and events with no marketplace are never paused, so the kill notice still flows. Reviving makes the events claimable again.
- Credential revocation follows, best-effort and reported by class code; a failed revocation never undoes the kill, and re-running kill retries it. Reviving does not restore credentials.
- A repeat kill is a no-op. There is no unkill event (approved); who cleared the switch is recorded through the audit writer.
- The lock and the claim predicate are each proven by a CI mutation check.

## Consequences
- A Python API and a CLI exist. An HTTP endpoint needs a framework decision and is not built.
- Until the secrets manager exists, revocation fails loudly (`NotConfiguredRevoker`) so nobody assumes credentials were pulled.

## Revisit when
An HTTP API is required, or queue pausing needs a different grain.
