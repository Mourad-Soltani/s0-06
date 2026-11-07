# ADR-27: Cutover kit 1: real email, configuration from the environment, containers

Status: proposed
Source: Phase 5 (production cutover), cutover items C3, C6

## Context
Sprint 4 closed with a pipeline that only ran against fakes: no real email transport, no way to start the event-bus worker (only the review API,
the webhook receiver and two command-line tools had entry points), no configuration loader, and no container definition. The cutover checklist
cannot start without a system that can be run.

## Decision
- **Email provider: Postmark** (`adapters/email_channel/postmark.py`), behind the existing `EmailTransport` protocol. The adapter, the footer, the
  List-Unsubscribe header, the Closer's claim-before-send and the database send gate are unchanged. Link tracking and open tracking are off: Postmark would
  otherwise rewrite the checkout link, changing the host the Guard approved and dropping the attributed lead id. The contact id is turned into an address
  by a resolver (`orchestrator/address_resolver.py`, reads `pii.contact`) for the one call that needs it; the address is never stored, logged or put in
  an exception, and no HTTP error body is ever read. SES was not chosen because C3 needs lookup by key and Postmark can carry the key as metadata.
- **Idempotency.** Postmark has no idempotency header. A repeated HTTP call is not deduplicated by Postmark; what prevents a second email is the Closer's
  claim before the send and the reconciler after a crash. `lookup` (is there a message with this key?) uses Postmark's message search, which is
  eventually consistent: a false "never accepted" would let the reconciler release a message that went out. It is therefore OFF by default
  (`POSTMARK_LOOKUP=false`: the reconciler then leaves a stuck send in SENDING and audits it; no alert reaches anyone until a channel exists, C7) and is switched on only after the first live check.
- **Configuration** (`shared/config.py`): environment first, `NAME_FILE` for secrets (Docker secrets), `.env` for development only. All problems are
  reported at start-up by name and a fixed reason, never a value. Secrets are held in a `Secret` whose repr is `***`. Setting a name both directly and as
  `NAME_FILE` is an error.
- **One entry point** (`orchestrator/worker_main.py`): `run` (the bus worker with the whole chain, plus a periodic Closer sweep), `check` (database, email
  provider and each marketplace accept our credentials; prints names and OK/FAIL, sends nothing) and `reconcile` (one pass of the BL-04 job).
  Routing (ADR-26) is off unless `ROUTING_ENABLED=true`.
- **Containers.** One image, five services: Postgres, a one-shot `migrate` (Alembic, then `infra/docker/provision_roles.py`), the worker, the review API
  and the Streamlit UI. The migrations create only NOLOGIN group roles, so provisioning creates two login roles (`sap_worker`, `sap_review`), both members
  of `review_svc`, neither a superuser, with passwords from the environment. Each service gets only the variables it needs (no blanket `env_file`).
  The UI shares the review API's network namespace so it reaches the API at 127.0.0.1, which its own address check accepts without TLS; the only published
  port is the UI's, on the host's loopback. The image runs as an unprivileged user and `.dockerignore` keeps `.env`, `secrets/` and `config/` out of it.

## Consequences
- Not done here, and each blocks a live deployment: TLS in front of the UI (C14); the webhook receiver and the Scout loop are not in the compose file, so
  marketplace sales do not yet become leads automatically (consented CSV import still works); the reconciler needs a scheduler (BL-05); no language-model
  provider client exists (C2), so the LLM key is loaded and checked but drafts use the standard template; no cost alert channel (C7); no backups or
  restore drill (C8); no secrets manager or credential revoker (C6); no tracing exporter (C4); dependencies are version ranges, not a lock file.
- Postmark's behaviour was written from its documentation without network access: the request and response shapes, and above all the metadata search
  parameter, are checked by tests that pin what this code sends, not by Postmark. Run `worker_main check` and one real send to a seed address first.
- The role split is untested under load: `review_svc` has been exercised by the review API, but the whole pipeline had only run as a superuser in tests.
  `test_address_resolver_pg.py` checks the provisioned roles on the statements the worker is known to need; the first real run is the full test.
- Inbound replies are not read (the transport's `list_threads` is empty), consistent with v1.1 sending only.

## Revisit when
The first live send succeeds (turn on lookup if Postmark's search proves authoritative), a second email provider is needed, or the system moves off a
single host (secrets manager, managed Postgres, separate worker and receiver processes).
