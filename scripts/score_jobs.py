"""Score scraped jobs against the base resume and keep only strong fits.

One OpenRouter call per job returns a base fit score, boosts, dealbreakers, and
the matched/missing requirements (no separate parse step). The final 1-10 score
is computed here from those; only jobs scoring >= MIN_SCORE (7) continue.

Usage:
    python scripts/score_jobs.py
    python scripts/score_jobs.py --min-score 7 --model anthropic/claude-haiku-5.5
Reads output/raw_jobs.json, writes output/scored_jobs.json (every job) and
output/qualified_jobs.json (score >= min), and prints "N scraped, M qualified".
"""

import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from openrouter import chat, default_model, free_requests_remaining
from seen_jobs import load_pending, mark_seen

ROOT = Path(__file__).resolve().parent.parent
RESUME_FILE = ROOT / "resume" / "base_resume.md"
RAW_FILE = ROOT / "output" / "raw_jobs.json"
SCORED_FILE = ROOT / "output" / "scored_jobs.json"
QUALIFIED_FILE = ROOT / "output" / "qualified_jobs.json"

MIN_SCORE = 7  # Jobs scoring below this get no resume.
CALLS_PER_QUALIFIED = 2  # Tailoring: 1 call, plus 1 retry if validation fails.
MAX_DESCRIPTION_CHARS = 12000  # Long postings are mostly boilerplate past this.

# Final score = base_score + boosts (max +2), then capped at DEALBREAKER_CAP if any
# dealbreaker applies. The arithmetic is done in code so the model can't skip a rule.
MAX_BOOST = 2
DEALBREAKER_CAP = 5
BOOSTS = {
    "financial_services": "The employer or project is in banking, brokerage, fintech, wealth management, or insurance.",
    "cloud_infrastructure": "The work centers on Azure, GCP, cloud migration, or infrastructure modernization.",
    "agile_scrum": "Agile/Scrum delivery is a core part of the role (sprints, ceremonies, Jira-driven delivery).",
    "pmp": "The posting explicitly requires or prefers PMP certification.",
}
DEALBREAKERS = {
    "security_clearance": "Requires an active or obtainable government security clearance.",
    "citizenship_or_no_sponsorship": "Requires US citizenship or permanent residency, or states no visa sponsorship.",
    "too_senior": "Requires 12+ years of experience, or is a Director, VP, or Head-of level role.",
}


def _bullets(items: dict) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in items.items())


SYSTEM_PROMPT = f"""You are a strict technical recruiter screening job postings for one candidate.
Compare the job description against the candidate's resume.

1. base_score (integer 1-8), judged only on role fit and requirements:
- 8: Technology/IT/digital project or program management role, and the resume meets every
  hard requirement (domain, years, methodology, tools, certifications).
- 7: Same kind of role; one or two minor gaps (a nice-to-have tool, a slightly different domain).
- 5-6: Technology PM role, but a hard requirement the resume does not show (a required platform,
  industry, license, or certification).
- 1-4: Different field. Construction, engineering trades, manufacturing plants, utilities/water,
  clinical healthcare, field/site PM roles.
Judge only from what the resume actually says; do not assume unstated experience. Generic overlap
(scheduling, budgets, stakeholders) is not enough on its own for 7+.

2. boosts: list every key below that clearly applies to the job (only if base_score >= 5):
{_bullets(BOOSTS)}

3. dealbreakers: list every key below that the posting states. Do not guess; if the posting is
silent, it does not apply.
{_bullets(DEALBREAKERS)}

Reply with only a JSON object, no prose and no code fence:
{{"base_score": <integer 1-8>, "boosts": [<keys>], "dealbreakers": [<keys>],
 "matched": [<requirements the resume meets>], "missing": [<requirements the resume does not show>],
 "reason": "<one sentence>"}}"""


def build_user_prompt(resume: str, job: dict) -> str:
    description = (job["description"] or "")[:MAX_DESCRIPTION_CHARS]
    return (
        f"<resume>\n{resume}\n</resume>\n\n"
        f"<job>\nTitle: {job['title']}\nCompany: {job['company']}\n"
        f"Workplace: {job.get('workplace_type') or 'unknown'}\n\n{description}\n</job>"
    )


def _last_json_object(text: str) -> dict | None:
    """Reasoning models may think out loud before the answer, so take the last
    JSON object in the reply that has a base_score."""
    decoder = json.JSONDecoder()
    for start in reversed([m.start() for m in re.finditer(r"\{", text)]):
        try:
            obj, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "base_score" in obj:
            return obj
    return None


def parse_reply(text: str) -> dict:
    data = _last_json_object(text)
    if data is None:
        raise ValueError(f"No JSON object in model reply: {text[:200]!r}")
    base = int(data["base_score"])
    if not 1 <= base <= 8:
        raise ValueError(f"base_score out of range: {base}")
    boosts = [b for b in data.get("boosts") or [] if b in BOOSTS] if base >= 5 else []
    dealbreakers = [d for d in data.get("dealbreakers") or [] if d in DEALBREAKERS]
    score = min(10, base + min(MAX_BOOST, len(boosts)))
    if dealbreakers:
        score = min(score, DEALBREAKER_CAP)
    return {
        "score": score,
        "base_score": base,
        "boosts": boosts,
        "dealbreakers": dealbreakers,
        "matched": list(data.get("matched") or []),
        "missing": list(data.get("missing") or []),
        "reason": str(data.get("reason") or ""),
    }


def score_job(model: str, resume: str, job: dict) -> dict:
    return parse_reply(chat(SYSTEM_PROMPT, build_user_prompt(resume, job), model=model))


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-score", type=int, default=MIN_SCORE)
    parser.add_argument("--model", default=default_model())
    args = parser.parse_args()

    resume = RESUME_FILE.read_text(encoding="utf-8")
    jobs = json.loads(RAW_FILE.read_text(encoding="utf-8"))

    scored, errors, deferred = [], [], []
    pending = len(load_pending())
    for job in jobs:
        # Free-model budget: keep ~2 requests per qualified/pending job for tailoring.
        # Jobs not scored today stay unseen and are picked up tomorrow.
        remaining = free_requests_remaining(args.model)
        qualified_so_far = sum(j["score"] >= args.min_score for j in scored)
        if remaining is not None and remaining <= CALLS_PER_QUALIFIED * (qualified_so_far + pending) + 1:
            deferred = jobs[jobs.index(job):]
            print(f"  BUDGET   {remaining} free requests left; deferring {len(deferred)} jobs to the next run")
            break
        try:
            job.update(score_job(args.model, resume, job))
        except Exception as exc:  # e.g. free-tier daily limit; retry the job tomorrow.
            errors.append(job)
            print(f"  ERR {job['title']} | {job['company']}: {str(exc)[:160]}")
            continue
        scored.append(job)
        flags = " ".join([f"+{b}" for b in job["boosts"]] + [f"!{d}" for d in job["dealbreakers"]])
        print(f"  {job['score']:>2}  {job['title']} | {job['company']} [{job['source']}]  {flags}")

    mark_seen(scored)  # Unscored (errored) jobs stay unseen so a later run retries them.
    qualified = [j for j in scored if j["score"] >= args.min_score]
    SCORED_FILE.write_text(json.dumps(scored, indent=2, ensure_ascii=False), encoding="utf-8")
    QUALIFIED_FILE.write_text(json.dumps(qualified, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(jobs)} scraped, {len(scored)} scored, {len(qualified)} qualified "
          f"(score >= {args.min_score}), {len(errors)} errors, {len(deferred)} deferred"
          f" -> {QUALIFIED_FILE.relative_to(ROOT)}")
    if errors and not scored:
        sys.exit(1)


if __name__ == "__main__":
    main()
