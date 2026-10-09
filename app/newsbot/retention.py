from __future__ import annotations
import logging, os, re, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import generated_images_dir, source_images_dir

LOGGER = logging.getLogger(__name__)
TERMINAL = ("published", "failed", "skipped")
IMAGE_FILE = re.compile(r"event-(\d+)\.(?:jpe?g|png)$", re.IGNORECASE)


def _transient_image_dirs() -> tuple[Path, Path]:
    return source_images_dir(), generated_images_dir()

def retention_days() -> int:
    value = int(os.getenv("NEWSBOT_RAW_RETENTION_DAYS", "30"))
    if not 7 <= value <= 365:
        raise ValueError("NEWSBOT_RAW_RETENTION_DAYS must be between 7 and 365")
    return value

def _managed_event_image(path_value: str, event_id: int) -> Path | None:
    path = Path(path_value)
    if path.is_symlink() or path.name != f"event-{event_id}{path.suffix}":
        return None
    if not IMAGE_FILE.fullmatch(path.name):
        return None
    try:
        resolved = path.resolve(strict=False)
        roots = {root.resolve() for root in _transient_image_dirs()}
    except OSError:
        return None
    if resolved.parent not in roots:
        return None
    return resolved


def discard_event_image(db: sqlite3.Connection, event_id: int) -> bool:
    """Remove a post-specific local image after Telegram has accepted the post."""
    row = db.execute("SELECT image_path FROM events WHERE id=?", (event_id,)).fetchone()
    if not row or not row["image_path"]:
        return False
    path = _managed_event_image(str(row["image_path"]), event_id)
    if path is None:
        LOGGER.error("refusing to remove event image outside managed transient directories event=%d", event_id)
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning("could not remove published event image event=%d error=%s", event_id, type(exc).__name__)
        return False
    with db:
        db.execute("UPDATE events SET image_path=NULL WHERE id=? AND image_path=?", (event_id, row["image_path"]))
    LOGGER.info("removed local copy of published event image event=%d", event_id)
    return True


def cleanup_old_event_images(db: sqlite3.Connection) -> int:
    days = int(os.getenv("NEWSBOT_IMAGE_RETENTION_DAYS", "30"))
    if not 1 <= days <= 365:
        raise ValueError("NEWSBOT_IMAGE_RETENTION_DAYS must be between 1 and 365")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    marks = ",".join("?" for _ in TERMINAL)
    removed = 0
    rows = db.execute(
        f"SELECT id,image_path FROM events WHERE image_path IS NOT NULL AND state IN ({marks}) "
        "AND COALESCE(published_at,updated_at,created_at)<?",
        (*TERMINAL, cutoff),
    ).fetchall()
    for row in rows:
        path = _managed_event_image(str(row["image_path"]), int(row["id"]))
        if path is None:
            LOGGER.error("refusing to remove unmanaged event image event=%d", int(row["id"]))
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("could not remove stale event image event=%d error=%s", int(row["id"]), type(exc).__name__)
            continue
        with db:
            db.execute("UPDATE events SET image_path=NULL WHERE id=? AND image_path=?",
                       (int(row["id"]), row["image_path"]))
        removed += 1

    referenced = {str(row[0]) for row in db.execute("SELECT image_path FROM events WHERE image_path IS NOT NULL")}
    for root in _transient_image_dirs():
        try:
            root.mkdir(parents=True, exist_ok=True)
            candidates = list(root.iterdir())
        except OSError as exc:
            LOGGER.warning("could not inspect transient image directory path=%s error=%s", root, type(exc).__name__)
            continue
        for path in candidates:
            if not IMAGE_FILE.fullmatch(path.name) or path.is_symlink() or not path.is_file() or str(path) in referenced:
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified >= datetime.fromisoformat(cutoff):
                    continue
                path.unlink()
                removed += 1
            except OSError as exc:
                LOGGER.warning("could not remove orphan event image path=%s error=%s", path.name, type(exc).__name__)
    if removed:
        LOGGER.info("image retention removed %d obsolete transient file(s)", removed)
    return removed


def _purge_stale_headlines(db: sqlite3.Connection) -> int:
    """Drop cached card headlines for terminal events older than the headline TTL (3 days)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    marks = ",".join("?" for _ in TERMINAL)
    with db:
        cursor = db.execute(
            f"UPDATE events SET headline_text=NULL, headline_generated_at=NULL "
            f"WHERE (headline_text IS NOT NULL OR headline_generated_at IS NOT NULL) "
            f"AND state IN ({marks}) AND COALESCE(published_at,updated_at,created_at)<?",
            (*TERMINAL, cutoff),
        )
    return int(cursor.rowcount or 0)


def cleanup_old_content(db: sqlite3.Connection) -> int:
    """Drop copied RSS bodies from old terminal events; keep event and source audit metadata."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days())).isoformat(timespec="seconds")
    marks = ",".join("?" for _ in TERMINAL)
    eligible = f"""SELECT a.id FROM articles a
        JOIN event_articles ea ON ea.article_id=a.id JOIN events e ON e.id=ea.event_id
        WHERE a.first_seen_at<? AND e.state IN ({marks})
        AND (a.summary IS NOT NULL OR a.content IS NOT NULL OR EXISTS
             (SELECT 1 FROM article_sources ax WHERE ax.article_id=a.id AND ax.raw_entry_json NOT IN ('{{}}','')))"""
    with db:
        ids = [int(r[0]) for r in db.execute(eligible, (cutoff, *TERMINAL)).fetchall()]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            db.execute(f"UPDATE articles SET summary=NULL,content=NULL WHERE id IN ({placeholders})", ids)
            db.execute(f"UPDATE article_sources SET raw_entry_json='{{}}' WHERE article_id IN ({placeholders})", ids)
            db.execute("CREATE TABLE IF NOT EXISTS maintenance_state (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    cleanup_old_event_images(db)
    _purge_stale_headlines(db)
    if ids:
        _vacuum_if_due(db)
    LOGGER.info("retention cleanup cleared RSS bodies for %d old terminal article(s)",len(ids))
    return len(ids)

def _vacuum_if_due(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS maintenance_state (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    row = db.execute("SELECT value FROM maintenance_state WHERE key='last_vacuum_at'").fetchone()
    now = datetime.now(timezone.utc)
    if row:
        try:
            previous = datetime.fromisoformat(row[0])
            if now - previous < timedelta(days=7):
                return
        except ValueError:
            pass
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
        with db:
            db.execute("INSERT INTO maintenance_state(key,value) VALUES('last_vacuum_at',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (now.isoformat(timespec="seconds"),))
        LOGGER.info("SQLite compacted after retention cleanup")
    except sqlite3.OperationalError as exc:
        LOGGER.warning("SQLite compaction deferred: %s", type(exc).__name__)
