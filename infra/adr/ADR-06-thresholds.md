# ADR-06: Qualification thresholds

Status: accepted
Source: S0-02

## Context
There is no labelled data yet to calibrate lead scoring.

## Decision
- A lead qualifies at composite score 60 or more with confidence 0.75 or more, is disqualified below 40, and goes to review in between or when confidence is low. These are placeholders.
- Recalibrate after 100 labelled leads.

## Consequences
- Early scoring will be wrong in both directions; the review queue absorbs it.

## Revisit when
100 labelled leads exist.
