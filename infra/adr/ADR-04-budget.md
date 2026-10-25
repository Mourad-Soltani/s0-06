# ADR-04: Cost ceiling and model routing

Status: accepted
Source: S0-02, S1-10

## Context
The product owner set a ceiling for LLM plus infrastructure spend.

## Decision
- $300 a month in total. Small, cheap models for Scout, Qualifier, Closer and Compliance Guard; a stronger model only for Copywriter, batched; no LLM for the Analyst.
- An alert at 80% and a second at 100%. Exceeding the ceiling is a bug, not a scaling event.
- Implemented by the cost ledger and tracker (S1-10); design in docs/S0-09-llm-routing-and-cost-design.md.

## Consequences
- Money is integer micro-USD, estimates round up, and an unpriced model is an error.
- Open: the provider, model names and prices (configuration), and the alert channel (D2). See the cutover checklist.

## Revisit when
Spend approaches the ceiling for two months running, or the provider changes.
