# Production cutover checklist

**Production cutover** means onboarding the first real buyer or sending to the first real email address. Until every
row marked "blocks production cutover" is closed, the platform runs on synthetic data only (ADR-10). Status words:
Open (known to be undone), Unconfirmed (not evidenced in the project record, confirm before cutover), Done.

| ID | Item | Source | Status | Blocks production cutover |
|---|---|---|---|---|
| C1 | Retool review UI: query-log retention inventoried and cleared or mitigated | ADR-10 | Open | Yes, blocks production cutover |
| C2 | LLM provider chosen; retention terms inventoried; prompts and responses covered by erasure | ADR-10, ADR-04 | Open | Yes, blocks production cutover |
| C3 | Email service provider chosen; deletion API path for erasure inventoried | ADR-10 | Open | Yes, blocks production cutover |
| C4 | OTel exporter or collector chosen; trace retention inventoried | ADR-16, ADR-10 | Open | Yes, blocks production cutover |
| C5 | Email DNS (SPF, DKIM, DMARC) verified and warm-up plan run | S0-08 | Unconfirmed | Yes, blocks production cutover |
| C6 | Secrets manager live; a real credential revoker replaces `NotConfiguredRevoker` | S0-04, ADR-15 | Open | Yes, blocks production cutover |
| C7 | Cost alert channel decided (D2); model names and prices configured | ADR-04 | Open | Yes, blocks production cutover |
| C8 | Backups configured at 30 days; a restore plus erasure-ledger replay drill passed in the real environment | ADR-10 | Unconfirmed | Yes, blocks production cutover |
| C9 | Legal review of outbound messaging against marketplace terms (Etsy, Fiverr, Amazon) | spec section 12 | Unconfirmed | Yes, blocks production cutover |
| C10 | Lemon Squeezy and Payhip accounts provisioned; vendor spike report (S0-03) verified for the adapters in use | S0-03, S0-05 | Unconfirmed | Yes, blocks production cutover |
| C11 | Kill switch: decide whether an HTTP API is required for v1.1 | ADR-15 | Open | Decision needed |
| C12 | LICENSE copyright holder confirmed | S1-00 | Open | No |
| C13 | Thresholds recalibrated after 100 labelled leads | ADR-06 | Open | No, after cutover |
