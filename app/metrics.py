"""
Aggregate metrics that answer:

    "If I were an engineering leader, how would I know this is working?"

Reads from the events and tasks tables only; no external calls.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import db
from .models import TaskStatus


_ACTIVE = {
    TaskStatus.RECEIVED.value,
    TaskStatus.CLASSIFIED.value,
    TaskStatus.SESSION_CREATED.value,
    TaskStatus.RUNNING.value,
}
_COMPLETED = {TaskStatus.COMPLETED.value}
_FAILED = {TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _avg_minutes(deltas: list[float]) -> Optional[float]:
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas) / 60.0, 2)


def compute_metrics() -> dict:
    events = db.list_events()
    tasks = db.list_tasks()

    total_events = len(events)
    ignored_events = sum(1 for e in events if not e["eligible"])
    eligible_tasks = len(tasks)

    sessions_created = sum(1 for t in tasks if t["devin_session_id"])
    active_tasks = sum(1 for t in tasks if t["status"] in _ACTIVE)
    completed_tasks = sum(1 for t in tasks if t["status"] in _COMPLETED)
    failed_tasks = sum(1 for t in tasks if t["status"] in _FAILED)
    prs_opened = sum(1 for t in tasks if t["pr_url"])

    # Conversion rates use eligible_tasks (i.e. tasks that actually entered
    # the funnel) as the denominator, which is the honest framing for an
    # engineering leader.
    def _rate(numerator: int) -> float:
        if eligible_tasks == 0:
            return 0.0
        return round(numerator / eligible_tasks, 4)

    # Time-to-PR: created_at -> updated_at on the first task event that set
    # pr_url. We approximate using updated_at when pr_url is set.
    pr_deltas: list[float] = []
    completion_deltas: list[float] = []
    for t in tasks:
        created = _parse_iso(t["created_at"])
        if created is None:
            continue
        if t["pr_url"]:
            updated = _parse_iso(t["updated_at"])
            if updated is not None:
                pr_deltas.append((updated - created).total_seconds())
        if t["status"] == TaskStatus.COMPLETED.value:
            completed = _parse_iso(t["completed_at"]) or _parse_iso(t["updated_at"])
            if completed is not None:
                completion_deltas.append((completed - created).total_seconds())

    by_kind: dict[str, dict[str, int]] = {}
    for t in tasks:
        kind = t["issue_kind"]
        bucket = by_kind.setdefault(
            kind, {"tasks": 0, "prs_opened": 0, "failed": 0}
        )
        bucket["tasks"] += 1
        if t["pr_url"]:
            bucket["prs_opened"] += 1
        if t["status"] in _FAILED:
            bucket["failed"] += 1

    return {
        "total_events_received": total_events,
        "eligible_tasks": eligible_tasks,
        "ignored_events": ignored_events,
        "sessions_created": sessions_created,
        "active_tasks": active_tasks,
        "completed_tasks": completed_tasks,
        "failed_tasks": failed_tasks,
        "prs_opened": prs_opened,
        "issue_to_session_conversion_rate": _rate(sessions_created),
        "issue_to_pr_conversion_rate": _rate(prs_opened),
        "average_time_to_pr_minutes": _avg_minutes(pr_deltas),
        "average_completion_time_minutes": _avg_minutes(completion_deltas),
        "by_issue_kind": by_kind,
    }
