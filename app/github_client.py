"""
Minimal GitHub REST client.

Only the calls the service actually needs: read an issue, search for a PR
that references the issue, and (optionally) post a comment back.

The GitHub token is *read-only* by intent. The control plane never
modifies the target repository directly; Devin handles all code changes
through its own GitHub integration.
"""

from __future__ import annotations

from typing import Optional

import httpx

from .config import settings


GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = httpx.Timeout(15.0)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_issue(issue_number: int) -> dict:
    """Fetch a single issue by number from the configured repo."""
    url = (
        f"{GITHUB_API}/repos/"
        f"{settings.github_owner}/{settings.github_repo}/issues/{issue_number}"
    )
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.get(url, headers=_headers())
    resp.raise_for_status()
    return resp.json()


def find_pr_for_issue(issue_number: int) -> Optional[str]:
    """Best-effort: find a PR in the repo whose title/body mentions #N.

    Used as a fallback when Devin's session response does not yet expose a
    pr_url. Returns the html_url of the first match or None.
    """
    query = (
        f"repo:{settings.github_owner}/{settings.github_repo} "
        f"type:pr \"#{issue_number}\""
    )
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.get(
            f"{GITHUB_API}/search/issues",
            headers=_headers(),
            params={"q": query},
        )
    if resp.status_code != 200:
        # Search is rate-limited and noisy; treat as "no result" rather than
        # propagating an exception that would mask the real task state.
        return None
    items = resp.json().get("items") or []
    if not items:
        return None
    return items[0].get("html_url")


def comment_on_issue(issue_number: int, body: str) -> dict:
    """Post a comment back to the issue. Requires Issues: write on the token.

    Optional: the service still works without this. It exists so the demo
    can show the Devin session URL appearing on the GitHub issue.
    """
    url = (
        f"{GITHUB_API}/repos/"
        f"{settings.github_owner}/{settings.github_repo}/issues/{issue_number}/comments"
    )
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.post(url, headers=_headers(), json={"body": body})
    resp.raise_for_status()
    return resp.json()
