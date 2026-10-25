# ADR-08: What "conversion" means in v1.1

Status: accepted
Source: S0-02

## Context
Gumroad, Lemon Squeezy and Payhip expose no buyer-messaging API.

## Decision
- Conversion in v1.1 is inbound marketplace conversion. The loop is: discover from inbound signals, reach out by email to consented contacts only, convert back on the marketplace through a checkout link or offer code.
- `lead_id` is carried in the checkout URL and the offer code so the Analyst can attribute either path.

## Consequences
- Nobody should expect outbound on-platform messaging in v1.1.
- Email to checkout attribution is a weaker link than in-platform messaging.

## Revisit when
An adapter with real outbound messaging is added.
