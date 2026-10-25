# ADR-13: Postgres-only outbox event bus

Status: accepted
Source: S1-06, S1-09

## Context
Spec 5.1 requires events to be persisted before consumption, with idempotent consumers keyed by `event_id`. v1.1 has one seller account, one worker and 10 to 30 proposals a day.

## Decision
- Events live in `core.outbox_event` and are published in the same transaction as the state change that produced them (`shared.bus.publisher.publish` never commits).
- Workers claim rows with `core.claim_outbox_events` (`FOR UPDATE SKIP LOCKED`, lease, attempt count), acknowledge with `outbox_ack`, fail through `outbox_fail` with backoff and then `DEAD`, and crashed claims are recovered when the lease expires and dead-lettered by `outbox_reap_dead`.
- Each consumer's handler runs in one transaction with its `core.consumer_receipt` row, so a redelivered event has exactly one effect per consumer.
- Outbox rows carry ids and codes only; the database blocks email-shaped payload values, non-objects and payloads over 8 KB. Failures store the exception class code, never its message. No Redis in v1.1.

## Consequences
- One fewer service and failure domain. Delivery is at-least-once, with exactly-once effects per consumer.
- Rate budgets are in-process and single-worker, guarded by a Postgres advisory lock per lane.
- `DONE` and `DEAD` rows are never deleted in v1.1; retention needs a cleanup role later.

## Revisit when
A second worker is needed, polling throughput becomes a problem, or Shopify's cost-based limits arrive.
