"""Scrape fresh technology Project Manager postings from several Apify job sources.

Sources (each normalized to the same job shape, tagged with "source"); the actor IDs come
from APIFY_ACTOR_* in .env, these are the actors the code was written for:
  linkedin   curious_coder/linkedin-jobs-scraper   (LinkedIn search URL with remote/hybrid,
                                                    full-time, last-3-days filters)
  indeed     valig/indeed-jobs-scraper
  dice       shahidirfan/Dice-Job-Scraper          (tech-focused board)
  glassdoor  bebity/glassdoor-jobs-scraper
  career     fantastic-jobs/career-site-job-listing-api  (direct postings from company career
                                                    sites: Workday, Greenhouse, Lever, Ashby...)

Pipeline inside this step, all before any LLM call:
  merge -> de-duplicate (link, then title+company) -> free title/job-type filter ->
  drop jobs already scored on earlier days (state/seen_jobs.json) -> cap at --max-jobs,
  filling slots by priority: company career sites, then Indeed jobs that apply on the
  employer's own site, then Dice/Glassdoor/other Indeed, and LinkedIn last.

Usage:
    python scripts/scrape_jobs.py                          # all sources, defaults below
    python scripts/scrape_jobs.py --sources indeed,dice --max-jobs 10
Writes output/raw_jobs.json and prints per-source counts.
"""

import argparse
import json
import os
import re
from decimal import Decimal
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from apify_client import ApifyClient
from dotenv import load_dotenv

from seen_jobs import load_seen

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = ROOT / "output" / "raw_jobs.json"
# Every relevant-or-rejected job with its outcome, for the Sheet's "All Jobs" tab.
LOG_FILE = ROOT / "output" / "scrape_log.json"

# Technology PM titles that match the base resume (PMP, Agile, cloud, IT delivery).
TITLES = ["Technical Project Manager", "IT Project Manager", "Technical Program Manager",
          "Digital Project Manager", "IT Program Manager"]
LINKEDIN_KEYWORDS = " OR ".join(f'"{t}"' for t in TITLES[:4])
# Search window: last 3 days (configurable), so each run sees more postings;
# state/seen_jobs.json stops the overlap between days from being scored twice.
LOOKBACK_DAYS = 3

# Free pre-filter on titles, so the daily LLM budget is spent only on plausible roles.
TITLE_INCLUDE = re.compile(r"\b(project|program|programme|delivery)\s+manager\b|\bPMO\b", re.I)
TITLE_EXCLUDE = re.compile(
    r"\b(intern|internship|junior|jr\.?|assistant|associate|coordinator|apprentice|"
    r"construction|civil|electrical|mechanical|facilities|facility|superintendent|"
    r"estimator|clinical|nursing|healthcare|pharma|manufacturing|plant|"
    r"director|vice president|vp|head of|chief)\b", re.I)
# Optional hard rejects from the job description (no visa sponsorship / security clearance
# required); remove them from hard_reject() if they don't apply to you. Checked in code before any LLM call; score_jobs.py keeps the same two
# dealbreakers as a backstop for wording these patterns miss.
NO_SPONSORSHIP = re.compile(
    r"\b(?:not|unable to|cannot|can't|won't|will not|do not|does not|don't|doesn't|"
    r"is not able to|are not able to)\s+(?:be able to\s+)?(?:provide\s+|offer\s+|support\s+|consider\s+)?"
    r"(?:any\s+)?(?:h-?1b\s+|work\s+|employment\s+)?(?:visa\s+)?sponsor"
    r"|\bno\s+(?:visa\s+|h-?1b\s+)?sponsorship"
    r"|sponsorship\s+(?:is\s+)?(?:not|un)\s*(?:available|provided|offered|possible)"
    r"|not\s+eligible\s+for\s+(?:visa\s+)?sponsorship"
    r"|without\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+|any\s+)?(?:visa\s+)?sponsorship"
    r"|\bu\.?s\.?\s+citizens?(?:hip)?\s+(?:is\s+)?(?:only|required)"
    r"|must\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen"
    r"|\b(?:usc|us\s+citizens?)\s*(?:/|or|and)\s*(?:gc|green\s+card)"
    r"|green\s+card\s+holders?\s+only", re.I)
CLEARANCE = re.compile(
    r"security\s+clearance|secret\s+clearance|top\s+secret|\bts\s*/\s*sci\b|\bts-sci\b"
    r"|clearance\s+(?:is\s+)?required|active\s+(?:dod\s+)?clearance|dod\s+clearance"
    r"|public\s+trust\s+clearance|polygraph", re.I)
EXCLUDED_EMPLOYMENT = re.compile(r"contract|part[\s_-]?time|temporary|intern|per[\s_]diem|volunteer", re.I)


# ---------- per-source input builders and normalizers ----------

def _text(value) -> str:
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return value or ""


def linkedin_input(limit: int) -> dict:
    url = "https://www.linkedin.com/jobs/search/?" + urlencode({
        "keywords": LINKEDIN_KEYWORDS, "location": "United States",
        "f_WT": "2,3", "f_JT": "F", "f_TPR": f"r{LOOKBACK_DAYS * 86400}"})
    return {"urls": [url], "limitPerSource": limit, "scrapeCompany": False}


def linkedin_job(i: dict) -> dict:
    link = i.get("link") or ""
    if "linkedin.com/jobs/view/" in link:  # Drop per-search tracking params.
        link = urlunsplit(urlsplit(link)._replace(query=""))
    return {"title": i.get("title"), "company": i.get("companyName"), "link": link,
            "apply_link": i.get("applyUrl") or link,  # Empty applyUrl = LinkedIn Easy Apply.
            "description": i.get("descriptionText"), "posted_date": i.get("postedAt"),
            "workplace_type": None, "employment_type": i.get("employmentType"),
            "seniority_level": i.get("seniorityLevel"), "location": _text(i.get("location"))}


def indeed_input(limit: int) -> dict:
    return {"country": "us", "title": TITLES[0], "location": "Remote", "limit": limit,
            "datePosted": str(LOOKBACK_DAYS)}


def indeed_job(i: dict) -> dict:
    loc = i.get("location") or {}
    return {"title": i.get("title"), "company": _text(i.get("employer")),
            "link": i.get("url") or i.get("jobUrl"),
            "apply_link": i.get("jobUrl") or i.get("url"),  # jobUrl = the employer's own ATS page.
            "description": _text(i.get("description")),
            "posted_date": i.get("datePublished"), "workplace_type": "Remote",
            "employment_type": _text(list((i.get("jobTypes") or {}).values())),
            "seniority_level": None,
            "location": ", ".join(filter(None, [loc.get("city"), loc.get("admin1Code")])),
            "stale": bool(i.get("isRepost") or i.get("expired"))}


def dice_input(limit: int) -> dict:
    return {"keyword": TITLES[1], "posted_date": f"{LOOKBACK_DAYS}d", "includeRemote": True,
            "results_wanted": limit, "maxPages": 1}


def dice_job(i: dict) -> dict:
    return {"title": i.get("title"), "company": i.get("companyName"),
            "link": i.get("detailsPageUrl") or i.get("url"),
            "apply_link": i.get("detailsPageUrl") or i.get("url"),
            "description": i.get("description_text"), "posted_date": i.get("postedDate"),
            "workplace_type": i.get("workSetting"), "employment_type": i.get("employmentType"),
            "seniority_level": None, "location": _text(i.get("jobLocation") or i.get("location"))}


def glassdoor_input(limit: int) -> dict:
    return {"keyword": TITLES[1], "matchTitle": "loose", "location": "United States",
            "maxItems": limit, "scrapeDetails": True, "postedWithinDays": str(LOOKBACK_DAYS),
            "country": "https://www.glassdoor.com"}


def glassdoor_job(i: dict) -> dict:
    loc = i.get("job_location") or {}
    return {"title": i.get("job_title"), "company": i.get("company_name"),
            "link": i.get("job_url"), "apply_link": i.get("job_apply_url") or i.get("job_url"),
            "description": i.get("job_description"),
            "posted_date": i.get("job_posted_date"),
            "workplace_type": "Remote" if i.get("job_remote") else None,
            "employment_type": _text(i.get("job_types")), "seniority_level": _text(i.get("job_levels")),
            "location": loc.get("unknown") if isinstance(loc, dict) else _text(loc)}


def career_input(limit: int) -> dict:
    # This actor only offers 1h/24h/7d windows; 7d is the closest to LOOKBACK_DAYS.
    return {"timeRange": "7d", "limit": max(limit, 10),  # Actor minimum is 10.
            "titleSearch": TITLES, "locationSearch": ["United States"],
            "aiWorkArrangementFilter": ["Hybrid", "Remote OK", "Remote Solely"],
            "aiEmploymentTypeFilter": ["FULL_TIME"], "aiExperienceLevelFilter": ["5-10"],
            "descriptionType": "text"}


def career_job(i: dict) -> dict:
    return {"title": (i.get("title") or "").strip(), "company": i.get("organization"),
            "link": i.get("url"), "apply_link": i.get("url"),  # The company's application page.
            "description": i.get("description_text"),
            "posted_date": i.get("date_posted"), "workplace_type": i.get("ai_work_arrangement"),
            "employment_type": _text(i.get("ai_employment_type")),
            "seniority_level": i.get("ai_experience_level"),
            "location": _text(i.get("locations_derived")),
            "visa_sponsorship": i.get("ai_visa_sponsorship")}


# name: (actor id, input builder, normalizer, default daily limit)
# Each source is an Apify Store actor. Its ID is NOT hardcoded: set it in .env (see .env.example).
# Find an actor on https://apify.com/store, open it, and copy the ID shown under
# "API" (or use "username/actor-name"). The input builders and normalizers below were
# written for these actors; another actor may need its own input/field mapping:
#   APIFY_ACTOR_LINKEDIN   curious_coder/linkedin-jobs-scraper
#   APIFY_ACTOR_INDEED     valig/indeed-jobs-scraper
#   APIFY_ACTOR_DICE       shahidirfan/Dice-Job-Scraper
#   APIFY_ACTOR_GLASSDOOR  bebity/glassdoor-jobs-scraper
#   APIFY_ACTOR_CAREER     fantastic-jobs/career-site-job-listing-api
# Sources whose variable is empty are skipped.
# name: (env var with the actor ID, input builder, normalizer, default daily limit)
SOURCES = {
    "linkedin": ("APIFY_ACTOR_LINKEDIN", linkedin_input, linkedin_job, 5),   # Lowest priority.
    "indeed": ("APIFY_ACTOR_INDEED", indeed_input, indeed_job, 25),          # Many link to employer ATS.
    "dice": ("APIFY_ACTOR_DICE", dice_input, dice_job, 10),
    "glassdoor": ("APIFY_ACTOR_GLASSDOOR", glassdoor_input, glassdoor_job, 10),
    "career": ("APIFY_ACTOR_CAREER", career_input, career_job, 10),
}


def actor_id(name: str) -> str | None:
    return (os.environ.get(SOURCES[name][0]) or "").strip() or None


# ---------- pipeline ----------

def run_source(client: ApifyClient, name: str, limit: int, max_charge_usd: float) -> list[dict]:
    env_var, build_input, normalize, _ = SOURCES[name]
    if not actor_id(name):
        raise RuntimeError(f"{env_var} is not set in .env")
    run = client.actor(actor_id(name)).call(run_input=build_input(limit),
                                      max_total_charge_usd=Decimal(str(max_charge_usd)))
    if run is None or str(run.status).split(".")[-1].upper() != "SUCCEEDED":
        raise RuntimeError(f"Apify run did not succeed: {getattr(run, 'status', None)}")
    items = client.dataset(run.default_dataset_id).list_items().items
    return [{"source": name, **normalize(i)} for i in items]


def dedupe_key(job: dict) -> str:
    return re.sub(r"\W+", " ", f"{job['title']} {job['company']}").strip().lower()


def loose_key(job: dict) -> str:
    """Title + first meaningful word of the company, so one posting listed by two boards under
    "Penske" and "Penske Truck Leasing Co., L.P." counts once (both got resumes on 2026-10-21)."""
    words = [w for w in re.findall(r"[a-z0-9]+", (job["company"] or "").lower())
             if w not in {"the", "inc", "llc", "co", "corp", "company", "group", "a"}]
    title = re.sub(r"\W+", " ", job["title"] or "").strip().lower()
    return f"{title} | {words[0] if words else ''}"


def relevant(job: dict) -> bool:
    title = job["title"] or ""
    if not TITLE_INCLUDE.search(title) or TITLE_EXCLUDE.search(title):
        return False
    return not EXCLUDED_EMPLOYMENT.search(job.get("employment_type") or "")


def hard_reject(job: dict) -> str | None:
    """Reason to reject outright from the description, or None."""
    text = job.get("description") or ""
    if NO_SPONSORSHIP.search(text):
        return "no sponsorship"
    if CLEARANCE.search(text):
        return "security clearance"
    return None


def priority(job: dict) -> int:
    """Lower is better. Company career-site postings are almost always real roles, while
    LinkedIn is full of reposted/ghost listings, so the daily scoring slots go to company
    portals first (configurable)."""
    if job["source"] == "career":
        return 0
    if job["source"] == "indeed" and "indeed.com" not in (job.get("apply_link") or "indeed.com"):
        return 1  # Indeed listing that applies on the employer's own ATS page.
    if job["source"] == "linkedin":
        return 3
    return 2


def select_by_priority(jobs: list[dict], cap: int) -> list[dict]:
    """Fill the cap tier by tier (see priority); within a tier, alternate between sources."""
    selected = []
    for tier in sorted({priority(j) for j in jobs}):
        by_source: dict[str, list[dict]] = {}
        for j in jobs:
            if priority(j) == tier:
                by_source.setdefault(j["source"], []).append(j)
        selected += [j for group in zip_longest(*by_source.values()) for j in group if j]
    return selected[:cap]


def scrape(sources: list[str], limits: dict[str, int], max_jobs: int,
           max_charge_usd: float) -> tuple[list[dict], list[dict]]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set (add it to .env)")
    client = ApifyClient(token)

    jobs, failed = [], []
    for name in sources:
        try:
            found = run_source(client, name, limits[name], max_charge_usd)
            print(f"  {name:<10} {len(found)} jobs")
            jobs.extend(found)
        except Exception as exc:  # One broken board shouldn't sink the whole morning.
            failed.append(name)
            print(f"  {name:<10} FAILED: {str(exc)[:200]}")
    if failed and len(failed) == len(sources):
        raise RuntimeError(f"All sources failed: {', '.join(failed)}")
    if not jobs:  # Every board returning nothing means a scraper broke, not a quiet day.
        raise RuntimeError("No jobs returned by any source")

    seen = load_seen()
    seen_links, seen_keys, unique, log = set(), set(), [], []

    def note(job: dict, outcome: str) -> None:
        log.append({k: job.get(k) for k in ("source", "title", "company", "link", "posted_date")}
                   | {"outcome": outcome})

    dropped = {"incomplete": 0, "duplicate": 0, "repost/expired": 0, "not relevant": 0, "no sponsorship": 0,
               "security clearance": 0, "seen before": 0}
    for j in jobs:
        if not (j["title"] and j["company"] and j["link"] and j["description"]):
            dropped["incomplete"] += 1
            continue
        key = dedupe_key(j)
        loose = loose_key(j)
        if j["link"] in seen_links or key in seen_keys or loose in seen_keys:
            dropped["duplicate"] += 1
            continue
        seen_links.add(j["link"])
        seen_keys.update((key, loose))
        if j.get("stale"):
            dropped["repost/expired"] += 1
            note(j, "Rejected: reposted or expired listing")
            continue
        if not relevant(j):
            dropped["not relevant"] += 1
            note(j, "Rejected: title or job type not a fit")
            continue
        reason = hard_reject(j)
        if reason:
            dropped[reason] += 1
            print(f"  REJECT ({reason}): {j['title']} | {j['company']} [{j['source']}]")
            note(j, f"Rejected: {reason}")
            continue
        if j["link"] in seen or key in seen:
            dropped["seen before"] += 1
            continue
        unique.append(j)

    selected = select_by_priority(unique, max_jobs)
    chosen = {j["link"] for j in selected}
    for j in unique:
        note(j, "Sent to scoring" if j["link"] in chosen else "Skipped: over daily scoring cap")
    print(f"{len(jobs)} found -> dropped " + ", ".join(f"{v} {k}" for k, v in dropped.items())
          + f" -> {len(unique)} new relevant -> {len(selected)} kept (cap {max_jobs})")
    if failed:
        print(f"WARNING: sources failed today: {', '.join(failed)}")
    return selected, log


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", default="all",
                        help=f"Comma-separated subset of: {', '.join(SOURCES)} (default all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Override every source's daily limit (for cheap test runs)")
    parser.add_argument("--max-jobs", type=int, default=30,
                        help="Max jobs passed on to scoring (free OpenRouter tier: 50 calls/day)")
    parser.add_argument("--max-charge-usd", type=float, default=0.50,
                        help="Hard spend cap for each Apify run")
    args = parser.parse_args()

    sources = ([s for s in SOURCES if actor_id(s)] if args.sources == "all"
               else [s.strip() for s in args.sources.split(",")])
    if not sources:
        parser.error("No job sources configured: set at least one APIFY_ACTOR_* in .env")
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        parser.error(f"Unknown sources: {', '.join(unknown)}")
    limits = {s: args.limit or SOURCES[s][3] for s in sources}

    jobs, log = scrape(sources, limits, args.max_jobs, args.max_charge_usd)
    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    LOG_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    by_source = {}
    for j in jobs:
        by_source[j["source"]] = by_source.get(j["source"], 0) + 1
    print(f"{len(jobs)} jobs scraped {by_source} -> {OUTPUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
