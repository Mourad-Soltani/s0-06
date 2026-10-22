"""Kill switch orchestration and CLI, with a fake store: no database. Names are KU*."""
import pytest

from shared.killswitch import GLOBAL, KillSwitch, KillResult
from shared.killswitch_cli import main

MARKETS = ["GUMROAD", "ETSY", "PAYHIP"]


class FakeStore:
    def __init__(self):
        self.killed = {GLOBAL: False, **{m: False for m in MARKETS}}
        self.writes = []

    def get_state(self, scope):
        return self.killed[scope]

    def set_state(self, scope, killed, actor, reason_code):
        self.writes.append((scope, killed, actor, reason_code))
        self.killed[scope] = killed

    def marketplaces(self):
        return list(MARKETS)

    def states(self):
        return list(self.killed.items())


class Revoker:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def revoke(self, marketplace):
        self.calls.append(marketplace)
        if marketplace in self.fail:
            raise RuntimeError("vault said no for tenant@example.test")


def test_KU1_kill_flips_then_revokes_one_marketplace_and_audits():
    store, rev, seen = FakeStore(), Revoker(), []
    res = KillSwitch(store, rev, lambda a, d: seen.append((a, d))).kill("GUMROAD", "ops-1", "INCIDENT")
    assert store.killed["GUMROAD"] is True and store.killed["ETSY"] is False
    assert rev.calls == ["GUMROAD"] and res == KillResult("GUMROAD", changed=True)
    assert res.fully_applied
    assert seen == [("KILL", {"scope": "GUMROAD", "actor": "ops-1", "reason_code": "INCIDENT",
                              "changed": True, "failed_revocations": 0})]


def test_KU2_global_kill_revokes_every_marketplace():
    rev = Revoker()
    KillSwitch(FakeStore(), rev).kill(GLOBAL, "ops-1", "INCIDENT")
    assert rev.calls == MARKETS


def test_KU3_a_failed_revocation_is_reported_by_class_code_and_never_undoes_the_kill():
    store, rev = FakeStore(), Revoker(fail={"ETSY"})
    res = KillSwitch(store, rev).kill(GLOBAL, "ops-1", "INCIDENT")
    assert store.killed[GLOBAL] is True                      # the kill stands
    assert rev.calls == MARKETS                              # one failure does not stop the others
    assert res.failed_revocations == (("ETSY", "RUNTIME_ERROR"),) and not res.fully_applied
    assert "tenant@example.test" not in repr(res)            # exception text never leaves the failing call


def test_KU4_repeating_a_kill_changes_nothing_but_retries_revocation():
    store, rev = FakeStore(), Revoker(fail={"GUMROAD"})
    ks = KillSwitch(store, rev)
    assert ks.kill("GUMROAD", "ops-1", "INCIDENT").changed is True
    rev.fail.clear()
    again = ks.kill("GUMROAD", "ops-2", "INCIDENT")
    assert again.changed is False and again.fully_applied and rev.calls == ["GUMROAD", "GUMROAD"]


def test_KU5_an_unknown_scope_is_refused_before_anything_changes():
    store, rev, seen = FakeStore(), Revoker(), []
    ks = KillSwitch(store, rev, lambda a, d: seen.append(a))
    for call in (lambda: ks.kill("NOPE", "ops-1", "X"), lambda: ks.revive("NOPE", "ops-1")):
        with pytest.raises(ValueError):
            call()
    assert store.writes == [] and rev.calls == [] and seen == []


def test_KU6_revive_flips_back_without_touching_credentials():
    store, rev = FakeStore(), Revoker()
    ks = KillSwitch(store, rev)
    ks.kill("GUMROAD", "ops-1", "INCIDENT")
    calls_after_kill = list(rev.calls)
    res = ks.revive("GUMROAD", "ops-1")
    assert store.killed["GUMROAD"] is False and res.changed is True and rev.calls == calls_after_kill
    assert ks.revive("GUMROAD", "ops-1").changed is False


def test_KU7_an_audit_failure_is_reported_and_never_undoes_the_kill():
    def broken(action, details):
        raise OSError("disk full")

    store = FakeStore()
    res = KillSwitch(store, Revoker(), broken).kill("GUMROAD", "ops-1", "INCIDENT")
    assert store.killed["GUMROAD"] is True
    assert res.audit_error_code == "OSERROR" and not res.fully_applied      # acronym prefixes are not split


def run_cli(argv, ks):
    lines = []
    code = main(argv, make_service=lambda dsn: ks, out=lines.append)
    return code, lines


def test_KU8_cli_exit_codes_and_loud_failures():
    ks = KillSwitch(FakeStore(), Revoker())
    code, lines = run_cli(["--dsn", "x", "kill", "GUMROAD", "--actor", "ops-1", "--reason", "INCIDENT"], ks)
    assert code == 0 and lines == ["kill GUMROAD: changed"]
    code, lines = run_cli(["--dsn", "x", "status"], ks)
    assert code == 0 and any(l.startswith("GUMROAD") and "KILLED" in l for l in lines)
    ks2 = KillSwitch(FakeStore(), Revoker(fail={"GUMROAD"}))
    code, lines = run_cli(["--dsn", "x", "kill", "GUMROAD", "--actor", "ops-1", "--reason", "INCIDENT"], ks2)
    assert code == 2 and any("CREDENTIALS NOT REVOKED for GUMROAD" in l for l in lines)
    code, lines = run_cli(["--dsn", "x", "kill", "NOPE", "--actor", "ops-1", "--reason", "X"], ks)
    assert code == 1 and lines == ["error: ValueError"]
    code, lines = run_cli(["--dsn", "x", "revive", "GUMROAD", "--actor", "ops-1"], ks)
    assert code == 0 and lines == ["revive GUMROAD: changed"]


def test_KU9_cli_needs_a_dsn():
    import os
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        code, lines = run_cli(["status"], KillSwitch(FakeStore(), Revoker()))
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved
    assert code == 1 and "no --dsn" in lines[0]
