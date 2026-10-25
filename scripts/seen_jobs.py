"""Remember which jobs were already scored, so later runs don't spend LLM calls on them.

Stored in state/seen_jobs.json as {link-or-title+company key: "YYYY-MM-DD"}. On Modal the
state/ folder is a persistent Modal Volume; locally it is a plain folder (gitignored).
Entries older than KEEP_DAYS are pruned.
"""

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = Path(os.environ.get("STATE_DIR", ROOT / "state")) / "seen_jobs.json"
KEEP_DAYS = 45


def _key(job: dict) -> str:
    return re.sub(r"\W+", " ", f"{job['title']} {job['company']}").strip().lower()


def load_seen() -> dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def mark_seen(jobs: list[dict]) -> None:
    seen = load_seen()
    today = date.today().isoformat()
    for job in jobs:
        seen[job["link"]] = today
        seen[_key(job)] = today
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(seen, indent=1, ensure_ascii=False), encoding="utf-8")


PENDING_FILE = STATE_FILE.parent / "pending_jobs.json"


def load_pending() -> list[dict]:
    """Qualified jobs whose tailoring couldn't run yet (e.g. LLM quota); tailored first next run."""
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_pending(jobs: list[dict]) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(jobs, indent=1, ensure_ascii=False), encoding="utf-8")
