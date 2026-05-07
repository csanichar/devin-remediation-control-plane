"""
Devin v3 organization sessions client.

Two operations are needed:
  - create a session for a given issue
  - poll a session by id (used by /tasks/{id}/sync)

The Devin API response shape can vary between versions, so the
extract_* helpers tolerate several common field names rather than
assuming one exact schema.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .config import settings


DEVIN_API_BASE = "https://api.devin.ai/v3"
DEFAULT_TIMEOUT = httpx.Timeout(30.0)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.devin_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _sessions_url() -> str:
    return f"{DEVIN_API_BASE}/organizations/{settings.devin_org_id}/sessions"


def create_devin_session(
    title: str,
    prompt: str,
    repo_url: str,
    issue_number: int,
    issue_kind: str,
) -> dict:
    """Create a new Devin session for the given issue.

    Raises httpx.HTTPStatusError on non-2xx so the caller can record a
    helpful failure_reason on the task.
    """
    payload = {
        "title": title,
        "prompt": prompt,
        "repos": [repo_url],
        "tags": [
            "superset",
            "devin-autofix",
            f"issue-{issue_number}",
            issue_kind,
            "take-home-demo",
        ],
        # Keep ACU spend bounded for the demo. Production would size this
        # by issue severity (see "Production Extensions" in the README).
        "max_acu_limit": 5,
        "structured_output_required": False,
    }
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.post(_sessions_url(), headers=_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


def get_devin_session(devin_session_id: str) -> dict:
    """Fetch the current state of a Devin session."""
    url = f"{_sessions_url()}/{devin_session_id}"
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.get(url, headers=_headers())
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
# Defensive response helpers
# --------------------------------------------------------------------------- #
#
# Devin response payloads have evolved across versions. Rather than
# crashing if a key is renamed, we look in a few likely places and
# return None when nothing matches. The caller then stores whatever it
# could find and surfaces the rest in the raw response.

def _first_present(data: dict, keys: tuple[str, ...]) -> Optional[Any]:
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None


def extract_session_id(session_response: dict) -> Optional[str]:
    value = _first_present(session_response, ("session_id", "id", "devin_id"))
    return str(value) if value is not None else None


def extract_session_url(session_response: dict) -> Optional[str]:
    return _first_present(session_response, ("url", "app_url", "session_url"))


def extract_pr_url(session_response: dict) -> Optional[str]:
    # Direct fields first.
    direct = _first_present(session_response, ("pr_url", "pull_request_url"))
    if direct:
        return direct

    # Then a list of pull requests, taking the first usable URL.
    prs = session_response.get("pull_requests")
    if isinstance(prs, list) and prs:
        first = prs[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url") or first.get("html_url") or first.get("pr_url")
    return None


def extract_status(session_response: dict) -> Optional[str]:
    """Return whatever Devin calls the current session status."""
    value = _first_present(session_response, ("status", "state", "session_status"))
    return str(value) if value is not None else None
