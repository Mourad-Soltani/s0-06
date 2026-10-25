# ADR-11: Contact-permission enforcement in the database

Status: accepted
Source: S0-06, S1-09

## Context
A rule that only the application checks is only as strong as every code path that reaches it.

## Decision
- A trigger on `core.action_proposal` fires on the transition into `SENDING`, the last point before the external side effect. It requires a compliance verdict of PASS and a matching, unexpired, unwithdrawn permission for a non-quarantined contact.
- `SENDING` is reachable only from `APPROVED`. Inserting a row directly as `SENDING` is rejected.
- It locks the contact row and then the permission row, in two statements, with `FOR SHARE`, so a concurrent withdrawal waits for an in-flight send and a send that starts after a withdrawal commits sees it. Do not collapse the two statements into one join: a join does not guarantee lock order.
- Send lock order is kill_switch, then contact, then contact_permission (ADR-15). `erase_lead` and `set_kill_switch` each take a prefix of that order, so none can deadlock with a send.
- The lock dependency is proven by a CI mutation check, not by inspection: tests T10a and T10b must fail against a schema with the lock removed, and K7a and K7b must fail against a mutant kill gate.

## Consequences
- The trigger fires on any `UPDATE OF status`. A data layer that writes `status` on rows that are not changing state, such as a whole-row ORM save, trips `CP001`.
- SQLSTATEs: CP001 (not allowed to send), CP002 (no valid permission), CP003 (kill switch), CP004 (invalid kill-switch input).

## Revisit when
The number of `FOR SHARE` statements changes. The mutation tripwire fails by design until the step and the tests are updated together.
