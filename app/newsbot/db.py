from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, publisher TEXT NOT NULL,
 independence_group TEXT NOT NULL, source_type TEXT NOT NULL, scope TEXT NOT NULL,
 language TEXT NOT NULL, trust_tier TEXT NOT NULL, source_priority INTEGER NOT NULL,
 image_ai_allowed INTEGER NOT NULL DEFAULT 0, image_reuse_allowed INTEGER NOT NULL DEFAULT 0,
 enabled INTEGER NOT NULL, last_attempt_at TEXT, last_success_at TEXT, last_state TEXT,
 last_http_status INTEGER, last_error TEXT, last_entry_count INTEGER NOT NULL DEFAULT 0,
 total_entries_seen INTEGER NOT NULL DEFAULT 0, total_new_articles INTEGER NOT NULL DEFAULT 0,
 total_duplicates INTEGER NOT NULL DEFAULT 0, consecutive_failures INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT, etag TEXT, last_modified TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
 id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, link TEXT,
 summary TEXT, content TEXT, processing_state TEXT NOT NULL DEFAULT 'collected',
 state_reason TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
 published_at TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS article_sources (
 article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(id), source_guid TEXT NOT NULL, source_url TEXT,
 fetched_at TEXT NOT NULL, raw_entry_json TEXT NOT NULL,
 PRIMARY KEY (article_id, source_id, source_guid)
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_article_sources_source ON article_sources(source_id, fetched_at);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 event_date TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'awaiting_verification',
 state_reason TEXT, confirmation_count INTEGER NOT NULL DEFAULT 0,
 news_priority TEXT NOT NULL DEFAULT 'routine', official_statement_verified INTEGER NOT NULL DEFAULT 0,
 relevance TEXT NOT NULL DEFAULT 'uncertain', relevance_score REAL,
 importance TEXT NOT NULL DEFAULT 'routine', importance_score INTEGER, interest_score INTEGER,
 publication_lane TEXT NOT NULL DEFAULT 'interesting_news', approved_at TEXT,
 draft_text TEXT, ai_state TEXT NOT NULL DEFAULT 'pending',
 ai_reason TEXT, ai_model TEXT, analyzed_at TEXT, ai_attempts INTEGER NOT NULL DEFAULT 0,
 ai_next_attempt_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, published_at TEXT,
 telegram_chat_id TEXT, telegram_message_id TEXT, image_path TEXT, image_prompt TEXT, image_generated_at TEXT,
 image_url TEXT, image_origin TEXT, image_page_url TEXT, image_license TEXT, image_credit TEXT,
 image_description TEXT, image_selected_at TEXT, image_selection_reason TEXT, image_selection_model TEXT,
 match_content_type TEXT NOT NULL DEFAULT 'news', match_home_team TEXT, match_away_team TEXT,
 match_home_key TEXT, match_away_key TEXT, match_home_score INTEGER, match_away_score INTEGER,
 match_competition TEXT, match_status TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK (state IN ('awaiting_verification','awaiting_review','approved','retry_wait','publishing','published','failed','skipped')),
 CHECK (news_priority IN ('routine','major_match'))
);
CREATE TABLE IF NOT EXISTS event_articles (
 event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 article_id INTEGER NOT NULL UNIQUE REFERENCES articles(id) ON DELETE CASCADE,
 PRIMARY KEY (event_id, article_id)
);
CREATE TABLE IF NOT EXISTS media_assets (
 id INTEGER PRIMARY KEY,
 asset_type TEXT NOT NULL CHECK(asset_type IN ('background','crest')),
 asset_key TEXT,
 name TEXT NOT NULL,
 file_path TEXT NOT NULL UNIQUE,
 mime_type TEXT NOT NULL CHECK(mime_type IN ('image/jpeg','image/png')),
 sha256 TEXT NOT NULL UNIQUE,
 source_url TEXT,
 license TEXT NOT NULL,
 credit TEXT,
 created_at TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_assets_lookup ON media_assets(asset_type,asset_key,enabled);
CREATE TABLE IF NOT EXISTS country_flags (
 country_code TEXT PRIMARY KEY,
 name TEXT NOT NULL,
 file_path TEXT NOT NULL UNIQUE,
 mime_type TEXT NOT NULL CHECK(mime_type IN ('image/jpeg','image/png')),
 sha256 TEXT NOT NULL UNIQUE,
 source_url TEXT,
 license TEXT NOT NULL,
 credit TEXT,
 created_at TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS country_flag_aliases (
 id INTEGER PRIMARY KEY,
 country_code TEXT NOT NULL REFERENCES country_flags(country_code) ON DELETE CASCADE,
 alias TEXT NOT NULL,
 alias_norm TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_country_flag_lookup ON country_flags(enabled,country_code);

CREATE TABLE IF NOT EXISTS media_asset_aliases (
 id INTEGER PRIMARY KEY,
 asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
 alias TEXT NOT NULL,
 alias_norm TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_state ON events(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_events_published ON events(published_at, news_priority);
"""

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA secure_delete=ON")
    connection.executescript(SCHEMA)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(sources)")}
    if "next_attempt_at" not in columns:
        connection.execute("ALTER TABLE sources ADD COLUMN next_attempt_at TEXT")
    if "image_ai_allowed" not in columns:
        connection.execute("ALTER TABLE sources ADD COLUMN image_ai_allowed INTEGER NOT NULL DEFAULT 0")
    if "image_reuse_allowed" not in columns:
        connection.execute("ALTER TABLE sources ADD COLUMN image_reuse_allowed INTEGER NOT NULL DEFAULT 0")
    event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
    migrations = {
        "relevance": "TEXT NOT NULL DEFAULT 'uncertain'",
        "relevance_score": "REAL",
        "importance": "TEXT NOT NULL DEFAULT 'routine'",
        "importance_score": "INTEGER",
        "interest_score": "INTEGER",
        "publication_lane": "TEXT NOT NULL DEFAULT 'interesting_news'",
        "approved_at": "TEXT",
        "review_notified_at": "TEXT",
        "review_message_id": "TEXT",
        "draft_text": "TEXT",
        "ai_state": "TEXT NOT NULL DEFAULT 'pending'",
        "ai_reason": "TEXT",
        "ai_model": "TEXT",
        "analyzed_at": "TEXT",
        "ai_attempts": "INTEGER NOT NULL DEFAULT 0",
        "ai_next_attempt_at": "TEXT",
        "telegram_chat_id": "TEXT",
        "telegram_message_id": "TEXT",
        "image_path": "TEXT",
        "image_prompt": "TEXT",
        "image_generated_at": "TEXT",
        "image_url": "TEXT", "image_origin": "TEXT", "image_page_url": "TEXT",
        "image_license": "TEXT", "image_credit": "TEXT", "image_description": "TEXT",
        "image_selected_at": "TEXT", "image_selection_reason": "TEXT", "image_selection_model": "TEXT",
        "match_content_type": "TEXT NOT NULL DEFAULT 'news'",
        "match_home_team": "TEXT", "match_away_team": "TEXT", "match_home_key": "TEXT",
        "match_away_key": "TEXT", "match_home_score": "INTEGER", "match_away_score": "INTEGER",
        "match_competition": "TEXT", "match_status": "TEXT",
    }
    for name, definition in migrations.items():
        if name not in event_columns:
            connection.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
    connection.execute("UPDATE events SET publication_lane='match_results' WHERE match_content_type='match_result' AND publication_lane!='match_results'")
    connection.execute("UPDATE events SET state='awaiting_verification',state_reason='Требуется новая редакционная оценка',ai_state='pending',ai_next_attempt_at=NULL WHERE ai_state='complete' AND interest_score IS NULL AND match_content_type!='match_result' AND state IN ('awaiting_verification','awaiting_review','approved','retry_wait')")
    return connection

def sync_sources(connection: sqlite3.Connection, sources: list[dict[str, Any]], now: str) -> None:
    with connection:
        for source in sources:
            connection.execute("""
                INSERT INTO sources (
                    id,name,url,publisher,independence_group,source_type,scope,language,
                    trust_tier,source_priority,image_ai_allowed,image_reuse_allowed,enabled,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,
                    publisher=excluded.publisher,independence_group=excluded.independence_group,
                    source_type=excluded.source_type,scope=excluded.scope,language=excluded.language,
                    trust_tier=excluded.trust_tier,source_priority=excluded.source_priority,
                    image_ai_allowed=excluded.image_ai_allowed,image_reuse_allowed=excluded.image_reuse_allowed,
                    enabled=excluded.enabled,updated_at=excluded.updated_at
            """, (source["id"],source["name"],source["url"],source["publisher"],
                source["independence_group"],source["source_type"],source["scope"],
                source["language"],source["trust_tier"],int(source["source_priority"]),
                int(bool(source.get("image_ai_allowed"))),int(bool(source.get("image_reuse_allowed"))),
                int(bool(source["enabled"])),now))

def conditional_headers(connection: sqlite3.Connection, source_id: str) -> dict[str, str]:
    row = connection.execute("SELECT etag,last_modified FROM sources WHERE id=?", (source_id,)).fetchone()
    headers = {}
    if row and row["etag"]: headers["If-None-Match"] = row["etag"]
    if row and row["last_modified"]: headers["If-Modified-Since"] = row["last_modified"]
    return headers

def record_source_result(connection: sqlite3.Connection, source_id: str, *, attempted_at: str,
                         state: str, http_status: int | None, error: str | None, entries: int,
                         new_articles: int, duplicates: int, etag: str | None,
                         last_modified: str | None, retry_after_seconds: int | None = None) -> None:
    from datetime import datetime, timedelta, timezone
    successful = state in {"ok", "ok_with_parse_warning", "not_modified"}
    row = connection.execute("SELECT consecutive_failures FROM sources WHERE id=?", (source_id,)).fetchone()
    failures = 0 if successful else (int(row["consecutive_failures"]) + 1 if row else 1)
    retry_at = None
    if not successful:
        backoff = min(86400, 300 * (2 ** min(failures - 1, 8)))
        delay = max(backoff, retry_after_seconds or 0)
        retry_at = (datetime.fromisoformat(attempted_at) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    with connection:
        connection.execute("""
            UPDATE sources SET last_attempt_at=?,
                last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
                last_state=?,last_http_status=?,last_error=?,last_entry_count=?,
                total_entries_seen=total_entries_seen+?,total_new_articles=total_new_articles+?,
                total_duplicates=total_duplicates+?, consecutive_failures=?, next_attempt_at=?,
                etag=COALESCE(?,etag),last_modified=COALESCE(?,last_modified)
            WHERE id=?
        """, (attempted_at, int(successful), attempted_at, state, http_status, error,
              entries, entries, new_articles, duplicates, failures, retry_at, etag, last_modified, source_id))
