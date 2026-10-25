"""Tailor the base resume to each qualified job and save it in a per-job folder.

For each job in output/qualified_jobs.json:
  1. One OpenRouter call rewrites the base resume for the job (reorder, re-emphasize,
     reword only; never invent experience, employers, tools, certifications, or metrics).
  2. scripts/validate_resume.py checks it. On failure we retry once with the problems
     fed back; if it still fails the job is flagged and skipped.
  3. scripts/format_resume_doc.py turns it into a Google Doc named
     os.environ.get("RESUME_TITLE", "Jane Doe | Resume") and exports a PDF into
     Desktop/Job Applications/<run date>/<Company> - <Job Title>/.

Usage:
    python scripts/tailor_job.py
    python scripts/tailor_job.py --no-docs        # markdown only, skip Google Docs/PDF
Writes output/tailored/<folder>.md and output/tailored_jobs.json.
"""

import argparse
import json
import os
import re
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from openrouter import chat, default_model, free_requests_remaining
from seen_jobs import load_pending, save_pending
from validate_resume import validate

ROOT = Path(__file__).resolve().parent.parent
RESUME_FILE = ROOT / "resume" / "base_resume.md"
QUALIFIED_FILE = ROOT / "output" / "qualified_jobs.json"
TAILORED_DIR = ROOT / "output" / "tailored"
RESULTS_FILE = ROOT / "output" / "tailored_jobs.json"

RESUME_TITLE = os.environ.get("RESUME_TITLE", "Jane Doe | Resume")
MAX_DESCRIPTION_CHARS = 12000

SYSTEM_PROMPT = """You tailor a Project Manager's resume to one job posting, producing a clean,
ATS-friendly resume. You may only reorder, re-emphasize, trim, and reword what is already in the
base resume.

Hard rules (breaking any of these makes the resume unusable):
- Never invent experience, employers, job titles, dates, tools, certifications, degrees, or metrics.
  Every number in your output must appear in the base resume, attached to the same claim.
- Requirements listed under "missing" are gaps. Do not claim them, not even partially.
- Keep every job title, employer, and date from the base resume. Write each role heading as
  "### <Title> — <Company>" (tidy spacing/slashes only; same words), and its dates on the next line
  as "Mon YYYY – Mon YYYY" (capitalize month names; keep "Present").
- Keep the name, email, phone, LinkedIn, Education, and Certifications facts unchanged. Do not add a
  location or any contact detail that is not in the base resume.
- No placeholder text, brackets, tables, emojis, or notes to the reader.
- Do not inflate. Never use words like "expert", "hands-on", "seasoned", "extensive", or "remote"
  unless the base resume uses them. A tool listed under Skills is only "experience with" it, never
  deep or hands-on expertise. Do not add qualifiers (global, large-scale, senior) to a claim that
  the base resume states without them.

What to do:
- Headline (line under the name): "Technical Project Manager | PMP".
- Professional Summary: 3-4 sentences leading with the experience this job cares about most,
  using the posting's own terms where the base resume genuinely supports them.
- Skills: ONE section (merge the base resume's Core Competencies and Skills; no duplicates).
  4-6 lines formatted "- <Category>: item, item, ...". Put the job's keywords first, using the
  posting's exact wording when it names the same thing the resume shows (e.g. "Project Financial
  Management" for budget tracking). Drop items irrelevant to this job. Never add a skill, tool, or
  methodology the base resume does not show.
- Professional Experience: start every bullet with a strong past-tense action verb ("Present" role
  too), one line or two at most, ordered by relevance to this job and reworded to mirror the
  posting's language. Keep every metric attached to its original claim.
- Never repeat a bullet across roles. If two roles in the base resume share identical bullets,
  give the most recent one its 6-7 most relevant bullets and the other 4 *different* ones.
- Keep the whole resume to at most two pages: about 650 words, never more than 700. Give older
  roles at most 4-5 bullets, keeping the most relevant ones.

Output exactly this Markdown structure, nothing else (no preamble, no code fence):
# <Name>
Technical Project Manager | PMP
<phone> | <email> | <LinkedIn URL>

## Professional Summary
<paragraph>

## Skills
- <Category>: <items>

## Professional Experience
### <Title> — <Company>
<Mon YYYY – Mon YYYY>
- <bullet>

## Education
- <Degree> | <School> | <Year>

## Certifications
- <certifications line from the base resume>"""


def build_user_prompt(resume: str, job: dict) -> str:
    description = (job["description"] or "")[:MAX_DESCRIPTION_CHARS]
    return (
        f"<base_resume>\n{resume}\n</base_resume>\n\n"
        f"<job>\nTitle: {job['title']}\nCompany: {job['company']}\n\n{description}\n</job>\n\n"
        f"<matched>\n{json.dumps(job.get('matched') or [], indent=1)}\n</matched>\n"
        f"<missing>\n{json.dumps(job.get('missing') or [], indent=1)}\n</missing>"
    )


def strip_fence(text: str) -> str:
    """Return just the resume: drop a code fence, and any reasoning some models
    (e.g. free Nemotron) write before it, by starting at the first '# ' heading."""
    text = text.strip()
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    text = match.group(1) if match else text
    heading = re.search(r"^# ", text, re.MULTILINE)
    return (text[heading.start():] if heading else text).strip() + "\n"


def folder_name(job: dict) -> str:
    """'<Company> - <Job Title>', safe for Windows and Google Drive."""
    name = f"{job['company']} - {job['title']}"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120]


def tailor(model: str, resume: str, job: dict) -> tuple[str, list[str]]:
    """Return (markdown, problems). Retries once with the validator's problems fed back."""
    user = build_user_prompt(resume, job)
    tailored = strip_fence(chat(SYSTEM_PROMPT, user, model=model, max_tokens=16000))
    problems = validate(tailored, resume)
    if problems:
        retry = (f"{user}\n\nYour previous draft failed validation:\n"
                 + "\n".join(f"- {p}" for p in problems)
                 + "\n\nRewrite the full resume, fixing every problem.")
        tailored = strip_fence(chat(SYSTEM_PROMPT, retry, model=model, max_tokens=16000))
        problems = validate(tailored, resume)
    return tailored, problems


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=default_model())
    parser.add_argument("--run-date", default=date.today().isoformat(),
                        help="Name of the dated run folder (default: today)")
    parser.add_argument("--no-docs", action="store_true",
                        help="Only write markdown; skip the Google Doc and PDF export")
    args = parser.parse_args()

    resume = RESUME_FILE.read_text(encoding="utf-8")
    # Jobs deferred on an earlier run go first, then today's qualified jobs.
    jobs, links = [], set()
    for job in load_pending() + json.loads(QUALIFIED_FILE.read_text(encoding="utf-8")):
        if job["link"] not in links:
            links.add(job["link"])
            jobs.append(job)
    TAILORED_DIR.mkdir(parents=True, exist_ok=True)

    if not args.no_docs:
        from format_resume_doc import publish_resume  # Needs gws; imported only when used.

    # Queue everything up front and remove each job only once it's finished, so a crash
    # mid-run can never lose a qualified job (it is retried on the next run).
    save_pending(jobs)
    unfinished = {j["link"] for j in jobs}

    def finish(job: dict) -> None:
        unfinished.discard(job["link"])
        save_pending([j for j in jobs if j["link"] in unfinished])

    results, pending = [], []
    for job in jobs:
        name = folder_name(job)
        run_date = job.get("run_date") or args.run_date
        remaining = free_requests_remaining(args.model)
        if remaining is not None and remaining < 2:
            pending.append(job)
            results.append({**job, "folder": name, "status": "deferred"})
            print(f"  DEFERRED {name} (free LLM quota used up; tailored on the next run)")
            continue
        try:
            tailored, problems = tailor(args.model, resume, job)
        except Exception as exc:  # LLM outage or quota: keep the job for the next run.
            pending.append(job)
            results.append({**job, "folder": name, "status": "deferred"})
            print(f"  DEFERRED {name}: {str(exc)[:160]}")
            continue
        md_path = TAILORED_DIR / f"{name}.md"
        md_path.write_text(tailored, encoding="utf-8")
        result = {**job, "folder": name, "run_date": run_date,
                  "markdown_path": str(md_path), "problems": problems}

        if problems:
            result["status"] = "validation_failed"
            print(f"  FLAGGED  {name}")
            for p in problems:
                print(f"           - {p}")
        elif args.no_docs:
            result["status"] = "tailored"
            print(f"  OK       {name} -> {md_path.relative_to(ROOT)}")
        else:
            try:
                result.update(publish_resume(tailored, RESUME_TITLE, run_date, name))
            except Exception as exc:  # Drive/Docs hiccup: keep the job for the next run.
                pending.append(job)
                results.append({**result, "status": "deferred"})
                print(f"  DEFERRED {name}: publishing failed: {str(exc)[:160]}")
                continue
            result["status"] = "ready"
            print(f"  OK       {name} -> {result['pdf_path']}")
        results.append(result)
        finish(job)

    save_pending(pending)
    RESULTS_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    count = {s: sum(r["status"] == s for r in results)
             for s in ("ready", "tailored", "validation_failed", "deferred")}
    print(f"{len(jobs)} to tailor: {count['ready'] + count['tailored']} tailored, "
          f"{count['validation_failed']} flagged, {count['deferred']} deferred to next run"
          f" -> {RESULTS_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
