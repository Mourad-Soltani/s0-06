# ADR-24: Settling sends that were claimed but never recorded

Status: proposed
Source: BL-04

## Context
The Closer commits its claim (SENDING) before it sends, so a message is never sent twice (ADR-23). The price is that a crash between
the send and the recording leaves a proposal in SENDING with the email possibly sent, and nothing noticed. Real sends must not start
until that is closed.

## Decision
- **Ask the provider, never guess.** A stuck proposal is settled by asking the provider whether it accepted a message with the proposal's
  idempotency key. The email adapter gains `find_send(key)` for this; it is not part of the shared adapter interface, so the contract
  suite is unchanged. The transport declares `supports_lookup` only when the provider can answer authoritatively.
- **Three outcomes.** The provider accepted it: the proposal becomes SENT with its provider reference, an audit event and `action.sent`,
  and nothing is sent. The provider says it never did: the proposal goes back to APPROVED and the Closer's sweep sends it once, with the
  same key. The provider cannot answer (lookup unsupported, rate limited, authentication expired, network, anything else): it STAYS in
  SENDING. "Could not find out" is never read as "not sent".
- **An unresolved proposal is audited, an alert hook is called, and its clock is reset**, so it is looked at again after another threshold
  and not on every run. A broken alert channel does not stop the other proposals being settled.
- **The job never sends.** It touches a proposal only if it is still SENDING and still stale at the moment of the update, so a Closer that
  finishes in the meantime wins. The threshold has a floor of one minute and must exceed the slowest legitimate send.
- **It reads no message body and no address**, so the application role is enough.
- **The provider reference** (a hash of the provider's message id, 32 hex characters, because provider ids can contain an address) is stored
  on the proposal by both the Closer and the job, so a send can be matched to the provider's records later.

## Consequences
- With a provider that cannot look up by key, every stuck send stays unresolved and alerts. That is the safe failure, and it makes the
  choice of provider (cutover C3) depend on lookup support or on an equivalent way to ask.
- A resend after a "never happened" answer relies on the provider honouring the idempotency key if its answer was wrong in the short window
  before the message became visible. The threshold is the protection.
- The job needs a scheduler. Nothing here runs it.

## Revisit when
A provider is chosen, the first stuck send happens in production, or the threshold needs tuning to the provider's real latency.
