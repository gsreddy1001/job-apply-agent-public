"""Daily Job Application Agent run on Modal.

Every day at 5:00 AM America/Los_Angeles (Pacific): scrape (Indeed, Dice, Glassdoor,
company career sites) -> score -> tailor -> Google Doc + PDF
(in Drive) -> tracker Sheet. Any failure sends a Telegram alert titled
"JOB-APPLY-AGENT — WHAT BROKE" and then re-raises, so Modal also marks the run failed.

Deploy:          modal deploy modal_app.py
Run once now:    modal run modal_app.py
Test the alert:  modal run modal_app.py --force-failure

Modal secrets (see CLAUDE.md for how they are created):
  job-agent-env  APIFY_TOKEN, OPENROUTER_API_KEY, OPENROUTER_MODEL,
                 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  job-agent-gws  GWS_CREDENTIALS_JSON  (output of `gws auth export --unmasked`)
"""

import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

import modal

APP_DIR = "/root/app"
MAX_JOBS_SCORED = 30  # Free OpenRouter tier: 50 requests/day, minus room for tailoring.
SCHEDULE = modal.Cron("0 5 * * *", timezone="America/Los_Angeles")  # 5:00 AM Pacific, DST-aware.
# Deploy with JOB_AGENT_PAUSE_SCHEDULE=1 to skip scheduled runs (e.g. after a manual run);
# a normal `modal deploy` restores the schedule.
if os.environ.get("JOB_AGENT_PAUSE_SCHEDULE"):
    SCHEDULE = None
ALERT_TITLE = "JOB-APPLY-AGENT — WHAT BROKE"

image = (
    # Ubuntu 24.04, not debian_slim: the gws native binary needs glibc >= 2.39.
    modal.Image.from_registry("ubuntu:24.04", add_python="3.12")
    .env({"DEBIAN_FRONTEND": "noninteractive"})
    .apt_install("curl", "ca-certificates", "tzdata")
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
        "apt-get install -y nodejs",
        "npm install -g @googleworkspace/cli@0.22.5",
    )
    .pip_install_from_requirements("requirements.txt")
    .env({
        "TZ": "America/Los_Angeles",         # Run-date folders use the schedule's local date.
        "PDF_DESTINATION": "drive",          # No Desktop on Modal.
        "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file",
        "PYTHONIOENCODING": "utf-8",
        "STATE_DIR": f"{APP_DIR}/state",
    })
    .add_local_dir("scripts", f"{APP_DIR}/scripts")
    .add_local_dir("resume", f"{APP_DIR}/resume")
)

app = modal.App("job-apply-agent", image=image)
# Persists state/seen_jobs.json between daily runs so jobs are never scored twice.
state_volume = modal.Volume.from_name("job-agent-state", create_if_missing=True)

STEPS = [
    ("scrape", ["scripts/scrape_jobs.py", "--max-jobs", str(MAX_JOBS_SCORED)]),
    ("score", ["scripts/score_jobs.py"]),
    ("tailor", ["scripts/tailor_job.py"]),
    ("sheet", ["scripts/write_sheet.py"]),
]


class StepFailed(RuntimeError):
    def __init__(self, step: str, output: str):
        super().__init__(f"step '{step}' failed")
        self.step, self.output = step, output


def _write_google_credentials() -> None:
    creds = os.environ.get("GWS_CREDENTIALS_JSON")
    if not creds:
        raise RuntimeError("GWS_CREDENTIALS_JSON secret is missing")
    path = Path("/tmp/gws_credentials.json")
    path.write_text(creds, encoding="utf-8")
    path.chmod(0o600)
    os.environ["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = str(path)


def _run_step(name: str, args: list[str]) -> None:
    print(f"=== {name} ===", flush=True)
    proc = subprocess.run([sys.executable, *args], cwd=APP_DIR, capture_output=True,
                          text=True, encoding="utf-8")
    output = (proc.stdout + proc.stderr).strip()
    print(output, flush=True)
    if proc.returncode != 0:
        raise StepFailed(name, output)


def send_alert(error: BaseException) -> None:
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("WARNING: Telegram secrets missing; cannot send failure alert.", flush=True)
        return
    import requests

    step = getattr(error, "step", "pipeline")
    detail = getattr(error, "output", "") or "".join(traceback.format_exception(error))
    text = (f"{ALERT_TITLE}\n\n"
            f"Step: {step}\n"
            f"When: {datetime.now():%Y-%m-%d %H:%M %Z}\n"
            f"Error: {error}\n\n"
            f"{detail[-3000:]}")
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat_id, "text": text}, timeout=30)
    print(f"Telegram alert sent: HTTP {response.status_code}", flush=True)


@app.function(
    schedule=SCHEDULE,
    timeout=3 * 60 * 60,
    secrets=[modal.Secret.from_name("job-agent-env"), modal.Secret.from_name("job-agent-gws")],
    volumes={f"{APP_DIR}/state": state_volume},
)
def daily_run(force_failure: bool = False) -> None:
    try:
        if force_failure:
            raise RuntimeError("Forced failure to test the Telegram alert")
        _write_google_credentials()
        try:
            for name, args in STEPS:
                _run_step(name, args)
        finally:
            state_volume.commit()  # Keep seen-jobs even if a later step fails.
    except Exception as exc:
        send_alert(exc)
        raise


@app.function(secrets=[modal.Secret.from_name("job-agent-env"),
                        modal.Secret.from_name("job-agent-gws")])
def preflight() -> None:
    """Cheap check (no scraping, no LLM calls): secrets present and Google reachable."""
    for key in ("APIFY_TOKEN", "OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        print(f"{key}: {'set' if os.environ.get(key) else 'MISSING'}")
    _write_google_credentials()
    sys.path.insert(0, f"{APP_DIR}/scripts")
    from format_resume_doc import ROOT_FOLDER, ensure_folder
    print(f"Google Drive OK, '{ROOT_FOLDER}' folder id: {ensure_folder(ROOT_FOLDER)}")


@app.local_entrypoint()
def main(force_failure: bool = False, check: bool = False) -> None:
    if check:
        preflight.remote()
    else:
        daily_run.remote(force_failure=force_failure)
