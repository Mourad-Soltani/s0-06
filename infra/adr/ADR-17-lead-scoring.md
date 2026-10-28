# ADR-17: Lead scoring, Scout and Qualifier

Status: proposed
Source: S2-03

## Context
Spec 3.1 to 3.4 define the Scout and Qualifier roles and the lead state machine, but give no weights, and there is no
labelled data yet to calibrate any (ADR-06).

## Decision
- The composite score is an exact integer weighted mean, rounded half up. The weights are placeholders: ICP match 35,
  pain 20, authority 10, timeline 10, budget 25 (percent). Authority and timeline weigh little because the products
  are low-ticket digital goods. Recalibrate with the thresholds after 100 labelled leads.
- The decision follows ADR-06 with an explicit precedence: below 40 is disqualified whatever the confidence; at 60 or more
  with confidence 0.75 or more is qualified; everything else goes to a human.
- The Qualifier is a bus consumer of `signal.captured`. It scores and sets the final status in one guarded update
  (`WHERE status = 'SIGNAL_CAPTURED'`), so a redelivery is a no-op, then publishes `lead.scored` and, for qualified
  leads, `lead.qualified`. The intermediate SCORED state of the spec's diagram is not persisted separately.
- The Scout reads adapters through the MarketplaceAdapter interface and the rate governor. In v1.1 every signal is a
  structured marketplace fact, so it uses no LLM. It keeps a cursor per marketplace account, pauses while the review
  queue is at 30 or more, and stops at 30 new leads a day (ADR-03). Pauses leave the cursor behind, so nothing is skipped.
- A lead's id is the trace id for its events (ADR-16). Signal payloads carry ids and codes only.
- The baseline scorer is rule-based. It cannot disqualify (its lowest possible composite is 41). An LLM scorer would
  implement the same protocol, routed per ADR-04; none exists until a provider is configured.
- Leads from marketplace signals carry the risk flag CONSENT_RISK, because no contact or permission is on record, so
  they are never Tier 2. QUALIFIED never means contactable: that needs a ContactPermission (ADR-11).

## Consequences
- Early scores will be wrong in both directions; the review queue absorbs it.
- The cursor is inclusive, so the last signal is read again each run; its unique `signal_id` makes that harmless.
- Marketplace buyer details are never read, so a lead from a sale has no contact until a consented list supplies one.

## Revisit when
100 labelled leads exist, or an LLM scorer is introduced.
