# ADR-03: Reviewer capacity and Scout throttle

Status: accepted
Source: S0-02

## Context
One human reviewer (founder/ops) gives roughly 30 to 60 minutes a day, which sustains about 20 to 40 proposals a day.

## Decision
- Throughput target: 10 to 30 proposals a day. The Scout is rate-limited to match, and intake pauses when the review queue is deep.

## Consequences
- A backed-up queue means the agent is generating work, not revenue, so backpressure is a design constraint.

## Revisit when
A second reviewer is added or review is partly automated.
