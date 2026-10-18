# Sales Agent Platform

Multi-marketplace sales agent for digital products: an orchestrator, marketplace adapters, and a shared execution layer.

## Status

- S0-06 (schema v0, tests, CI evidence gate, mutation check): **CLOSED**
- Current phase: **Sprint 1, infrastructure foundation**

## Layout

- `infra/schema/` Postgres schema v0, Alembic migrations, schema tests
- `infra/ci/` CI evidence gate, mutation check, and their self-tests
- `infra/adr/` architecture decision records (added in S1-12)
- `.github/workflows/ci.yml` CI. The required status check is `pytest`

## Rules

- `main` is protected: pull requests only, required check `pytest`, enforced for admins.
- No real credentials in this repo. The `pw` values in `ci.yml` belong to a throwaway CI service container.

License: MIT
