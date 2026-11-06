# BL-06: Tie a sale back to the lead that caused it

Status: backlog
Raised by: Dev Team, while building S4-01 and S4-02
Depends on: S4-01 (the inbox keeps the attributed lead id), the legal review (C9)

## Problem
The Closer puts the lead's id in the checkout link. Lemon Squeezy hands it back on the order webhook, and the inbox stores it, but nothing reads it,
so the system cannot say which lead converted, and the Analyst has nothing to learn from. Payhip hands nothing back.

## Proposal
- When the Scout ingests a signal that carries an attributed lead, mark that lead converted (a new status or event, with an audit record) and publish
  an event the Analyst can use.
- Never create a second lead for the same sale: the sale's own lead is the one that already exists.
- For Payhip, decide with the legal review whether matching a buyer's address to a contact is acceptable. Until then Payhip sales stay unattributed.
- Replace the raw lead id with a keyed token (C9) before any of this relies on it.

## Acceptance
Unit and Postgres tests for attribution, the no-duplicate rule, an unknown or erased lead, and that nothing personal is stored. ADR-25 updated.

## Priority
Needed before conversion data is used for decisions. Not a cutover blocker.
