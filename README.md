# Job Application Agent

A daily, unattended job-search pipeline for Project Manager roles. Every morning it:

1. **Scrapes** fresh postings from four Apify actors (company career sites, Indeed, Dice, Glassdoor), giving company career portals priority over job boards.
2. **Filters** before any AI call: de-duplicates across sources, drops irrelevant titles, contract/part-time roles, stale reposts, and (optionally) postings that require a security clearance or offer no visa sponsorship.
3. **Scores** each job 1–10 against your resume with one LLM call (OpenRouter), returning matched and missing requirements. The final score is computed in code from a base score plus boosts, capped by dealbreakers.
4. **Tailors** an ATS-friendly resume for every job scoring 7 or higher. It only reorders and rewords what is already in your base resume, and a validator rejects any invented employers, metrics, certifications, or inflated wording.
5. **Publishes** each resume as a Google Doc + PDF in `Job Applications/<date>/<Company> - <Job Title>/` on Google Drive, and logs it in a Google Sheet tracker with a status dropdown and a direct Apply link.

It never submits applications. You review each resume and click Apply yourself.

## How it runs

| Piece | What it does |
|---|---|
| `scripts/scrape_jobs.py` | Apify sources, normalization, de-duplication, hard rejects, priority selection |
| `scripts/score_jobs.py` | One OpenRouter call per job; scoring rubric, boosts, dealbreakers |
| `scripts/tailor_job.py` | Tailors the resume per job and retries once on validation failure |
| `scripts/validate_resume.py` | No-fabrication and format checks |
| `scripts/resume_docx.py` | Renders the ATS .docx layout |
| `scripts/format_resume_doc.py` | Uploads to Google Drive and exports the PDF (via the `gws` CLI) |
| `scripts/write_sheet.py` | Tracker Sheet: Applications, All Jobs, and Application Answers tabs |
| `scripts/sync_to_desktop.py` | Optional: copies each day's PDFs from Drive to your Desktop |
| `modal_app.py` | Daily [Modal](https://modal.com) cron job that runs the steps above |

Free-tier friendly: with an OpenRouter `:free` model, a budget guard reserves quota for tailoring, and qualified jobs that can't be finished are queued for the next run.

## Setup

1. **Python deps:** `pip install -r requirements.txt`
2. **Keys:** copy `.env.example` to `.env` and fill in your own keys. No keys are included in this repo; `.env.example` explains where to create each one (Apify, OpenRouter, Google, Modal, and optionally Telegram).
3. **Your resume:** replace `resume/base_resume.md` (a sample for "Jane Doe") with your own, keeping the same Markdown structure, and fill in `resume/application_profile.json`.
4. **Resume file name:** set `RESUME_TITLE`, e.g. `RESUME_TITLE="Your Name | Resume"`.
5. **Google:** install the Google Workspace CLI (`npm install -g @googleworkspace/cli`), create a Desktop OAuth client in your own Google Cloud project, save it as `~/.config/gws/client_secret.json`, then run `gws auth login -s drive,docs,sheets`.
6. **Search and filters:** adjust `TITLES`, `LOOKBACK_DAYS`, `hard_reject()` in `scrape_jobs.py`, and `BOOSTS` / `DEALBREAKERS` / `MIN_SCORE` in `score_jobs.py` to match your search.

## Run locally

```
python scripts/scrape_jobs.py --limit 5      # cheap test scrape -> output/raw_jobs.json
python scripts/score_jobs.py                 # -> output/qualified_jobs.json
python scripts/tailor_job.py --no-docs       # tailored markdown only
python scripts/tailor_job.py                 # + Google Doc and PDF
python scripts/write_sheet.py                # update the tracker Sheet
```

## Deploy on Modal

Create two Modal secrets: `job-agent-env` (your `.env` values) and `job-agent-gws` (`GWS_CREDENTIALS_JSON` = the output of `gws auth export --unmasked`). Then:

```
python -m modal run modal_app.py --check     # preflight: secrets + Drive access
python -m modal deploy modal_app.py          # daily schedule (5:00 AM America/Los_Angeles)
```

## Notes

- Never commit `.env`, `output/`, or `state/`; they're already in `.gitignore`.
- Respect each job board's terms of service. This project only reads public postings and leaves applying to you.

## License

Released under the [MIT License](LICENSE).
