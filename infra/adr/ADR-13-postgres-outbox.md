# ADR-13: Postgres-only outbox event bus

Status: proposed (draft for S1-12, implemented by S1-06 and migration 0002)

## Context
Spec 5.1 requires events to be persisted before consumption, with idempotent consumers keyed by `event_id`.
v1.1 has one seller account, one worker and an expected 10 to 30 proposals per day.

## Decision
- Events live in `core.outbox_event` and are published in the same transaction as the state change that
  produced them (`shared.bus.publisher.publish`, which never commits).
- Workers claim rows with `core.claim_outbox_events` (`FOR UPDATE SKIP LOCKED`, lease, attempt count) and
  acknowledge with `outbox_ack`. Failures go through `outbox_fail` with backoff, then `DEAD`. Crashed claims
  are recovered when their lease expires and dead-lettered by `outbox_reap_dead`.
- Each consumer's handler runs in one transaction together with its `core.consumer_receipt` row, so a
  redelivered event has exactly one effect per consumer, and a failure leaves no receipt.
- Outbox rows carry ids and codes only. The database blocks email-shaped payload values, non-objects and
  payloads over 8 KB. Error codes stored on failure come from the exception class name, never its message.
- No Redis in v1.1.

## Consequences
- One fewer service and failure domain. The ADR-10 "Redis queues" row is not applicable.
- Rate budgets are in-process and single-worker (S1-05 enforces this with an advisory lock).
- Delivery is at-least-once, with per-consumer effects exactly-once through receipts.
- `DONE` and `DEAD` rows are never deleted in v1.1. Retention needs a cleanup role later.
- Send lock order is kill_switch, then contact, then contact_permission (add to ADR-11).

## Revisit when
A second worker is needed, polling throughput becomes a problem, or Shopify's cost-based limits arrive.
