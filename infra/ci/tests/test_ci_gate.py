"""Self-tests for scripts/ci_gate.py and scripts/mutation_check.py. No database needed."""
import pathlib
import subprocess
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
SENTINEL = "expected a backend blocked on a lock"


def case(name, child=None, message=""):
    inner = f'<{child} message="{message}"/>' if child else ""
    return f'<testcase name="{name}">{inner}</testcase>'


T15 = "test_T15_restore_then_replay_ledger"
BASE = {
    "t1": case("test_T1_valid_permission_sends"),
    "t23b": case("test_T23b_scout_never_writes_pii_shaped_signal_ref", "skipped"),
    "clean": case("test_clean_db_up_down_up"),
    "behind": case("test_db_one_migration_behind[0]"),
    "t15": case(T15),
}


def run(tmp_path, script, **overrides):
    parts = {**BASE, **overrides}
    body = "".join(v for v in parts.values() if v is not None)
    f = tmp_path / "junit.xml"
    f.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    return subprocess.run([sys.executable, str(SCRIPTS / script), str(f)], capture_output=True, text=True)


def gate(tmp_path, **o):
    return run(tmp_path, "ci_gate.py", **o)


# ---------------- ci_gate.py
def test_gate_all_good(tmp_path):
    r = gate(tmp_path)
    assert r.returncode == 0 and "GATE OK" in r.stdout


def test_gate_t23b_skip_is_explicitly_allowed(tmp_path):
    assert "skipped" in BASE["t23b"]                       # the base fixture really contains the skip
    r = gate(tmp_path)
    assert r.returncode == 0


@pytest.mark.parametrize("child", ["skipped", "error", "failure"])
def test_gate_t15_not_passed_fails(tmp_path, child):
    r = gate(tmp_path, t15=case(T15, child))
    assert r.returncode != 0 and "test_T15" in r.stderr


def test_gate_t15_missing_fails(tmp_path):
    assert gate(tmp_path, t15=None).returncode != 0


def test_gate_t15_duplicated_fails(tmp_path):
    assert gate(tmp_path, t15=case(T15) + case(T15 + "_again")).returncode != 0


def test_gate_clean_roundtrip_missing_or_failed_fails(tmp_path):
    assert gate(tmp_path, clean=None).returncode != 0
    assert gate(tmp_path, clean=case("test_clean_db_up_down_up", "failure")).returncode != 0


def test_gate_behind_case_missing_or_failed_fails(tmp_path):
    assert gate(tmp_path, behind=None).returncode != 0
    assert gate(tmp_path, behind=case("test_db_one_migration_behind[0]", "error")).returncode != 0


def test_gate_any_failure_or_error_anywhere_fails(tmp_path):
    for child in ("failure", "error"):
        assert gate(tmp_path, t1=case("test_T1_valid_permission_sends", child)).returncode != 0


def test_gate_unexpected_skip_fails(tmp_path):
    r = gate(tmp_path, t1=case("test_T1_valid_permission_sends", "skipped"))
    assert r.returncode != 0 and "only" in r.stderr


def test_gate_missing_file_fails(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPTS / "ci_gate.py"), str(tmp_path / "nope.xml")],
                       capture_output=True, text=True)
    assert r.returncode != 0


def test_gate_corrupt_file_fails(tmp_path):
    f = tmp_path / "bad.xml"
    f.write_text("<garbage")
    r = subprocess.run([sys.executable, str(SCRIPTS / "ci_gate.py"), str(f)], capture_output=True, text=True)
    assert r.returncode != 0


def test_gate_empty_suite_fails(tmp_path):
    f = tmp_path / "empty.xml"
    f.write_text("<testsuites><testsuite></testsuite></testsuites>")
    r = subprocess.run([sys.executable, str(SCRIPTS / "ci_gate.py"), str(f)], capture_output=True, text=True)
    assert r.returncode != 0


# ---------------- mutation_check.py (the T10 'lock actually matters' proof)
T10A, T10B = "test_T10a_withdrawal_waits_for_in_flight_send", "test_T10b_send_waits_for_in_flight_withdrawal_then_fails"


def mut(tmp_path, a, b):
    f = tmp_path / "mutant.xml"
    f.write_text(f"<testsuites><testsuite>{a or ''}{b or ''}</testsuite></testsuites>")
    return subprocess.run([sys.executable, str(SCRIPTS / "mutation_check.py"), str(f)], capture_output=True, text=True)


def test_mutation_both_fail_via_sentinel_is_ok(tmp_path):
    r = mut(tmp_path, case(T10A, "failure", SENTINEL), case(T10B, "failure", SENTINEL))
    assert r.returncode == 0


def test_mutation_test_passing_against_mutant_is_rejected(tmp_path):
    r = mut(tmp_path, case(T10A), case(T10B, "failure", SENTINEL))
    assert r.returncode != 0 and "does not prove the lock" in r.stderr


def test_mutation_failing_for_wrong_reason_is_rejected(tmp_path):
    r = mut(tmp_path, case(T10A, "failure", "AssertionError: something else"), case(T10B, "failure", SENTINEL))
    assert r.returncode != 0 and "sentinel" in r.stderr


def test_mutation_error_skip_or_missing_is_rejected(tmp_path):
    assert mut(tmp_path, case(T10A, "error"), case(T10B, "failure", SENTINEL)).returncode != 0
    assert mut(tmp_path, case(T10A, "skipped"), case(T10B, "failure", SENTINEL)).returncode != 0
    assert mut(tmp_path, None, case(T10B, "failure", SENTINEL)).returncode != 0
