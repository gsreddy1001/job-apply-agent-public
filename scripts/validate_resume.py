"""Validate a tailored resume against the base resume before it is saved or logged.

Shared by scripts/tailor_job.py and the .claude/hooks/resume_hook.py hook.
Checks: required sections present, no placeholder text left over, nothing
fabricated that a script can detect (new role headings, new metrics, new certs),
no overstating words the base resume never uses, and short enough for two pages.

Usage:
    python scripts/validate_resume.py path/to/tailored.md
Exits 1 and prints the problems if validation fails.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_RESUME = ROOT / "resume" / "base_resume.md"

REQUIRED_SECTIONS = ["Summary", "Skills", "Experience"]
PLACEHOLDER = re.compile(
    r"\[[^\]]*\]|\{\{|\}\}|\bTODO\b|\bTBD\b|\bXXX+\b|lorem ipsum|<insert|placeholder",
    re.IGNORECASE,
)
# Metrics such as 98%, $6M, 12+, 40+. Any metric in the output must exist in the base.
METRIC = re.compile(r"\$\d[\d,.]*\s*[KMB]?|\d[\d,.]*\s*%|\d+\+")
# Two pages of this layout hold roughly 700 words.
MAX_WORDS = 700
# Self-promotional qualifiers that overstate the base resume. Flagged only when the
# base resume itself never uses them.
INFLATION_TERMS = ["expert", "hands-on", "remote", "seasoned", "extensive", "world-class",
                   "deep expertise", "mastery", "thought leader"]


def _sections(md: str) -> set[str]:
    return {m.group(1).strip().lower() for m in re.finditer(r"^##\s+(.+)$", md, re.MULTILINE)}


def _role_headings(md: str) -> set[str]:
    """Compare headings by their words only, so tidying punctuation and spacing
    ("Coordinator/ Acme" -> "Coordinator — Acme") is allowed but new words are not."""
    return {re.sub(r"[^a-z0-9]+", " ", m.group(1).lower()).strip()
            for m in re.finditer(r"^###\s+(.+)$", md, re.MULTILINE)}


def _metrics(md: str) -> set[str]:
    return {re.sub(r"\s+", "", m.group(0)).upper() for m in METRIC.finditer(md)}


def _certifications(md: str) -> str:
    match = re.search(r"^##\s+Certifications\s*$(.*?)(?=^##\s|\Z)", md, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def validate(tailored: str, base: str | None = None) -> list[str]:
    """Return a list of problems; empty means the resume passes."""
    base = base if base is not None else BASE_RESUME.read_text(encoding="utf-8")
    problems = []

    sections = _sections(tailored)
    for name in REQUIRED_SECTIONS:  # "Professional Summary" satisfies "Summary".
        if not any(name.lower() in s for s in sections):
            problems.append(f"Missing required section: {name}")

    for m in PLACEHOLDER.finditer(tailored):
        problems.append(f"Placeholder text left over: {m.group(0)!r}")

    new_roles = _role_headings(tailored) - _role_headings(base)
    for role in sorted(new_roles):
        problems.append(f"Role heading not in base resume (employer/title changed?): {role!r}")

    new_metrics = _metrics(tailored) - _metrics(base)
    for metric in sorted(new_metrics):
        problems.append(f"Metric not in base resume (possible fabrication): {metric!r}")

    for term in INFLATION_TERMS:
        pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        if pattern.search(tailored) and not pattern.search(base):
            problems.append(f"Overstating word not in base resume: {term!r}")

    words = len(tailored.split())
    if words > MAX_WORDS:
        problems.append(f"Too long for two pages: {words} words (max {MAX_WORDS})")

    certs = _certifications(tailored)
    base_certs = _certifications(base)
    if certs and certs != base_certs:
        problems.append("Certifications section differs from the base resume")

    return problems


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    problems = validate(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for p in problems:
        print(f"FAIL: {p}")
    if problems:
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
