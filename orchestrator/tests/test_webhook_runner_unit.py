"""The webhook receiver's production runner and the per-call inbox: configuration errors name settings and never values, the server gets the right
limits, and a connection string never prints. No network, no database. Names WS*."""
import contextlib
import io
import json
import sys
import types

import pytest

from orchestrator import webhook_server
from orchestrator.inbox_access import DsnSignalInbox
from orchestrator.webhook_app import MAX_BODY, WebhookApp
from shared.config import ConfigError

DSN = "postgresql://sap_webhook:pw-0123456789abcdefgh@db:5432/sap"
LS_SECRET, PH_KEY = "whsec-secret-0123456789", "payhip-key-secret-0123456789"
CFG = {"lemonsqueezy": {"secret": LS_SECRET, "store_id": 12345, "path_token": "ls-path-token-0123456789"},
       "payhip": {"api_key": PH_KEY, "path_token": "ph-path-token-0123456789"}}


class Inbox:
    def write(self, signal, attributed_lead=None):
        return True


def config_file(tmp_path, content=CFG):
    f = tmp_path / "webhooks.json"
    f.write_text(content if isinstance(content, str) else json.dumps(content))
    return str(f)


def env(tmp_path, content=CFG, **extra):
    return {"DATABASE_URL": DSN, "WEBHOOK_CONFIG_FILE": config_file(tmp_path, content), **extra}


def problems(environ, inbox=None):
    with pytest.raises(ConfigError) as e:
        webhook_server.build(environ, inbox=inbox)
    return e.value.problems


def test_WS1_a_good_configuration_builds_the_app_with_both_providers(tmp_path):
    app = webhook_server.build(env(tmp_path), inbox=Inbox())
    assert isinstance(app, WebhookApp) and set(app._handlers) == {"lemonsqueezy", "payhip"}
    only_ls = webhook_server.build(env(tmp_path, {"lemonsqueezy": CFG["lemonsqueezy"]}), inbox=Inbox())
    assert set(only_ls._handlers) == {"lemonsqueezy"}
    assert isinstance(webhook_server.build(env(tmp_path))._inbox, DsnSignalInbox)               # no inbox given: one that opens a connection per call


def test_WS2_every_kind_of_bad_configuration_names_the_setting_and_never_a_value(tmp_path):
    bad_file = "WEBHOOK_CONFIG_FILE: must be a readable JSON object"
    assert problems({"DATABASE_URL": DSN, "WEBHOOK_CONFIG_FILE": str(tmp_path / "missing.json")}) == (bad_file,)
    assert problems(env(tmp_path, "not json")) == (bad_file,) and problems(env(tmp_path, "[1, 2]")) == (bad_file,)
    assert problems(env(tmp_path, {})) == ("WEBHOOK_CONFIG_FILE: configure at least one of lemonsqueezy, payhip",)
    assert problems({"WEBHOOK_CONFIG_FILE": config_file(tmp_path)}) == ("DATABASE_URL: must be a postgresql:// URL",)
    assert problems({"DATABASE_URL": "mysql://u:pw-leak-0123456789@h/d", "WEBHOOK_CONFIG_FILE": config_file(tmp_path)}) == ("DATABASE_URL: must be a postgresql:// URL",)
    each = "WEBHOOK_CONFIG_FILE: each provider needs its secret or api_key, store_id (Lemon Squeezy) and a path_token of 16 to 128 safe characters"
    broken = [{"lemonsqueezy": {**CFG["lemonsqueezy"], "path_token": "short"}}, {"lemonsqueezy": {**CFG["lemonsqueezy"], "path_token": "has space 0123456789"}},
              {"lemonsqueezy": {"secret": LS_SECRET, "path_token": "ls-path-token-0123456789"}}, {"lemonsqueezy": {**CFG["lemonsqueezy"], "store_id": 0}},
              {"lemonsqueezy": {**CFG["lemonsqueezy"], "store_id": "abc"}}, {"lemonsqueezy": {**CFG["lemonsqueezy"], "secret": "  "}},
              {"payhip": {"api_key": PH_KEY}}, {"payhip": {**CFG["payhip"], "path_token": "x" * 129}}, {"lemonsqueezy": "text"}]
    for content in broken:
        got = problems(env(tmp_path, content))
        assert got == (each,), content
        assert LS_SECRET not in " ".join(got) and PH_KEY not in " ".join(got)
    assert problems(env(tmp_path, {"gumroad": {"secret": "x"}})) == ("WEBHOOK_CONFIG_FILE: configure at least one of lemonsqueezy, payhip",)


def test_WS3_a_bad_configuration_or_port_stops_the_process_with_names_only(tmp_path):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = webhook_server.main({"DATABASE_URL": "mysql://u:pw-leak-0123456789@h/d", "WEBHOOK_CONFIG_FILE": config_file(tmp_path)})
        code_port = webhook_server.main({**env(tmp_path), "PORT": "99999"})
    assert code == 2 and code_port == 2 and "CONFIG ERROR DATABASE_URL" in err.getvalue() and "CONFIG ERROR PORT" in err.getvalue()
    assert "pw-leak" not in err.getvalue() and LS_SECRET not in err.getvalue()


def test_WS4_the_server_binds_where_it_is_told_with_the_body_limit_threads_and_no_server_banner(tmp_path):
    calls = []
    fake = types.SimpleNamespace(serve=lambda app, **kw: calls.append((app, kw)))
    saved = sys.modules.get("waitress")
    sys.modules["waitress"] = fake
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert webhook_server.main({**env(tmp_path), "BIND_HOST": "0.0.0.0", "PORT": "9090"}) == 0
            assert webhook_server.main(env(tmp_path)) == 0
    finally:
        if saved is None:
            del sys.modules["waitress"]
        else:
            sys.modules["waitress"] = saved
    (app1, kw1), (_, kw2) = calls
    assert isinstance(app1, WebhookApp)
    assert (kw1["host"], kw1["port"], kw1["threads"], kw1["max_request_body_size"], kw1["ident"]) == ("0.0.0.0", 9090, 4, MAX_BODY, "webhooks")
    assert (kw2["host"], kw2["port"]) == ("127.0.0.1", 8081)                                         # the safe defaults when nothing is set
    assert "pw-0123456789" not in out.getvalue() and LS_SECRET not in out.getvalue()


def test_WS5_the_per_call_inbox_checks_its_url_hides_it_and_connects_only_when_used():
    inbox = DsnSignalInbox(DSN)                                                                      # no connection is opened here
    assert repr(inbox) == "DsnSignalInbox()" and "pw-0123" not in repr(inbox)
    assert repr(DsnSignalInbox("host=db dbname=sap user=u password=pw-0123456789")) == "DsnSignalInbox()"       # libpq key=value form is accepted too
    for bad in ("", "   ", None, 5):
        with pytest.raises(ValueError):
            DsnSignalInbox(bad)


def test_WS6_the_waitress_dependency_is_declared():
    from pathlib import Path
    lines = (Path(__file__).resolve().parents[2] / "requirements.txt").read_text().split()
    assert any(l.lower().startswith("waitress") for l in lines)
