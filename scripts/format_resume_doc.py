"""Turn a tailored markdown resume into a formatted Google Doc and a PDF on the Desktop.

Uses the Google Workspace CLI (`gws`, npm install -g @googleworkspace/cli):
  1. markdown -> ATS-friendly .docx (scripts/resume_docx.py)
  2. Upload the .docx to Drive, converted to a Google Doc, inside Drive folder
     "Job Applications/<run date>/<Company> - <Job Title>/". A previous Doc with the
     same title in that folder is moved to trash first, so re-runs replace it.
  3. Export the Doc as PDF into the same folder path on the Desktop, or with
     PDF_DESTINATION=drive (Modal) upload it next to the Doc in Drive.

Usage (normally called from tailor_job.py):
    python scripts/format_resume_doc.py output/tailored/<folder>.md --folder "<Company> - <Job Title>"
"""

import argparse
import json
import os
import shutil
import re
import subprocess
import time
from datetime import date
from pathlib import Path

from resume_docx import build_docx

ROOT_FOLDER = "Job Applications"
GWS_RETRY_WAITS = [5, 20, 60]  # Seconds between attempts on transient Google errors.
TRANSIENT_GWS_ERROR = re.compile(r"internal error|backend ?error|rate ?limit|quota exceeded|\b(429|500|502|503|504)\b|timed? ?out|unavailable", re.I)
STAGING_DIR = Path(__file__).resolve().parent.parent / "output" / "staging"
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Windows forbids '|' in file names, so the local PDF uses a look-alike bar (U+FF5C).
WINDOWS_SAFE_BAR = "｜"

def desktop_dir() -> Path:
    if os.environ.get("DESKTOP_DIR"):
        return Path(os.environ["DESKTOP_DIR"])
    onedrive = Path.home() / "OneDrive" / "Desktop"
    return onedrive if onedrive.is_dir() else Path.home() / "Desktop"


def gws_executable() -> str:
    """Prefer the native binary: on Windows the npm `gws.cmd` shim runs through cmd.exe,
    which mangles JSON arguments containing quotes or '|' (the resume title has one)."""
    launcher = shutil.which("gws")
    npm_default = Path(os.environ.get("APPDATA", "")) / "npm" / "gws.cmd"
    if not launcher and npm_default.is_file():  # Fresh install, PATH not reloaded yet.
        launcher = str(npm_default)
    if not launcher:
        raise RuntimeError("gws not found. Install it: npm install -g @googleworkspace/cli")
    native = Path(launcher).parent / "node_modules" / "@googleworkspace" / "cli" / "bin" / "gws.exe"
    return str(native) if native.is_file() else launcher


def gws(*args: str) -> dict:
    # gws refuses --upload/--output paths outside its working directory, so it always
    # runs from STAGING_DIR and file arguments are passed relative to it.
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    for wait in GWS_RETRY_WAITS + [None]:
        proc = subprocess.run([gws_executable(), *args], capture_output=True, text=True,
                              encoding="utf-8", cwd=STAGING_DIR)
        if proc.returncode == 0:
            return json.loads(proc.stdout) if proc.stdout.strip() else {}
        error = proc.stderr.strip() or proc.stdout.strip()
        # Google APIs return occasional transient "Internal Error"/503/rate-limit failures
        # (one hit a 2026-10-20 cloud run); retry those, fail fast on anything else.
        if wait is None or not TRANSIENT_GWS_ERROR.search(error):
            raise RuntimeError(f"gws {' '.join(args[:3])} failed: {error}")
        time.sleep(wait)
    raise AssertionError("unreachable")


def ensure_folder(name: str, parent: str = "root") -> str:
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (f"name = '{escaped}' and mimeType = '{FOLDER_MIME}' "
             f"and '{parent}' in parents and trashed = false")
    found = gws("drive", "files", "list",
                "--params", json.dumps({"q": query, "fields": "files(id)"})).get("files", [])
    if found:
        return found[0]["id"]
    created = gws("drive", "files", "create",
                  "--json", json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}))
    return created["id"]


def publish_resume(markdown_text: str, title: str, run_date: str, folder: str) -> dict:
    """Create the resume Google Doc and export its PDF. Returns doc_id, doc_link, pdf_path."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)  # Fresh Modal containers don't have it.
    build_docx(markdown_text, STAGING_DIR / "upload.docx")
    return publish_docx(title, run_date, folder)


def publish_docx(title: str, run_date: str, folder: str) -> dict:
    """Upload STAGING_DIR/upload.docx as Google Doc `title` in the job's Drive folder
    (replacing an older one), then export its PDF to the Desktop or Drive."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    drive_folder = ensure_folder(folder, ensure_folder(run_date, ensure_folder(ROOT_FOLDER)))

    title_q = title.replace("\\", "\\\\").replace("'", "\\'")
    previous = gws("drive", "files", "list", "--params", json.dumps({
        "q": f"(name = '{title_q}' or name = '{title_q}.pdf') "
             f"and '{drive_folder}' in parents and trashed = false",
        "fields": "files(id)"})).get("files", [])
    for old in previous:
        gws("drive", "files", "update", "--params", json.dumps({"fileId": old["id"], "fields": "id"}),
            "--json", json.dumps({"trashed": True}))

    doc = gws("drive", "files", "create",
              "--json", json.dumps({"name": title, "mimeType": DOC_MIME, "parents": [drive_folder]}),
              "--upload", "upload.docx", "--upload-content-type", DOCX_MIME,
              "--params", json.dumps({"fields": "id,webViewLink"}))
    gws("drive", "files", "export",
        "--params", json.dumps({"fileId": doc["id"], "mimeType": "application/pdf"}),
        "--output", "upload.pdf")

    if os.environ.get("PDF_DESTINATION", "desktop") == "drive":
        # Modal has no Desktop: put the PDF next to the Doc in Drive. With Google Drive
        # for desktop installed, that folder syncs to the PC anyway.
        pdf = gws("drive", "files", "create",
                  "--json", json.dumps({"name": f"{title}.pdf", "parents": [drive_folder]}),
                  "--upload", "upload.pdf", "--upload-content-type", "application/pdf",
                  "--params", json.dumps({"fields": "id,webViewLink"}))
        pdf_path = pdf.get("webViewLink")
    else:
        local_folder = desktop_dir() / ROOT_FOLDER / run_date / folder
        local_folder.mkdir(parents=True, exist_ok=True)
        pdf_path = local_folder / f"{title.replace('|', WINDOWS_SAFE_BAR)}.pdf"
        shutil.move(STAGING_DIR / "upload.pdf", pdf_path)

    return {"doc_id": doc["id"], "doc_link": doc.get("webViewLink"), "pdf_path": str(pdf_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("markdown_file")
    parser.add_argument("--folder", required=True, help='"<Company> - <Job Title>"')
    parser.add_argument("--title", default=os.environ.get("RESUME_TITLE", "Jane Doe | Resume"))
    parser.add_argument("--run-date", default=date.today().isoformat())
    args = parser.parse_args()
    text = Path(args.markdown_file).read_text(encoding="utf-8")
    print(json.dumps(publish_resume(text, args.title, args.run_date, args.folder), indent=2))


if __name__ == "__main__":
    main()
