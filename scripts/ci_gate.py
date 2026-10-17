"""CI evidence gate. Exit non-zero unless the suite provably ran and the required cases passed.

Usage: python scripts/ci_gate.py junit.xml
Closes: missing/unparseable junit.xml, missing required cases, failed/errored/skipped required cases,
and any skip other than the by-design T23b.
"""
import sys
import xml.etree.ElementTree as ET

ALLOWED_SKIPS = ("test_T23b",)
EXACTLY_ONE = ("test_T15", "test_clean_db_up_down_up")
AT_LEAST_ONE = ("test_db_one_migration_behind",)


def main(path: str) -> None:
    try:
        root = ET.parse(path).getroot()
    except (FileNotFoundError, ET.ParseError) as e:
        sys.exit(f"GATE FAIL: {path} missing or unreadable ({e}); pytest did not run to completion")

    cases = list(root.iter("testcase"))
    if not cases:
        sys.exit("GATE FAIL: no testcases in junit.xml")

    def status(c):
        for child in ("failure", "error", "skipped"):
            if c.find(child) is not None:
                return child
        return "passed"

    problems = []
    for prefix in EXACTLY_ONE:
        found = [c for c in cases if c.get("name", "").startswith(prefix)]
        if len(found) != 1:
            problems.append(f"{prefix}: expected exactly 1 case, found {len(found)}")
        elif status(found[0]) != "passed":
            problems.append(f"{prefix}: {status(found[0])}")
    for prefix in AT_LEAST_ONE:
        found = [c for c in cases if c.get("name", "").startswith(prefix)]
        if not found:
            problems.append(f"{prefix}: no cases found")
        problems += [f"{c.get('name')}: {status(c)}" for c in found if status(c) != "passed"]

    for c in cases:
        name, st = c.get("name", ""), status(c)
        if st in ("failure", "error"):
            problems.append(f"{name}: {st}")
        elif st == "skipped" and not name.startswith(ALLOWED_SKIPS):
            problems.append(f"{name}: skipped (only {ALLOWED_SKIPS} may skip)")

    if problems:
        sys.exit("GATE FAIL:\n  " + "\n  ".join(sorted(set(problems))))
    print(f"GATE OK: {len(cases)} cases, required cases passed, no unexpected skips")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "junit.xml")
