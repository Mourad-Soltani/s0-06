# ADR-19: Compliance Guard

Status: proposed
Source: S3-02

## Context
The marketplace adapter is the policy enforcement point (ADR-02), but a draft can be wrong in ways no single adapter
knows: an unresolved placeholder, a link to the wrong host, a discount above the seller's cap, a deceptive subject.
The Guard is the last check before a human sees a draft and before anything can be sent.

## Decision
- **Deterministic rules, no LLM.** A draft is judged by pure functions over structured facts (`orchestrator/guard_rules.py`).
  The same draft always gets the same findings.
- **Two severities.** BLOCK means the draft must not go out as written. ESCALATE means a human must decide. The verdict is
  BLOCK if anything blocks, else ESCALATE if anything escalates, else PASS.
- **Fail closed.** No configured link hosts means no link is allowed. No configured discount cap means any discount
  escalates. No adapter for the marketplace is a BLOCK (`NO_ADAPTER`). A proposal with no readable body is a BLOCK.
- **Rules** (codes in the module): missing or malformed message; no current permission for outreach (checked live in the
  database: not withdrawn, not expired, contact not quarantined); discount invalid, above cap, or not what the text
  promises; unresolved placeholders; fake Re/Fwd subjects outside a real reply; personal data in a subject; links that
  are not https, use a shortener, carry credentials, point at an IP, or leave the allowlist; prohibited claims; pressure
  language, shouting, spam phrases and long subjects (escalations); low Copywriter confidence and a TOS risk flag.
- **The adapter's own preflight runs too** and its reasons become findings, so the Guard cannot pass what the adapter
  would refuse.
- **One transaction** records the verdict, the findings (append-only `core.compliance_finding`, one run id per
  evaluation), the audit event and, on PASS, the `compliance.passed` event. A failed audit leaves no verdict.
- **An edit clears the verdict (ADR-18)**, so every changed draft is judged again. Earlier findings are kept.
- **The Guard does not assign the autonomy tier.** The Copywriter does, from shadow-mode statistics (ADR-18).

## Consequences
- The pattern lists are a starting point. Tune them against real reviewer rejections, and keep every change tested.
- ADR-07 lets a seller override a discount cap. Until that path exists (backlog BL-01) a cap breach cannot be approved.
- The Guard reads message bodies, so it runs as `review_svc`. It never writes message text anywhere.

## Revisit when
The first real rejections are in, a second outbound channel is added, or an LLM-based content check is proposed.
