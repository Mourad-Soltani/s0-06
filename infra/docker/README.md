# Running the system (cutover kit 1)

Postgres, the event-bus worker, the review API and the Streamlit review UI, in containers on one machine. Read ADR-27 first: it says what this does
and, more importantly, what it does not yet do.

## 1. Prepare (once)
```
cp .env.example .env                       # then fill it in; .env is git-ignored and never enters the image
openssl rand -hex 24                       # run three times: POSTGRES_PASSWORD, SAP_WORKER_PASSWORD, SAP_REVIEW_PASSWORD
cp secrets/review_tokens.example.json secrets/review_tokens.json     # then replace the token with `openssl rand -hex 32` and name the reviewer
```
Fill in the Postmark token and sender, your postal address and unsubscribe URL (both appear in every email), the marketplaces you sell on,
`ALLOWED_LINK_HOSTS` (every host a checkout link may use) and `ICP_PRODUCT_IDS`. For Payhip put its catalogue under `config/` (a JSON list of
`{"key", "name", "price_minor"}`) and set `PAYHIP_CATALOG_FILE=/config/payhip.json`.

## 2. Start
```
docker compose up -d --build
docker compose logs -f migrate worker      # migrate prints "provisioned roles: sap_worker, sap_review"; the worker prints "worker ... started"
docker compose run --rm worker python -m orchestrator.worker_main check
```
`check` prints `database`, `email_provider` and one line per marketplace, each OK or FAIL. Do not go further until all are OK. It sends nothing.
The review UI is at http://127.0.0.1:8501 on this machine; sign in with a token from `secrets/review_tokens.json`. Put a TLS proxy in front before anyone
else uses it (cutover C14).

## 3. Use
```
docker compose run --rm -v "$PWD/leads.csv:/tmp/leads.csv:ro" worker python -m orchestrator.lead_intake_cli /tmp/leads.csv --actor you --dry-run
docker compose run --rm worker python -m orchestrator.worker_main reconcile      # run this every 5 minutes from cron or a systemd timer
```
Drop `--dry-run` to import for real. A draft appears in the review UI with the marketplace its checkout link points to; approving it queues the send.

## 4. First live send (do this before any real contact)
Use a seed contact you own. Confirm: the email arrives with the footer; the checkout link is unchanged (no click tracking); the unsubscribe link works;
the message exists in Postmark's activity feed with the metadata key `idem`. Only then consider `POSTMARK_LOOKUP=true`.

## Operations
- Stop: `docker compose stop` (the worker finishes the event in hand). Logs carry error classes and ids, never addresses or message text.
- Rotate a password: change it in `.env`, then `docker compose up -d migrate worker review-api` (migrate re-keys the roles).
- Back up: the `pgdata` volume holds everything. No backup or restore drill exists yet (C8).
- Upgrade: `git pull && docker compose up -d --build`; migrations run first and the worker waits for them.
