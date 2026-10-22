"""Proves a test depends on the thing it claims to test, by running it against a MUTANT and requiring it to fail.

Default (no extra args): T10a and T10b must fail through the lock-waiter sentinel (the 0001 send-gate locks).
  python mutation_check.py mutant.xml
Generalized:
  python mutation_check.py mutant2.xml --expect test_K7a test_K7b            # lock tests, sentinel required
  python mutation_check.py mutant3.xml --expect test_KQ1 test_KQ2 --no-sentinel   # any assertion failure
Exits 0 only if EVERY expected case failed (not errored, skipped or passed) and, unless --no-sentinel, failed
for the right reason (the lock-waiter sentinel), not from an infra problem.
"""
import sys
import xml.etree.ElementTree as ET

SENTINEL = "expected a backend blocked on a lock"
DEFAULT_NEEDED = ("test_T10a", "test_T10b")


def main(path, needed=DEFAULT_NEEDED, require_sentinel=True):
    try:
        root = ET.parse(path).getroot()
    except (FileNotFoundError, ET.ParseError) as e:
        sys.exit(f"MUTATION FAIL: {path} missing or unreadable ({e})")
    cases = list(root.iter("testcase"))
    problems = []
    for prefix in needed:
        found = [c for c in cases if c.get("name", "").startswith(prefix)]
        if len(found) != 1:
            problems.append(f"{prefix}: expected exactly 1 case, found {len(found)}")
            continue
        c = found[0]
        if c.find("error") is not None:
            problems.append(f"{prefix}: errored (harness problem, not a real failure)")
        elif c.find("skipped") is not None:
            problems.append(f"{prefix}: skipped")
        elif c.find("failure") is None:
            problems.append(f"{prefix}: PASSED against the mutant - the test does not prove the lock or condition it claims")
        elif require_sentinel and SENTINEL not in (c.find("failure").get("message") or "") + (c.find("failure").text or ""):
            problems.append(f"{prefix}: failed, but not via the lock-waiter sentinel")
    if problems:
        sys.exit("MUTATION FAIL:\n  " + "\n  ".join(problems))
    if tuple(needed) == DEFAULT_NEEDED and require_sentinel:
        print("MUTATION OK: T10a and T10b both fail when FOR SHARE is removed")
    else:
        print(f"MUTATION OK: {' and '.join(p.replace('test_', '') for p in needed)} fail against the mutant")


def parse(argv):
    if not argv:
        sys.exit("usage: mutation_check.py junit.xml [--expect NAME_PREFIX ...] [--no-sentinel]")
    path, needed, require, mode = argv[0], [], True, None
    for tok in argv[1:]:
        if tok == "--expect":
            mode = "expect"
        elif tok == "--no-sentinel":
            require, mode = False, None
        elif mode == "expect":
            needed.append(tok)
        else:
            sys.exit(f"unexpected argument {tok!r}")
    return path, tuple(needed) or DEFAULT_NEEDED, require


if __name__ == "__main__":
    main(*parse(sys.argv[1:]))
