"""
Lightweight task and issue type constants.

Plain string enums are used so that values can be stored in SQLite as TEXT
and compared without conversion. Code reads as `TaskStatus.RUNNING` while
the database simply stores the lowercase string.
"""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    RECEIVED = "received"
    IGNORED = "ignored"
    CLASSIFIED = "classified"
    SESSION_CREATED = "session_created"
    RUNNING = "running"
    PR_OPENED = "pr_opened"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class IssueKind(str, Enum):
    IMPORT_STALE_CHART_REFS = "import-stale-chart-refs"
    IMPORT_MISSING_DATASET = "import-missing-dataset"
    FRONTEND_CACHE_BOUND = "frontend-cache-bound"
    GENERAL = "general-devin-autofix"


# A task in one of these statuses is considered "in flight" for an issue.
# Used by the idempotency guard in db.find_active_task_for_issue so that
# repeated GitHub events for the same issue do not start duplicate sessions.
ACTIVE_TASK_STATUSES = (
    TaskStatus.CLASSIFIED.value,
    TaskStatus.SESSION_CREATED.value,
    TaskStatus.RUNNING.value,
    TaskStatus.PR_OPENED.value,
)


# Label that gates whether the service will create a Devin session.
DEVIN_AUTOFIX_LABEL = "devin-autofix"
