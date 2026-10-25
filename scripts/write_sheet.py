"""Update the "Job Application Tracker" Google Sheet after each run.

Tabs:
  Applications         one row per job whose tailored resume is ready (status "ready"),
                       with resume links and a direct "Apply" link.
                       Status dropdown, date checks, and status colors are re-applied to all
                       data rows on every run. Rows are matched by Job Link, so re-runs never
                       touch your Status / Applied Date / Follow-up Date / Notes edits; only the
                       resume, source, and apply-link columns are refreshed.
  All Jobs             every job the scraper looked at today with its source and outcome
                       (rejected: no sponsorship / clearance / title, low score, resume ready...).
  Application Answers  standard answers for application forms, from resume/application_profile.json.

The Sheet lives in Drive folder "Job Applications" and is created on first run.

Usage:
    python scripts/write_sheet.py
Reads output/tailored_jobs.json, output/scored_jobs.json and output/scrape_log.json.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from format_resume_doc import ROOT_FOLDER, ensure_folder, gws

ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE = ROOT / "output" / "tailored_jobs.json"
SCORED_FILE = ROOT / "output" / "scored_jobs.json"
SCRAPE_LOG = ROOT / "output" / "scrape_log.json"
PROFILE_FILE = ROOT / "resume" / "application_profile.json"

SHEET_TITLE = "Job Application Tracker"
TAB = "Applications"
ALL_TAB = "All Jobs"
ANSWERS_TAB = "Application Answers"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
MIN_SCORE = 7  # Jobs scoring below this get no resume.

# New columns are only ever appended at the end, so existing rows keep their layout.
COLUMNS = [
    "Run Date", "Company", "Job Title", "Score", "Status", "Job Link",
    "Resume (Google Doc)", "Resume PDF", "Posted", "Workplace",
    "Why It Matched", "Boosts", "Key Gaps", "Applied Date", "Follow-up Date",
    "Notes", "Added At", "Source", "Apply Link",
]
COL = {name: i for i, name in enumerate(COLUMNS)}
WIDTHS = {"Company": 160, "Job Title": 260, "Job Link": 220, "Resume (Google Doc)": 140,
          "Resume PDF": 200, "Why It Matched": 380, "Key Gaps": 320, "Notes": 260,
          "Source": 90, "Apply Link": 110}
# Columns the pipeline refreshes on existing rows (everything else belongs to the candidate).
REFRESH = [("Resume (Google Doc)", "Resume PDF"), ("Source", "Apply Link")]

ALL_COLUMNS = ["Run Date", "Source", "Company", "Job Title", "Job Link", "Outcome", "Score",
               "Reason", "Logged At"]
ALL_WIDTHS = {"Company": 180, "Job Title": 280, "Job Link": 240, "Outcome": 230, "Reason": 420}

STATUSES = {  # Dropdown values and their row colors.
    "Ready to apply": (0.85, 0.92, 1.00),
    "Applied": (1.00, 0.95, 0.80),
    "Interviewing": (0.88, 0.82, 0.98),
    "Offer": (0.80, 0.94, 0.80),
    "Rejected": (0.96, 0.80, 0.80),
    "Skipped": (0.90, 0.90, 0.90),
}
NEW_STATUS = "Ready to apply"
HEADER_FORMAT = {"repeatCell": {
    "cell": {"userEnteredFormat": {
        "backgroundColor": {"red": 0.12, "green": 0.22, "blue": 0.39},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
    "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}}


def _col_letter(index: int) -> str:
    return chr(ord("A") + index)


def _text(value) -> str:
    """Plain text cell. A leading = + - @ would be parsed as a formula under USER_ENTERED."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in "=+-@" else s


def _link(url: str | None, label: str) -> str:
    url = url or ""
    return f'=HYPERLINK("{url}","{label}")' if url.startswith("http") else _text(url)


def _rgb(r: float, g: float, b: float) -> dict:
    return {"red": r, "green": g, "blue": b}


def _batch(sheet_id: str, requests: list[dict]) -> None:
    if requests:
        gws("sheets", "spreadsheets", "batchUpdate",
            "--params", json.dumps({"spreadsheetId": sheet_id}),
            "--json", json.dumps({"requests": requests}))


def _values(sheet_id: str, rng: str) -> list[list]:
    return gws("sheets", "spreadsheets", "values", "get", "--params", json.dumps(
        {"spreadsheetId": sheet_id, "range": rng})).get("values", [])


def _tabs(sheet_id: str) -> dict[str, dict]:
    sheets = gws("sheets", "spreadsheets", "get", "--params", json.dumps({
        "spreadsheetId": sheet_id,
        "fields": "sheets(properties(sheetId,title),conditionalFormats)"}))["sheets"]
    return {t["properties"]["title"]: t for t in sheets}


# ---------- sheet + tab setup ----------

def find_sheet(folder_id: str) -> str | None:
    query = (f"name = '{SHEET_TITLE}' and mimeType = '{SHEET_MIME}' "
             f"and '{folder_id}' in parents and trashed = false")
    files = gws("drive", "files", "list",
                "--params", json.dumps({"q": query, "fields": "files(id)"})).get("files", [])
    return files[0]["id"] if files else None


def create_sheet(folder_id: str) -> str:
    created = gws("sheets", "spreadsheets", "create", "--json", json.dumps({
        "properties": {"title": SHEET_TITLE},
        "sheets": [{"properties": {"title": TAB, "gridProperties": {"frozenRowCount": 1}}}],
    }))
    sheet_id = created["spreadsheetId"]
    # Move it from My Drive root into the Job Applications folder.
    gws("drive", "files", "update", "--params", json.dumps(
        {"fileId": sheet_id, "addParents": folder_id, "removeParents": "root", "fields": "id"}))
    return sheet_id


def ensure_tab(sheet_id: str, tabs: dict, title: str, header: list[str],
               widths: dict[str, int]) -> int:
    """Create the tab if missing and keep its header in sync. Widths and the filter are set
    when the tab is created or new columns appear, so manual resizing is preserved."""
    if title not in tabs:
        reply = gws("sheets", "spreadsheets", "batchUpdate",
                    "--params", json.dumps({"spreadsheetId": sheet_id}),
                    "--json", json.dumps({"requests": [{"addSheet": {"properties": {
                        "title": title, "gridProperties": {"frozenRowCount": 1}}}}]}))
        tab_id = reply["replies"][0]["addSheet"]["properties"]["sheetId"]
    else:
        tab_id = tabs[title]["properties"]["sheetId"]

    current = (_values(sheet_id, f"'{title}'!1:1") or [[]])[0]
    if current == header:
        return tab_id
    gws("sheets", "spreadsheets", "values", "update",
        "--params", json.dumps({"spreadsheetId": sheet_id, "range": f"'{title}'!A1",
                                "valueInputOption": "RAW"}),
        "--json", json.dumps({"values": [header]}))
    index = {name: i for i, name in enumerate(header)}
    requests = [{"repeatCell": {**HEADER_FORMAT["repeatCell"],
                                "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1}}},
                {"setBasicFilter": {"filter": {"range": {
                    "sheetId": tab_id, "startRowIndex": 0, "startColumnIndex": 0,
                    "endColumnIndex": len(header)}}}}]
    for name, width in widths.items():
        if index[name] >= len(current):  # Only columns that are new to this tab.
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                          "startIndex": index[name], "endIndex": index[name] + 1},
                "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    _batch(sheet_id, requests)
    return tab_id


def apply_row_rules(sheet_id: str, tab: dict) -> None:
    """(Re)apply the Status dropdown, date validation, and status colors to every data
    row of Applications. Runs on each write, so rows inserted or sorted by hand never lose them."""
    tab_id = tab["properties"]["sheetId"]
    status_col, last_col = COL["Status"], len(COLUMNS)
    # Drop old color rules (highest index first) and re-add them anchored at row 2.
    requests = [{"deleteConditionalFormatRule": {"sheetId": tab_id, "index": i}}
                for i in reversed(range(len(tab.get("conditionalFormats", []))))]
    requests.append({"setDataValidation": {
        "range": {"sheetId": tab_id, "startRowIndex": 1,
                  "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST",
                               "values": [{"userEnteredValue": s} for s in STATUSES]},
                 "strict": True, "showCustomUi": True}}})
    for name in ("Applied Date", "Follow-up Date"):
        requests.append({"setDataValidation": {
            "range": {"sheetId": tab_id, "startRowIndex": 1,
                      "startColumnIndex": COL[name], "endColumnIndex": COL[name] + 1},
            "rule": {"condition": {"type": "DATE_IS_VALID"}, "strict": True}}})
    status_letter = _col_letter(status_col)
    for status, color in STATUSES.items():
        requests.append({"addConditionalFormatRule": {"rule": {
            "ranges": [{"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": 0,
                        "endColumnIndex": last_col}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": f'=${status_letter}2="{status}"'}]},
                "format": {"backgroundColor": _rgb(*color)}}}}})
    _batch(sheet_id, requests)


# ---------- Applications tab ----------

def build_row(job: dict, added_at: str) -> list:
    row = [""] * len(COLUMNS)
    row[COL["Run Date"]] = job.get("run_date") or added_at[:10]
    row[COL["Company"]] = _text(job["company"])
    row[COL["Job Title"]] = _text(job["title"])
    row[COL["Score"]] = job["score"]
    row[COL["Status"]] = NEW_STATUS
    row[COL["Job Link"]] = job["link"]
    row[COL["Resume (Google Doc)"]] = _link(job.get("doc_link"), "Open resume")
    row[COL["Resume PDF"]] = _link(job.get("pdf_path"), "Open PDF")
    row[COL["Posted"]] = _text(job.get("posted_date"))
    row[COL["Workplace"]] = _text(job.get("workplace_type") or "Not stated")
    row[COL["Why It Matched"]] = _text(job.get("reason"))
    row[COL["Boosts"]] = _text(", ".join(b.replace("_", " ") for b in job.get("boosts") or []))
    row[COL["Key Gaps"]] = _text("; ".join((job.get("missing") or [])[:3]))
    row[COL["Added At"]] = added_at
    row[COL["Source"]] = _text(job.get("source"))
    row[COL["Apply Link"]] = _link(job.get("apply_link") or job["link"], "Apply")
    return row


def existing_rows(sheet_id: str, tab: str, link_col: int) -> dict[str, int]:
    """Job Link -> sheet row number."""
    letter = _col_letter(link_col)
    rows = _values(sheet_id, f"'{tab}'!{letter}2:{letter}")
    return {r[0]: i + 2 for i, r in enumerate(rows) if r}


def refresh_pipeline_columns(sheet_id: str, jobs: list[dict], rows: dict[str, int]) -> int:
    """For jobs already in the sheet, refresh only the resume, source, and apply-link columns
    (a re-tailored resume gets a new Doc). The candidate's own columns are untouched."""
    data = []
    for j in jobs:
        values = build_row(j, "")
        for first, last in REFRESH:
            a, b = COL[first], COL[last]
            data.append({"range": f"{TAB}!{_col_letter(a)}{rows[j['link']]}:{_col_letter(b)}{rows[j['link']]}",
                         "values": [values[a:b + 1]]})
    if data:
        gws("sheets", "spreadsheets", "values", "batchUpdate",
            "--params", json.dumps({"spreadsheetId": sheet_id}),
            "--json", json.dumps({"valueInputOption": "USER_ENTERED", "data": data}))
    return len(jobs)


def append_rows(sheet_id: str, tab: str, rows: list[list]) -> None:
    if rows:
        gws("sheets", "spreadsheets", "values", "append",
            "--params", json.dumps({"spreadsheetId": sheet_id, "range": f"'{tab}'!A1",
                                    "valueInputOption": "USER_ENTERED",
                                    "insertDataOption": "OVERWRITE"}),
            "--json", json.dumps({"values": rows}))


# ---------- All Jobs tab ----------

def _load(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def all_jobs_rows(logged_at: str) -> list[list]:
    """One row per job the scraper considered today, with its final outcome."""
    scored = {j["link"]: j for j in _load(SCORED_FILE)}
    tailored = {j["link"]: j for j in _load(RESULTS_FILE)}
    rows = []
    for entry in _load(SCRAPE_LOG):
        outcome, score, reason = entry["outcome"], "", ""
        if outcome == "Sent to scoring":
            job = scored.get(entry["link"])
            if job is None:
                outcome = "Scoring error (retried next run)"
            else:
                score, reason = job["score"], job.get("reason", "")
                tailored_job = tailored.get(entry["link"], {})
                if job.get("dealbreakers"):
                    outcome = "Rejected: " + ", ".join(d.replace("_", " ") for d in job["dealbreakers"])
                elif job["score"] < MIN_SCORE:
                    outcome = f"Rejected: score {job['score']} (below {MIN_SCORE})"
                elif tailored_job.get("status") == "ready":
                    outcome = "Resume ready (see Applications tab)"
                elif tailored_job.get("status") == "validation_failed":
                    outcome = "Qualified, but resume flagged by validation"
                elif tailored_job.get("status") == "deferred":
                    outcome = "Qualified, resume deferred to next run (LLM quota)"
                else:
                    outcome = "Qualified"
        rows.append([entry.get("run_date") or logged_at[:10], _text(entry.get("source")),
                     _text(entry.get("company")), _text(entry.get("title")), entry.get("link"),
                     _text(outcome), score, _text(reason), logged_at])
    return rows


# ---------- main ----------

def main() -> None:
    load_dotenv(ROOT / ".env")
    jobs = _load(RESULTS_FILE)
    ready = [j for j in jobs if j.get("status") == "ready"]

    folder_id = ensure_folder(ROOT_FOLDER)
    sheet_id = os.environ.get("SHEET_ID") or find_sheet(folder_id) or create_sheet(folder_id)
    tabs = _tabs(sheet_id)
    ensure_tab(sheet_id, tabs, TAB, COLUMNS, WIDTHS)
    ensure_tab(sheet_id, tabs, ALL_TAB, ALL_COLUMNS, ALL_WIDTHS)
    ensure_tab(sheet_id, tabs, ANSWERS_TAB, ["Question", "Answer"], {"Question": 380, "Answer": 560})
    tabs = _tabs(sheet_id)
    apply_row_rules(sheet_id, tabs[TAB])

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Applications: add new ready jobs, refresh pipeline columns on existing ones.
    rows = existing_rows(sheet_id, TAB, COL["Job Link"])
    new = [j for j in ready if j["link"] not in rows]
    append_rows(sheet_id, TAB, [build_row(j, now) for j in new])
    for j in new:
        print(f"  ADDED    {j['company']} - {j['title']} (score {j['score']})")
    refreshed = refresh_pipeline_columns(sheet_id, [j for j in ready if j["link"] in rows], rows)

    # All Jobs: everything the scraper considered, skipping links already logged.
    logged = existing_rows(sheet_id, ALL_TAB, ALL_COLUMNS.index("Job Link"))
    all_rows = [r for r in all_jobs_rows(now) if r[4] not in logged]
    append_rows(sheet_id, ALL_TAB, all_rows)

    # Application Answers: rewritten every run from the profile file.
    answers = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))["answers"]
    gws("sheets", "spreadsheets", "values", "clear", "--params", json.dumps(
        {"spreadsheetId": sheet_id, "range": f"'{ANSWERS_TAB}'!A2:B"}))
    gws("sheets", "spreadsheets", "values", "update",
        "--params", json.dumps({"spreadsheetId": sheet_id, "range": f"'{ANSWERS_TAB}'!A2",
                                "valueInputOption": "RAW"}),
        "--json", json.dumps({"values": answers}))

    print(f"Applications: {len(new)} added, {refreshed} refreshed, "
          f"{len(jobs) - len(ready)} not ready (flagged/no resume) | "
          f"All Jobs: {len(all_rows)} logged | Application Answers: {len(answers)} rows")
    print(f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
