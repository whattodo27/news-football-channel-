from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import connect
from .workflow import approve_event, utc_now

LOGGER = logging.getLogger(__name__)
LOCAL_ZONE = ZoneInfo("Europe/Moscow")


def autopublish_enabled() -> bool:
    return os.getenv("NEWSBOT_AUTOPUBLISH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


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


async def _publish_in_isolated_connection(db_path: str, event_id: int) -> None:
    from .publisher import publish_event

    connection = connect(Path(db_path))
    try:
        await publish_event(connection, event_id)
    finally:
        connection.close()


def auto_approve_pending(db: sqlite3.Connection) -> int:
    """Approve every review-ready event in autonomous mode, respecting daily limits.

    Reuses the same gate as manual review (editorial readiness, independent
    confirmations, and the interesting_news daily limit), so over-quota or
    not-ready events are skipped rather than published.
    """
    rows = db.execute(
        """SELECT id FROM events WHERE state='awaiting_review'
           ORDER BY CASE publication_lane WHEN 'match_results' THEN 0 ELSE 1 END,
                    interest_score DESC, created_at ASC, id ASC"""
    ).fetchall()
    approved = 0
    for row in rows:
        try:
            approve_event(db, int(row["id"]))
            approved += 1
        except ValueError as exc:
            LOGGER.info("auto-approve skipped event=%s (%s)", row["id"], exc)
    if approved:
        LOGGER.info("auto-approved %d event(s) for publication", approved)
    return approved


async def run_autopublish_cycle(db: sqlite3.Connection) -> int:
    """Autonomous cycle: auto-approve review-ready events, then publish approved ones."""
    if not autopublish_enabled():
        return 0
    from .editorial import load_rules, match_result_eligible
    from .publisher import recover_stale_publishing

    recover_stale_publishing(db)
    auto_approve_pending(db)
    rules = load_rules()
    story_limit = int(rules["interesting_news"]["daily_limit"])
    start, end = _today_window()
    story_count = int(db.execute(
        """SELECT COUNT(*) FROM events WHERE publication_lane='interesting_news' AND state='published'
           AND published_at>=? AND published_at<?""", (start, end)
    ).fetchone()[0])
    due = db.execute(
        """SELECT id,publication_lane,match_competition FROM events
           WHERE state IN ('approved','retry_wait')
             AND (state!='retry_wait' OR next_attempt_at IS NULL OR next_attempt_at<=?)
             AND ai_state='complete' AND relevance='football'
             AND draft_text IS NOT NULL AND trim(draft_text)!=''
           ORDER BY CASE publication_lane WHEN 'match_results' THEN 0 ELSE 1 END,
                    interest_score DESC,created_at,id""",
        (utc_now(),),
    ).fetchall()
    db_path = _db_path(db)
    if db_path is None:
        LOGGER.error("autopublish enabled but SQLite database is not a file; no event was published")
        return 0
    sent = 0
    for row in due:
        lane = str(row["publication_lane"])
        if lane == "interesting_news" and story_count >= story_limit:
            continue
        if lane == "match_results" and not match_result_eligible(row["match_competition"], rules)[0]:
            continue
        try:
            await _publish_in_isolated_connection(str(db_path), int(row["id"]))
            if lane == "interesting_news": story_count += 1
            sent += 1
        except Exception as exc:
            LOGGER.warning("autopublish stopped at event=%d error=%s", int(row["id"]), type(exc).__name__)
            break
    if sent:
        LOGGER.info("autopublish cycle published=%d (manual approvals only)", sent)
    return sent
