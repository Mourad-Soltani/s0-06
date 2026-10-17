"""Proves T10a/T10b actually depend on the FOR SHARE lock.

CI runs the T10 tests against a MUTANT schema (FOR SHARE removed) and then calls this script
on that run's junit XML. It exits 0 only if BOTH T10 cases FAILED and failed *for the right
reason* (the lock-waiter sentinel), not from an infra error, a skip, or a pass.
"""
import sys
import xml.etree.ElementTree as ET

SENTINEL = "expected a backend blocked on a lock"
NEEDED = ("test_T10a", "test_T10b")


def main(path: str) -> None:
    try:
        root = ET.parse(path).getroot()
    except (FileNotFoundError, ET.ParseError) as e:
        sys.exit(f"MUTATION FAIL: {path} missing or unreadable ({e})")
    cases = list(root.iter("testcase"))
    problems = []
    for prefix in NEEDED:
        found = [c for c in cases if c.get("name", "").startswith(prefix)]
        if len(found) != 1:
            problems.append(f"{prefix}: expected exactly 1 case, found {len(found)}")
            continue
        c = found[0]
        if c.find("error") is not None:
            problems.append(f"{prefix}: errored (harness problem, not a lock failure)")
        elif c.find("skipped") is not None:
            problems.append(f"{prefix}: skipped")
        elif c.find("failure") is None:
            problems.append(f"{prefix}: PASSED against the mutant - the test does not prove the lock")
        elif SENTINEL not in (c.find("failure").get("message") or "") + (c.find("failure").text or ""):
            problems.append(f"{prefix}: failed, but not via the lock-waiter sentinel")
    if problems:
        sys.exit("MUTATION FAIL:\n  " + "\n  ".join(problems))
    print("MUTATION OK: T10a and T10b both fail when FOR SHARE is removed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "mutant.xml")
