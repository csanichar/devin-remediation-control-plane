"""
SQLite persistence layer for events and tasks.

Uses the standard library `sqlite3` module deliberately. SQLAlchemy would
add complexity that the take-home does not need, and the schema is tiny.
All helpers return plain dicts so the FastAPI endpoints can serialize them
directly without dealing with sqlite3.Row objects.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .models import ACTIVE_TASK_STATUSES


# Resolved at import time so the path is stable regardless of cwd at request
# time. The path matches the docker-compose volume mount (./data:/app/data).
DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "devin_remediation.db",
)


def now_iso() -> str:
    """ISO-8601 UTC timestamp suitable for storing as TEXT in SQLite."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    # Each call opens a short-lived connection. SQLite handles this fine for
    # the request rates this service is designed for, and it sidesteps the
    # "SQLite objects created in a thread can only be used in that same
    # thread" pitfall under FastAPI's threadpool.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they do not already exist. Safe to call repeatedly."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                action TEXT,
                issue_number INTEGER,
                eligible INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_number INTEGER NOT NULL,
                issue_title TEXT NOT NULL,
                issue_url TEXT NOT NULL,
                repo_url TEXT NOT NULL,
                base_branch TEXT,
                issue_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                devin_session_id TEXT,
                devin_session_url TEXT,
                pr_url TEXT,
                failure_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            """
        )


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [{key: row[key] for key in row.keys()} for row in rows]


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #

def record_event(
    event_type: str,
    action: Optional[str],
    issue_number: Optional[int],
    eligible: bool,
    reason: Optional[str] = None,
) -> dict:
    """Append a row to the events table.

    `eligible` answers: did the service actually act on this event?
    `reason` documents why it did or did not.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO events (event_type, action, issue_number, eligible, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_type, action, issue_number, 1 if eligible else 0, reason, now_iso()),
        )
        event_id = cur.lastrowid
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def list_events() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC").fetchall()
    return _rows_to_dicts(rows)


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #

def create_task(
    issue_number: int,
    issue_title: str,
    issue_url: str,
    repo_url: str,
    base_branch: Optional[str],
    issue_kind: str,
    status: str,
) -> dict:
    ts = now_iso()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (
                issue_number, issue_title, issue_url, repo_url, base_branch,
                issue_kind, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue_number,
                issue_title,
                issue_url,
                repo_url,
                base_branch,
                issue_kind,
                status,
                ts,
                ts,
            ),
        )
        task_id = cur.lastrowid
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


# Columns that update_task is allowed to modify. Guarding against arbitrary
# kwargs keeps this function safe from typos sneaking into raw SQL.
UPDATABLE_TASK_FIELDS = {
    "issue_title",
    "issue_url",
    "repo_url",
    "base_branch",
    "issue_kind",
    "status",
    "devin_session_id",
    "devin_session_url",
    "pr_url",
    "failure_reason",
    "completed_at",
}


def update_task(task_id: int, **fields: Any) -> Optional[dict]:
    bad = set(fields) - UPDATABLE_TASK_FIELDS
    if bad:
        raise ValueError(f"Unknown task fields: {sorted(bad)}")
    if not fields:
        return get_task(task_id)

    fields["updated_at"] = now_iso()
    columns = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [task_id]
    with _connect() as conn:
        conn.execute(f"UPDATE tasks SET {columns} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_dict(row)


def get_task(task_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_dict(row)


def list_tasks() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    return _rows_to_dicts(rows)


def find_active_task_for_issue(issue_number: int) -> Optional[dict]:
    """Idempotency guard.

    GitHub may deliver multiple events for the same issue (opened + labeled,
    or duplicate webhook deliveries). If a task already exists in an active
    status we return it instead of starting a second Devin session.
    """
    placeholders = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
    sql = (
        "SELECT * FROM tasks WHERE issue_number = ? "
        f"AND status IN ({placeholders}) "
        "ORDER BY id DESC LIMIT 1"
    )
    params = (issue_number, *ACTIVE_TASK_STATUSES)
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return _row_to_dict(row)
