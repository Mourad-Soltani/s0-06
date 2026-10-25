# ADR-10: Backups and copies of PII outside the database

Status: accepted
Source: S0-06

## Context
Backups are not the only copy of personal data outside Postgres.

## Decision
- Backups are kept 30 days. After any restore, `core.erasure_ledger` is replayed through `erase_lead` before service resumes (test T15 proves the mechanism).
- Every external copy is inventoried below. A row marked pending inventory blocks production cutover.

| Copy | In erasure scope | Status |
|---|---|---|
| Postgres backups | Yes, via the 30-day cap and ledger replay | Decided; T15 gates it |
| Redis queues and budgets | Not applicable | ADR-13: there is no Redis in v1.1 |
| OTel traces and logs | Yes | ADR-16: spans carry ids and codes only; exporter and retention pending inventory |
| Retool review UI | Yes | Reviewer sessions show PII; query-log retention pending inventory |
| LLM provider logs | Must be in | Provider and retention terms pending inventory |
| Email service provider | Must be in | Deletion API path pending inventory |
| Analytics warehouse and BI | Not applicable | None in v1.1; adding one needs its own ADR |
| Gumroad, Lemon Squeezy and Payhip buyer records | Out | The platforms hold these as their own data |

## Consequences
- Synthetic test data can run before the inventory is complete. Real customer data cannot.
- The pending rows are on infra/cutover-checklist.md.

## Revisit when
A new system holds personal data, or a provider changes its retention.
