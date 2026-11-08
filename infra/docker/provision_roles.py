"""Creates (or re-keys) the three login roles the containers use, after the migrations have run: python infra/docker/provision_roles.py
The migrations create the roles app_rw, pii_svc and review_svc as NOLOGIN groups (0002 to 0007). Nothing in them can log in, so something has to
create the roles the services connect as, with passwords that never appear in the repository:
  sap_worker  member of review_svc: the event-bus worker (needs message bodies, so pii access, ADR-23)
  sap_review  member of review_svc: the review API (ADR-18)
  sap_webhook member of app_rw ONLY: the webhook receiver (ADR-28). It faces the internet, so it can write the signal inbox and read nothing personal.
Neither is a superuser, can create databases or roles, or owns anything. Re-running it changes the passwords (rotation) and leaves the memberships.
Needs ADMIN_DATABASE_URL (an owner or superuser connection) and SAP_WORKER_PASSWORD, SAP_REVIEW_PASSWORD, SAP_WEBHOOK_PASSWORD (or the same names with _FILE).
Passwords must be 20+ characters of letters, digits and . _ ~ - only, because they travel inside a connection URL. Prints role names, never a password."""
from __future__ import annotations

import re
import sys
from typing import Mapping, Optional

ROLES = (("sap_worker", "SAP_WORKER_PASSWORD", "review_svc"), ("sap_review", "SAP_REVIEW_PASSWORD", "review_svc"),
         ("sap_webhook", "SAP_WEBHOOK_PASSWORD", "app_rw"))
PASSWORD = re.compile(r"^[A-Za-z0-9._~-]{20,128}$")


def provision(conn, passwords: Mapping[str, str]) -> list:
    for _, env_name, _group in ROLES:                                 # every password is checked before anything is created
        if not PASSWORD.match(passwords.get(env_name, "")):
            raise ValueError(f"{env_name}: must be 20 to 128 characters of letters, digits and . _ ~ -")
    from psycopg import sql
    done = []
    for role, env_name, group in ROLES:
        password = passwords[env_name]
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL("{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION").format(
            sql.SQL(verb), sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(role)))
        done.append(role)
    return done


def main(environ: Optional[Mapping[str, str]] = None) -> int:
    from shared.config import ConfigError, load_environ
    try:
        env = load_environ(environ)
    except ConfigError as exc:
        print("CONFIG ERROR " + "; ".join(exc.problems), file=sys.stderr)
        return 2
    url = env.get("ADMIN_DATABASE_URL", "")
    if not url.startswith(("postgresql://", "postgres://")):
        print("CONFIG ERROR ADMIN_DATABASE_URL: must be a postgresql:// URL", file=sys.stderr)
        return 2
    import psycopg
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            roles = provision(conn, env)
    except ValueError as exc:                     # a password that does not meet the rule: the message names the variable, not the value
        print(f"CONFIG ERROR {exc}", file=sys.stderr)
        return 2
    print("provisioned roles: " + ", ".join(roles))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/infra/docker/", 1)[0] or ".")
    sys.exit(main())
