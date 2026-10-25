"""ADR and cutover-checklist consistency (S1-12). Reads files only. Names are AD*."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADR = ROOT / "infra" / "adr"
FILES = sorted(p for p in ADR.glob("ADR-*.md"))
README = (ADR / "README.md").read_text()
CHECKLIST = (ROOT / "infra" / "cutover-checklist.md").read_text()


def status_of(path):
    return re.search(r"^Status: (\w+)$", path.read_text(), re.M).group(1)


def test_AD1_adrs_are_numbered_without_gaps_and_have_the_required_sections():
    numbers = [int(re.match(r"ADR-(\d\d)-", p.name).group(1)) for p in FILES]
    assert numbers == list(range(1, len(FILES) + 1)) and len(FILES) >= 13
    for p in FILES:
        text = p.read_text()
        assert text.startswith(f"# ADR-{int(p.name[4:6]):02d}: "), p.name
        assert re.search(r"^Status: (accepted|proposed)$", text, re.M), p.name
        for heading in ("## Context", "## Decision", "## Consequences", "## Revisit when"):
            assert heading in text, f"{p.name} lacks {heading}"


def test_AD2_the_index_lists_every_adr_with_its_real_status():
    for p in FILES:
        row = next((l for l in README.splitlines() if f"({p.name})" in l), None)
        assert row, f"{p.name} is not in the index"
        assert f"| {status_of(p)} |" in row, f"{p.name}: index status differs from the file"


def test_AD3_every_pending_inventory_row_is_on_the_cutover_checklist_as_a_blocker():
    adr10 = next(p for p in FILES if p.name.startswith("ADR-10")).read_text()
    pending = [l for l in adr10.splitlines() if l.startswith("|") and "pending inventory" in l]
    assert len(pending) >= 4
    for line in pending:
        name = line.split("|")[1].strip()                         # e.g. "Retool review UI"
        key = name.split()[0].lower()                             # "retool", "llm", "email", "otel"
        rows = [l for l in CHECKLIST.splitlines() if key in l.lower() and l.startswith("| C")]
        assert rows, f"{name} is pending inventory but not on the checklist"
        assert all("blocks production cutover" in r for r in rows), f"{name} must block production cutover"


def test_AD4_cross_references_point_at_adrs_that_exist():
    highest = len(FILES)
    for p in FILES + [ADR / "README.md", ROOT / "infra" / "cutover-checklist.md"]:
        for n in re.findall(r"ADR-(\d\d)\b", p.read_text()):
            assert 1 <= int(n) <= highest, f"{p.name} cites ADR-{n}, which does not exist"


def test_AD5_checklist_rows_have_a_status_and_the_file_holds_no_credentials():
    rows = [l for l in CHECKLIST.splitlines() if l.startswith("| C")]
    assert len(rows) >= 10
    for r in rows:
        assert re.search(r"\| (Open|Unconfirmed|Done) \|", r), r
    for p in FILES + [ADR / "README.md"]:
        assert not re.search(r"(password|secret|token)\s*[:=]\s*\S", p.read_text(), re.I), p.name
