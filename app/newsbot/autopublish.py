from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import connect
from .workflow import utc_now

LOGGER = logging.getLogger(__name__)
LOCAL_ZONE = ZoneInfo("Europe/Moscow")


def autopublish_enabled() -> bool:
    return os.getenv("NEWSBOT_AUTOPUBLISH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _daily_limits() -> tuple[int, int]:
    routine = int(os.getenv("NEWSBOT_AUTOPUBLISH_DAILY_ROUTINE_LIMIT", "2"))
    major = int(os.getenv("NEWSBOT_AUTOPUBLISH_DAILY_MAJOR_LIMIT", "4"))
    if not 0 <= routine <= 20 or not 0 <= major <= 20:
        raise ValueError("Autopublish daily limits must be between 0 and 20")
    return routine, major


def _today_window() -> tuple[str, str]:
    now = datetime.now(LOCAL_ZONE)
    start = datetime.combine(now.date(), time.min, tzinfo=LOCAL_ZONE).astimezone(timezone.utc)
    end = (start + timedelta(days=1))
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _db_path(db: sqlite3.Connection) -> Path | None:
    row = db.execute("PRAGMA database_list").fetchone()
    if not row or not row[2]:
        return None
    return Path(row[2])


def _publish_in_isolated_connection(db_path: str, event_id: int) -> None:
    from .publisher import publish_event

    connection = connect(Path(db_path))
    try:
        publish_event(connection, event_id)
    finally:
        connection.close()


async def run_autopublish_cycle(db: sqlite3.Connection) -> int:
    """Disabled by default; only publishes events that a human has explicitly approved."""
    if not autopublish_enabled():
        return 0
    routine_limit, major_limit = _daily_limits()
    start, end = _today_window()
    counts = {
        row["news_priority"]: int(row["count"])
        for row in db.execute(
            """SELECT news_priority,COUNT(*) AS count FROM events
               WHERE state='published' AND published_at>=? AND published_at<?
               GROUP BY news_priority""", (start, end)
        ).fetchall()
    }
    due = db.execute(
        """SELECT id,news_priority FROM events
           WHERE state IN ('approved','retry_wait')
             AND (state!='retry_wait' OR next_attempt_at IS NULL OR next_attempt_at<=?)
             AND ai_state='complete' AND relevance='football' AND importance_score>=10
             AND draft_text IS NOT NULL AND trim(draft_text)!=''
           ORDER BY CASE news_priority WHEN 'major_match' THEN 0 ELSE 1 END,created_at,id""",
        (utc_now(),),
    ).fetchall()
    db_path = _db_path(db)
    if db_path is None:
        LOGGER.error("autopublish enabled but SQLite database is not a file; no event was published")
        return 0
    sent = 0
    for row in due:
        priority = str(row["news_priority"])
        count = counts.get(priority, 0)
        limit = major_limit if priority == "major_match" else routine_limit
        if count >= limit:
            continue
        try:
            await asyncio.to_thread(_publish_in_isolated_connection, str(db_path), int(row["id"]))
            counts[priority] = count + 1
            sent += 1
        except Exception as exc:
            LOGGER.warning("autopublish stopped at event=%d error=%s", int(row["id"]), type(exc).__name__)
            break
    if sent:
        LOGGER.info("autopublish cycle published=%d (manual approvals only)", sent)
    return sent
