"""Minimal OpenRouter chat client shared by the pipeline scripts."""

import json
import os
import time

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RETRY_WAITS = [15, 45, 120]  # Seconds between attempts.


def default_model() -> str:
    return os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL


def free_requests_remaining(model: str | None = None) -> int | None:
    """Requests left today on OpenRouter's free-model quota (50/day on a $0 account), or
    None when the model is paid or the number is unavailable. Used to budget each run."""
    if not (model or default_model()).endswith(":free"):
        return None
    try:
        data = requests.get("https://openrouter.ai/api/v1/key", timeout=30, headers={
            "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"}).json()["data"]
        return int(data["free_model_daily_requests"]["remaining"])
    except Exception:
        return None


def chat(system: str, user: str, model: str | None = None, max_tokens: int = 8000,
         temperature: float = 0) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (add it to .env)")
    payload = {
        "model": model or default_model(),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # Free models are often rate-limited upstream (429) or briefly unavailable (5xx);
    # back off and retry so one busy minute doesn't fail the unattended daily run.
    for attempt, wait in enumerate(RETRY_WAITS + [None]):
        try:
            response = requests.post(OPENROUTER_URL, json=payload, timeout=300,
                                     headers={"Authorization": f"Bearer {api_key}"})
        except requests.RequestException as exc:
            if wait is None:
                raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
            time.sleep(wait)
            continue
        if response.status_code == 402:
            raise RuntimeError("OpenRouter: out of credits (402). Add credits at openrouter.ai/settings/credits")
        if response.status_code in RETRY_STATUSES and wait is not None:
            time.sleep(wait)
            continue
        if not response.ok:
            raise RuntimeError(f"OpenRouter {response.status_code}: {response.text[:500]}")
        body = response.json()
        if "choices" not in body:  # OpenRouter sometimes returns 200 with an error object.
            error = body.get("error") or body
            if wait is not None and str(error.get("code", "")) in {"429", "502", "503"}:
                time.sleep(wait)
                continue
            raise RuntimeError(f"OpenRouter error: {json.dumps(error)[:400]}")
        content = body["choices"][0]["message"]["content"]
        if content or wait is None:
            return content or ""
        time.sleep(wait)  # Empty reply: treat like a transient failure.
    raise AssertionError("unreachable")
