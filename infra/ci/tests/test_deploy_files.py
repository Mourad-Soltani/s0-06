"""The deployment files agree with the code and keep secrets out of the image and the repository. Plain text checks: no Docker, no YAML library.
Names DP*."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "infra" / "docker"))
def _strip_comments(text):
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line).rstrip() for line in text.splitlines())


COMPOSE = _strip_comments((ROOT / "docker-compose.yml").read_text())          # comments may mention ports and env files: only the settings count
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
SECRET_WORDS = ("PASSWORD", "TOKEN", "_KEY", "DATABASE_URL")


def service_blocks():
    """service name -> its text, from the two-space-indented keys under `services:`."""
    body = COMPOSE.split("\nservices:\n", 1)[1].split("\nsecrets:\n", 1)[0]
    parts = re.split(r"^  ([a-z][a-z0-9-]*):\s*$", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def env_names(text):
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.M))


def interpolated(text):
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))


def read_by_settings():
    src = (ROOT / "shared" / "config.py").read_text()
    return set(re.findall(r'\b(?:text|secret|number|integer|flag|csv)\("([A-Z][A-Z0-9_]*)"', src))


def test_DP1_the_expected_services_exist():
    assert set(service_blocks()) == {"db", "migrate", "worker", "webhooks", "review-api", "ui"}


def test_DP2_every_setting_the_code_reads_is_in_the_env_example_and_passed_to_the_worker():
    wanted = read_by_settings()
    assert {"DATABASE_URL", "POSTMARK_SERVER_TOKEN", "GUMROAD_ACCESS_TOKEN", "LEMON_SQUEEZY_API_KEY", "PAYHIP_API_KEY", "LLM_API_KEY", "ROUTING_ENABLED"} <= wanted
    worker = service_blocks()["worker"]
    for name in sorted(wanted):
        assert re.search(rf"^\s+{name}:", worker, flags=re.M), f"{name} is read by the code but not passed to the worker"
        if name != "DATABASE_URL":                        # the worker's DSN is built in compose from the role password
            assert name in env_names(ENV_EXAMPLE), f"{name} missing from .env.example"


def test_DP3_every_variable_compose_interpolates_is_documented_in_the_env_example():
    assert interpolated(COMPOSE) <= env_names(ENV_EXAMPLE) | set()


def test_DP4_the_env_example_holds_no_secret_values():
    for line in ENV_EXAMPLE.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            if any(w in name for w in SECRET_WORDS):
                assert value.strip() == "", f"{name} has a value in the example file"


def test_DP5_no_secret_is_written_into_compose_and_services_get_no_blanket_env_file():
    assert "env_file" not in COMPOSE
    for line in COMPOSE.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or ":" not in stripped:
            continue
        name, value = stripped.split(":", 1)
        if any(w in name for w in SECRET_WORDS) and value.strip() and not value.strip().startswith("/run/secrets/"):     # a path to a mounted secret file is fine
            assert "${" in value, f"{name} must come from the environment"


def test_DP6_each_service_receives_only_what_it_needs():
    blocks = service_blocks()
    ui, review, worker, db = (interpolated(blocks[k]) for k in ("ui", "review-api", "worker", "db"))
    assert ui == set()                                                     # the UI holds no credential at all: the reviewer types a token
    assert review == {"SAP_REVIEW_PASSWORD"}
    assert interpolated(blocks["webhooks"]) == {"SAP_WEBHOOK_PASSWORD"}                  # the internet-facing receiver holds no email, marketplace or model key
    assert "POSTMARK_SERVER_TOKEN" in worker and not worker & {"POSTGRES_PASSWORD", "SAP_REVIEW_PASSWORD", "SAP_WEBHOOK_PASSWORD"}
    assert db == {"POSTGRES_PASSWORD"}
    assert not interpolated(blocks["review-api"]) & {"POSTMARK_SERVER_TOKEN", "GUMROAD_ACCESS_TOKEN", "LEMON_SQUEEZY_API_KEY", "LLM_API_KEY"}


def test_DP7_only_the_review_ui_is_published_and_only_on_loopback_and_the_database_is_not():
    blocks = service_blocks()
    published = re.findall(r'^\s+- "([^"]*:\d+:\d+)"', COMPOSE, flags=re.M)
    assert published == ["127.0.0.1:8081:8081", "127.0.0.1:8501:8501"]                   # the receiver and the review UI, on loopback, behind a TLS proxy
    assert all("ports:" not in blocks[name] for name in ("db", "migrate", "worker", "ui"))
    assert 'network_mode: "service:review-api"' in blocks["ui"]


def test_DP8_the_services_start_the_right_programs_and_the_worker_waits_for_the_migrations():
    blocks = service_blocks()
    assert "orchestrator.worker_main" in blocks["worker"] and "orchestrator.review_server" in blocks["review-api"] and "ui/app.py" in blocks["ui"]
    assert "orchestrator.webhook_server" in blocks["webhooks"] and "webhook_config" in blocks["webhooks"] and "BIND_HOST: 0.0.0.0" in blocks["webhooks"]
    assert "alembic upgrade head" in blocks["migrate"] and "provision_roles.py" in blocks["migrate"]
    for name in ("worker", "review-api", "webhooks"):
        assert "service_completed_successfully" in blocks[name]
    assert "service_healthy" in blocks["migrate"]
    for path in ("orchestrator/worker_main.py", "orchestrator/review_server.py", "orchestrator/webhook_server.py", "ui/app.py", "infra/docker/provision_roles.py", "infra/schema/alembic.ini"):
        assert (ROOT / path).is_file(), path


def test_DP9_the_image_runs_unprivileged_and_never_copies_credentials():
    docker = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^USER app$", docker, flags=re.M) and "--uid 10001" in docker
    assert not re.search(r"^\s*(ENV|ARG)\s+\S*(PASSWORD|TOKEN|KEY|SECRET)", docker, flags=re.M | re.I)
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert {".env", "secrets/", "config/", ".git"} <= set(ignore) and "!.env.example" in ignore


def test_DP10_credentials_are_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert {".env", "secrets/*", "config/*"} <= set(ignored) and {"!secrets/review_tokens.example.json", "!secrets/webhook_config.example.json"} <= set(ignored)
    assert (ROOT / "secrets" / "review_tokens.example.json").is_file() and not (ROOT / ".env").exists()


def test_DP11_provisioning_refuses_weak_or_url_unsafe_passwords_before_creating_anything():
    import provision_roles
    good = "a" * 20
    three = {"SAP_WORKER_PASSWORD": good, "SAP_REVIEW_PASSWORD": good, "SAP_WEBHOOK_PASSWORD": good}
    calls = []

    class Conn:
        def execute(self, *a, **k):
            calls.append(a)

    for bad in ("", "short", "x" * 19, "has space in it xxxxxxxxxxxx", "slash/inside-xxxxxxxxxxxxxx", "p@ss:word-xxxxxxxxxxxxxxx", "x" * 129):
        with pytest.raises(ValueError) as e:
            provision_roles.provision(Conn(), {**three, "SAP_WEBHOOK_PASSWORD": bad})
        assert "SAP_WEBHOOK_PASSWORD" in str(e.value) and (bad not in str(e.value) or bad == "")
    assert calls == []                                                    # no role was created before the last password failed
    assert [(r, g) for r, _, g in provision_roles.ROLES] == [("sap_worker", "review_svc"), ("sap_review", "review_svc"), ("sap_webhook", "app_rw")]


def test_DP12_the_worker_has_the_three_commands_the_runbook_names():
    src = (ROOT / "orchestrator" / "worker_main.py").read_text()
    assert 'choices=("run", "check", "reconcile")' in src
