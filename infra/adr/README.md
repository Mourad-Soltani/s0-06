# Architecture decision records

One decision per file. `accepted` means ruled by the product owner or Head Chief and in force; `proposed` means implemented in an open or just-merged ticket and awaiting confirmation. Superseding an ADR means a new ADR that says so, never an edit that erases the old reasoning.

| ADR | Decision | Status | Source |
|---|---|---|---|
| [ADR-01](ADR-01-catalog.md) | v1.1 catalog | accepted | S0-02 |
| [ADR-02](ADR-02-consent.md) | Consent and the quarantine bucket | accepted | S0-02, 0001 schema |
| [ADR-03](ADR-03-capacity.md) | Reviewer capacity and Scout throttle | accepted | S0-02 |
| [ADR-04](ADR-04-budget.md) | Cost ceiling and model routing | accepted | S0-02, S1-10 |
| [ADR-05](ADR-05-jurisdiction.md) | Jurisdiction and lawful basis | accepted | S0-02 |
| [ADR-06](ADR-06-thresholds.md) | Qualification thresholds | accepted | S0-02 |
| [ADR-07](ADR-07-discount-authority.md) | Discount authority | accepted | S0-02 |
| [ADR-08](ADR-08-conversion-definition.md) | What "conversion" means in v1.1 | accepted | S0-02 |
| [ADR-09](ADR-09-erasure-path.md) | Erasure: hard-delete PII, never rewrite audit | accepted | S0-06, S1-07 |
| [ADR-10](ADR-10-backups-and-external-copies.md) | Backups and copies of PII outside the database | accepted | S0-06 |
| [ADR-11](ADR-11-contact-permission-enforcement.md) | Contact-permission enforcement in the database | accepted | S0-06, S1-09 |
| [ADR-12](ADR-12-discount-units.md) | Discount units | accepted | S1-09 |
| [ADR-13](ADR-13-postgres-outbox.md) | Postgres-only outbox event bus | accepted | S1-06, S1-09 |
| [ADR-14](ADR-14-audit-choke-point.md) | Audit writer as the single choke point | proposed | S1-07 |
| [ADR-15](ADR-15-kill-switch.md) | Kill switch semantics | proposed | S1-08 |
| [ADR-16](ADR-16-tracing.md) | Tracing: one trace per lead, PII-free | proposed | S1-11 |

The production-cutover blockers are on [../cutover-checklist.md](../cutover-checklist.md).
