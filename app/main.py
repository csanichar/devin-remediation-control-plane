"""
FastAPI entry point — wires intake, policy, execution, and reporting.

Layers (matching the architecture diagram in README.md):
  1. Intake     -- /webhooks/github and /simulate/issue/{n}
  2. Policy     -- label filtering + classify_issue
  3. Execution  -- create_devin_session
  4. Reporting  -- /tasks, /tasks/{id}, /tasks/{id}/sync, /metrics
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request

from . import db, devin_client, github_client, prompts
from .config import settings
from .metrics import compute_metrics
from .models import DEVIN_AUTOFIX_LABEL, TaskStatus


logger = logging.getLogger("devin-remediation-control-plane")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


app = FastAPI(title="Devin Remediation Control Plane")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    logger.info("startup_db_ready path=%s", db.DB_PATH)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _has_autofix_label(issue: dict) -> bool:
    for label in issue.get("labels") or []:
        # GitHub label entries are objects with a "name", but in the wild
        # they are sometimes returned as bare strings.
        name = label.get("name") if isinstance(label, dict) else label
        if name == DEVIN_AUTOFIX_LABEL:
            return True
    return False


def _verify_github_signature(raw_body: bytes, signature_header: Optional[str]) -> None:
    """Verify X-Hub-Signature-256.

    If GITHUB_WEBHOOK_SECRET is unset, the service is in local/demo mode
    and unsigned requests are allowed. This is intentionally obvious so a
    production deploy that forgets to set the secret stands out in logs.
    """
    if not settings.github_webhook_secret:
        logger.warning(
            "webhook_signature_skipped reason=no_secret_configured "
            "(set GITHUB_WEBHOOK_SECRET in production)"
        )
        return

    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="missing or malformed signature")

    provided = signature_header.split("=", 1)[1]
    expected = hmac.new(
        settings.github_webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid signature")


def _serialize_task(task: dict) -> dict:
    """Tasks are already plain dicts from db.py; this hook lets us shape
    the response if we ever want to add derived fields."""
    return task


def start_devin_for_issue(issue: dict, repo_url: str) -> dict:
    """Shared pipeline: classify -> persist task -> request Devin session.

    Used by both /webhooks/github and /simulate/issue/{n} so the two
    intake paths cannot drift apart.
    """
    issue_number = issue["number"]
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    issue_url = issue.get("html_url") or ""

    # Idempotency: if there's already a live task for this issue, return it
    # rather than burning Devin ACUs on a duplicate session.
    existing = db.find_active_task_for_issue(issue_number)
    if existing:
        logger.info(
            "task_idempotent_hit issue=%s task=%s status=%s",
            issue_number, existing["id"], existing["status"],
        )
        return {"task": _serialize_task(existing), "deduped": True}

    base_branch = settings.target_base_branch
    issue_kind, prompt = prompts.build_prompt(
        repo_url=repo_url,
        base_branch=base_branch or "main",
        issue_number=issue_number,
        issue_url=issue_url,
        title=title,
        body=body,
    )

    task = db.create_task(
        issue_number=issue_number,
        issue_title=title,
        issue_url=issue_url,
        repo_url=repo_url,
        base_branch=base_branch,
        issue_kind=issue_kind,
        status=TaskStatus.CLASSIFIED.value,
    )
    logger.info(
        "task_created task=%s issue=%s kind=%s",
        task["id"], issue_number, issue_kind,
    )

    devin_title = f"[autofix] superset issue #{issue_number}: {title[:80]}"

    try:
        logger.info("devin_session_requested task=%s", task["id"])
        session = devin_client.create_devin_session(
            title=devin_title,
            prompt=prompt,
            repo_url=repo_url,
            issue_number=issue_number,
            issue_kind=issue_kind,
        )
    except httpx.HTTPError as exc:
        # Capture API error text but never the Authorization header.
        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"{exc.response.status_code}: {exc.response.text[:500]}"
        task = db.update_task(
            task["id"],
            status=TaskStatus.FAILED.value,
            failure_reason=f"devin_create_session_failed: {detail}",
        )
        logger.error("task_failed task=%s reason=%s", task["id"], detail)
        return {"task": _serialize_task(task), "error": detail}

    session_id = devin_client.extract_session_id(session)
    session_url = devin_client.extract_session_url(session)
    pr_url = devin_client.extract_pr_url(session)

    task = db.update_task(
        task["id"],
        devin_session_id=session_id,
        devin_session_url=session_url,
        pr_url=pr_url,
        status=TaskStatus.SESSION_CREATED.value,
    )
    logger.info(
        "devin_session_created task=%s session_id=%s",
        task["id"], session_id,
    )
    return {
        "task": _serialize_task(task),
        "devin_session": session,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/")
def health() -> dict:
    return {"status": "ok", "service": "devin-remediation-control-plane"}


@app.post("/webhooks/github")
async def github_webhook(request: Request) -> dict:
    """Receive GitHub issue events and route eligible ones to Devin."""
    raw = await request.body()
    _verify_github_signature(raw, request.headers.get("X-Hub-Signature-256"))

    event_type = request.headers.get("X-GitHub-Event") or "unknown"

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    logger.info("event_received type=%s", event_type)

    if event_type != "issues":
        db.record_event(
            event_type=event_type,
            action=payload.get("action"),
            issue_number=None,
            eligible=False,
            reason="non_issues_event",
        )
        return {"ignored": True, "reason": "non_issues_event"}

    action = payload.get("action")
    issue = payload.get("issue") or {}
    issue_number = issue.get("number")

    if action not in {"opened", "labeled", "edited"}:
        db.record_event(
            event_type=event_type,
            action=action,
            issue_number=issue_number,
            eligible=False,
            reason=f"unhandled_action:{action}",
        )
        logger.info("event_ignored reason=action action=%s", action)
        return {"ignored": True, "reason": f"unhandled_action:{action}"}

    if not _has_autofix_label(issue):
        db.record_event(
            event_type=event_type,
            action=action,
            issue_number=issue_number,
            eligible=False,
            reason="missing_devin_autofix_label",
        )
        logger.info("event_ignored reason=label issue=%s", issue_number)
        return {"ignored": True, "reason": "missing_devin_autofix_label"}

    db.record_event(
        event_type=event_type,
        action=action,
        issue_number=issue_number,
        eligible=True,
        reason=None,
    )
    return start_devin_for_issue(issue=issue, repo_url=settings.target_repo_url)


@app.post("/simulate/issue/{issue_number}")
def simulate_issue(issue_number: int) -> dict:
    """Pull the issue from GitHub and run the same pipeline as the webhook.

    Useful when you don't want to deal with ngrok during local development.
    """
    try:
        issue = github_client.get_issue(issue_number)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"github_get_issue_failed: {exc.response.text[:300]}",
        )

    eligible = _has_autofix_label(issue)
    db.record_event(
        event_type="simulate",
        action="manual",
        issue_number=issue_number,
        eligible=eligible,
        reason=None if eligible else "missing_devin_autofix_label",
    )

    if not eligible:
        logger.info("simulate_ignored issue=%s reason=label", issue_number)
        return {
            "ignored": True,
            "reason": "missing_devin_autofix_label",
            "issue_number": issue_number,
        }

    return start_devin_for_issue(issue=issue, repo_url=settings.target_repo_url)


@app.get("/tasks")
def list_tasks() -> dict:
    return {"tasks": [_serialize_task(t) for t in db.list_tasks()]}


@app.get("/tasks/{task_id}")
def get_task(task_id: int) -> dict:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return _serialize_task(task)


@app.post("/tasks/{task_id}/sync")
def sync_task(task_id: int) -> dict:
    """Pull fresh status from Devin and reconcile the task row.

    Devin's status vocabulary differs from ours, so this function maps it:
      - PR exists                               -> pr_opened
      - session reports completed AND PR exists -> completed
      - session reports a failure-ish status    -> failed
      - otherwise                               -> running
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if not task["devin_session_id"]:
        raise HTTPException(status_code=400, detail="task has no devin_session_id yet")

    try:
        session = devin_client.get_devin_session(task["devin_session_id"])
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"devin_get_session_failed: {exc.response.text[:300]}",
        )

    devin_status = devin_client.extract_status(session)
    pr_url = devin_client.extract_pr_url(session) or task["pr_url"]

    # Fall back to GitHub PR search if Devin hasn't surfaced the PR yet.
    if not pr_url:
        pr_url = github_client.find_pr_for_issue(task["issue_number"])

    if devin_status and devin_status.lower() in {"failed", "error", "cancelled", "canceled"}:
        new_status = TaskStatus.FAILED.value
    elif pr_url and devin_status and devin_status.lower() in {"completed", "finished", "succeeded", "success"}:
        new_status = TaskStatus.COMPLETED.value
    elif pr_url:
        new_status = TaskStatus.PR_OPENED.value
    else:
        new_status = TaskStatus.RUNNING.value

    fields: dict = {"status": new_status}
    if pr_url and pr_url != task["pr_url"]:
        fields["pr_url"] = pr_url
    if new_status == TaskStatus.COMPLETED.value and not task["completed_at"]:
        fields["completed_at"] = db.now_iso()

    updated = db.update_task(task_id, **fields)
    logger.info(
        "task_sync_completed task=%s status=%s pr=%s devin_status=%s",
        task_id, new_status, bool(pr_url), devin_status,
    )
    return {
        "task": _serialize_task(updated),
        "devin_status": devin_status,
        "devin_pr_url": pr_url,
    }


@app.get("/metrics")
def metrics() -> dict:
    return compute_metrics()
