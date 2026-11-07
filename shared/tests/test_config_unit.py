"""Settings: sources and their order, validation by name only, secrets that never print. No network, no database. Names CF*."""
import pickle
from decimal import Decimal

import pytest

from shared.config import ConfigError, Secret, Settings, load_environ, load_settings, read_env_file

GOOD = {
    "DATABASE_URL": "postgresql://sap_worker:s3cret-pw@db:5432/sap", "POSTMARK_SERVER_TOKEN": "pm-token-abcdef", "EMAIL_FROM": "hello@seller.example.com",
    "EMAIL_POSTAL_ADDRESS": "1 Example Street, Paris 75001, France", "EMAIL_UNSUBSCRIBE_URL": "https://seller.example.com/unsubscribe",
    "ALLOWED_LINK_HOSTS": "gumroad.com, Buy.Example.com", "ICP_PRODUCT_IDS": "gr-1,ls-1", "GUMROAD_ACCESS_TOKEN": "gum-token-xyz"}


def problems(env):
    with pytest.raises(ConfigError) as e:
        load_settings(env)
    return e.value.problems


def test_CF1_a_minimal_good_environment_loads_with_safe_defaults():
    s = load_settings(GOOD)
    assert isinstance(s, Settings) and s.marketplaces == ("GUMROAD",) and s.allowed_link_hosts == {"gumroad.com", "buy.example.com"}
    assert (s.postmark_stream, s.postmark_lookup, s.routing_enabled, s.max_discount_pct, s.worker_id, s.poll_interval, s.sweep_seconds) == (
        "outbound", False, False, Decimal("0"), "worker-1", 1.0, 60.0)
    assert s.icp_product_ids == {"gr-1", "ls-1"} and s.llm_configured is False and s.lemon_squeezy_key is None


def test_CF2_secrets_never_print_in_a_repr_a_str_or_a_pickle():
    s = load_settings({**GOOD, "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "llm-key-123456"})
    shown = repr(s) + str(s) + repr(s.database_url) + f"{s.postmark_token}" + str(s.llm_key)
    for value in ("s3cret-pw", "pm-token-abcdef", "gum-token-xyz", "llm-key-123456"):
        assert value not in shown
    assert s.database_url.reveal() == GOOD["DATABASE_URL"] and repr(s.postmark_token) == "Secret(***)" and s.llm_configured is True
    with pytest.raises(TypeError):
        pickle.dumps(s.postmark_token)
    with pytest.raises(AttributeError):
        s.postmark_token._value = "x"


def test_CF3_every_problem_is_reported_at_once_by_name_and_no_value_is_echoed():
    bad = {"DATABASE_URL": "mysql://u:leaked-pw@h/db", "POSTMARK_SERVER_TOKEN": "", "EMAIL_FROM": "not an address", "EMAIL_POSTAL_ADDRESS": "short",
           "EMAIL_UNSUBSCRIBE_URL": "http://x.test/u", "ALLOWED_LINK_HOSTS": "not a host", "ICP_PRODUCT_IDS": "bad id!",
           "MAX_DISCOUNT_PCT": "150", "POLL_INTERVAL_SECONDS": "abc", "ROUTING_ENABLED": "maybe"}
    got = problems(bad)
    names = {p.split(":")[0] for p in got}
    assert {"DATABASE_URL", "POSTMARK_SERVER_TOKEN", "EMAIL_FROM", "EMAIL_POSTAL_ADDRESS", "EMAIL_UNSUBSCRIBE_URL", "ALLOWED_LINK_HOSTS", "ICP_PRODUCT_IDS",
            "MAX_DISCOUNT_PCT", "POLL_INTERVAL_SECONDS", "ROUTING_ENABLED", "GUMROAD_ACCESS_TOKEN"} <= names
    assert "leaked-pw" not in " ".join(got) and "not an address" not in " ".join(got)


@pytest.mark.parametrize("name", ["DATABASE_URL", "POSTMARK_SERVER_TOKEN", "EMAIL_FROM", "EMAIL_POSTAL_ADDRESS", "EMAIL_UNSUBSCRIBE_URL", "ALLOWED_LINK_HOSTS", "ICP_PRODUCT_IDS"])
def test_CF4_each_required_setting_missing_is_an_error_naming_it(name):
    env = {k: v for k, v in GOOD.items() if k != name}
    assert problems(env) == (f"{name}: is required",)


def test_CF5_at_least_one_marketplace_is_needed_and_pairs_must_be_complete():
    env = {k: v for k, v in GOOD.items() if k != "GUMROAD_ACCESS_TOKEN"}
    assert problems(env)[0].startswith("GUMROAD_ACCESS_TOKEN: at least one marketplace")
    assert problems({**env, "LEMON_SQUEEZY_API_KEY": "k"})[0].startswith("LEMON_SQUEEZY_API_KEY: set together")
    assert problems({**env, "LEMON_SQUEEZY_STORE_ID": "42"})[0].startswith("LEMON_SQUEEZY_API_KEY: set together")
    assert problems({**GOOD, "LLM_PROVIDER": "anthropic"})[0].startswith("LLM_API_KEY: set together")
    assert problems({**GOOD, "LLM_API_KEY": "k"})[0].startswith("LLM_API_KEY: set together")
    assert problems({**env, "PAYHIP_API_KEY": "p"}) == ("PAYHIP_CATALOG_FILE: required with PAYHIP_API_KEY (Payhip cannot list products)",)
    s = load_settings({**env, "LEMON_SQUEEZY_API_KEY": "k", "LEMON_SQUEEZY_STORE_ID": "42", "PAYHIP_API_KEY": "p", "PAYHIP_CATALOG_FILE": "/data/payhip.json"})
    assert s.marketplaces == ("LEMON_SQUEEZY", "PAYHIP") and s.lemon_squeezy_store_id == "42" and s.payhip_catalog_file == "/data/payhip.json"


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("YES", True), ("on", True), ("false", False), ("0", False), ("off", False), ("", False)])
def test_CF6_flags_accept_the_usual_spellings_and_default_to_off(raw, expected):
    s = load_settings({**GOOD, "ROUTING_ENABLED": raw, "POSTMARK_LOOKUP": raw})
    assert (s.routing_enabled, s.postmark_lookup) == (expected, expected)


def test_CF7_numbers_are_range_checked_and_the_discount_is_exact():
    s = load_settings({**GOOD, "MAX_DISCOUNT_PCT": "12.5", "POLL_INTERVAL_SECONDS": "2", "CLOSER_SWEEP_SECONDS": "0", "WORKER_ID": "w-2"})
    assert (s.max_discount_pct, s.poll_interval, s.sweep_seconds, s.worker_id) == (Decimal("12.5"), 2.0, 0.0, "w-2")
    for name, value in (("MAX_DISCOUNT_PCT", "-1"), ("MAX_DISCOUNT_PCT", "NaN"), ("POLL_INTERVAL_SECONDS", "0"), ("POLL_INTERVAL_SECONDS", "61"),
                        ("CLOSER_SWEEP_SECONDS", "-5"), ("WORKER_ID", "has space")):
        assert problems({**GOOD, name: value})[0].startswith(name)


def test_CF8_an_env_file_fills_gaps_but_the_real_environment_wins(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# development only\n\nDATABASE_URL="postgresql://u:p@h/db"\nPOSTMARK_SERVER_TOKEN=from-file\nEMAIL_FROM=file@seller.example.com\n'
                 "EMAIL_POSTAL_ADDRESS='1 Example Street, Paris 75001, France'\nEMAIL_UNSUBSCRIBE_URL=https://seller.example.com/u\n"
                 "ALLOWED_LINK_HOSTS=gumroad.com\nICP_PRODUCT_IDS=gr-1\nGUMROAD_ACCESS_TOKEN=g\n")
    s = load_settings({"POSTMARK_SERVER_TOKEN": "from-env"}, env_file=str(f))
    assert s.postmark_token.reveal() == "from-env" and s.email_from == "file@seller.example.com" and s.postal_address.startswith("1 Example")
    assert load_settings(GOOD, env_file=str(tmp_path / "missing.env")).email_from == "hello@seller.example.com"          # a missing env file is simply not used


def test_CF9_a_malformed_env_file_reports_line_numbers_never_content(tmp_path):
    f = tmp_path / ".env"
    f.write_text("GOOD=1\nthis line has no equals sign and a secret-looking-thing\nlower=case\n")
    with pytest.raises(ConfigError) as e:
        read_env_file(f)
    assert e.value.problems == ("line 2: not NAME=value", "line 3: not NAME=value") and "secret-looking-thing" not in str(e.value)


def test_CF10_a_secret_can_come_from_a_file_and_not_both_ways(tmp_path):
    f = tmp_path / "pm_token"
    f.write_text("token-from-file\n")
    env = {k: v for k, v in GOOD.items() if k != "POSTMARK_SERVER_TOKEN"}
    assert load_settings({**env, "POSTMARK_SERVER_TOKEN_FILE": str(f)}).postmark_token.reveal() == "token-from-file"      # the trailing newline is dropped
    assert problems({**env, "POSTMARK_SERVER_TOKEN": "x", "POSTMARK_SERVER_TOKEN_FILE": str(f)}) == ("POSTMARK_SERVER_TOKEN: set both directly and as POSTMARK_SERVER_TOKEN_FILE",)
    assert problems({**env, "POSTMARK_SERVER_TOKEN_FILE": str(tmp_path / "nope")}) == ("POSTMARK_SERVER_TOKEN_FILE: file cannot be read",)


def test_CF11_only_the_named_secrets_honour_a_file_variable():
    merged = load_environ({"REVIEW_API_TOKENS_FILE": "/does/not/exist", "WEBHOOK_CONFIG_FILE": "/nope", "EMAIL_FROM": "a@b.test"})
    assert "REVIEW_API_TOKENS" not in merged and merged["REVIEW_API_TOKENS_FILE"] == "/does/not/exist"


def test_CF12_blank_values_count_as_unset_and_optional_settings_stay_optional():
    s = load_settings({**GOOD, "PAYHIP_API_KEY": "   ", "LLM_PROVIDER": "", "POSTMARK_MESSAGE_STREAM": ""})
    assert s.payhip_key is None and s.llm_provider is None and s.postmark_stream == "outbound"
