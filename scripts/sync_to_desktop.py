"""Copy the cloud run's resumes from Google Drive to this PC's Desktop.

The daily Modal run saves each resume in Drive under
"Job Applications/<date>/<Company> - <Job Title>/". This mirrors those PDFs to
Desktop/Job Applications/<date>/<Company> - <Job Title>/<RESUME_TITLE>.pdf
(the same layout local runs use). Files already on the Desktop are skipped, and a job
folder with only a Google Doc gets the Doc exported as PDF. Nothing in Drive is changed.

Usage:
    python scripts/sync_to_desktop.py              # last 14 days of run folders
    python scripts/sync_to_desktop.py --days 60
Runs automatically from the Windows scheduled task "SD Job Agent - Desktop Sync".
"""

import argparse
import json
import os
import re
import shutil
import time
from datetime import date, timedelta

from format_resume_doc import (DOC_MIME, FOLDER_MIME, ROOT_FOLDER, STAGING_DIR, WINDOWS_SAFE_BAR,
                               desktop_dir, ensure_folder, gws)

RESUME_TITLE = os.environ.get("RESUME_TITLE", "Jane Doe | Resume")


def _list(query: str) -> list[dict]:
    return gws("drive", "files", "list", "--params", json.dumps({
        "q": f"{query} and trashed = false", "fields": "files(id,name,mimeType)",
        "pageSize": 500})).get("files", [])


def _download(file: dict, target) -> None:
    if file["mimeType"] == DOC_MIME:
        gws("drive", "files", "export", "--params", json.dumps(
            {"fileId": file["id"], "mimeType": "application/pdf"}), "--output", "sync.pdf")
    else:
        gws("drive", "files", "get", "--params", json.dumps(
            {"fileId": file["id"], "alt": "media"}), "--output", "sync.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(STAGING_DIR / "sync.pdf", target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=14, help="How many recent run dates to sync")
    parser.add_argument("--log", help="Append output to this file (the scheduled task runs "
                                      "windowless via pythonw, which has no console)")
    args = parser.parse_args()
    if args.log:
        import sys
        from datetime import datetime
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
        print(f"==== {datetime.now():%Y-%m-%d %H:%M} ====")
    cutoff = (date.today() - timedelta(days=args.days)).isoformat()

    # The task often fires right after the laptop wakes or logs on, before Wi-Fi is back;
    # wait for Google to be reachable instead of failing (2026-10-23 sync was lost this way).
    for attempt in range(10):
        try:
            root = ensure_folder(ROOT_FOLDER)
            break
        except RuntimeError as exc:
            if attempt == 9:
                raise
            print(f"  Drive not reachable yet (attempt {attempt + 1}/10): {str(exc)[:120]}")
            time.sleep(20)
    local_root = desktop_dir() / ROOT_FOLDER
    copied = skipped = 0
    days = [f for f in _list(f"'{root}' in parents and mimeType = '{FOLDER_MIME}'")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["name"]) and f["name"] >= cutoff]
    for day in sorted(days, key=lambda f: f["name"]):
        for job in _list(f"'{day['id']}' in parents and mimeType = '{FOLDER_MIME}'"):
            files = _list(f"'{job['id']}' in parents")
            source = (next((f for f in files if f["name"] == f"{RESUME_TITLE}.pdf"), None)
                      or next((f for f in files if f["mimeType"] == DOC_MIME), None))
            if source is None:
                continue  # Empty folder (e.g. an upload that failed and was retried later).
            target = (local_root / day["name"] / job["name"]
                      / f"{RESUME_TITLE.replace('|', WINDOWS_SAFE_BAR)}.pdf")
            if target.exists():
                skipped += 1
                continue
            _download(source, target)
            copied += 1
            print(f"  COPIED   {day['name']} / {job['name']}")
    print(f"{copied} resumes copied, {skipped} already on Desktop -> {local_root}")


if __name__ == "__main__":
    main()
