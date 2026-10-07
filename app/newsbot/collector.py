from __future__ import annotations
import asyncio, calendar, hashlib, json, logging, re, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import feedparser, httpx
from .db import conditional_headers, record_source_result
from .workflow import index_article
from .intelligence import analyze_pending
from .autopublish import run_autopublish_cycle
from .retention import cleanup_old_content

LOGGER = logging.getLogger(__name__)
USER_AGENT = "NewsBot-RSS-Collector/1.0 (contact: mailto:galaxtv1@gmail.com)"
TRACKING_KEYS = {"fbclid","gclid","mc_cid","mc_eid","ref","source"}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def canonical_url(value: str) -> str:
    value = unescape(value.strip())
    if not value: return ""
    try: parts = urlsplit(value)
    except ValueError: return value
    query = [(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True)
             if k.lower() not in TRACKING_KEYS and not k.lower().startswith("utm_")]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(),parts.netloc.lower(),path,urlencode(query),""))

def normalized_text(value: str) -> str:
    return re.sub(r"\s+"," ",unescape(value).lower()).strip()

def entry_published(entry: dict[str, Any]) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try: return datetime.fromtimestamp(calendar.timegm(parsed),timezone.utc).isoformat(timespec="seconds")
        except (OverflowError,OSError,TypeError,ValueError): pass
    return entry.get("published") or entry.get("updated")

def make_dedupe_key(entry: dict[str, Any]) -> str:
    link = canonical_url(str(entry.get("link","")))
    if link: return "url:"+hashlib.sha256(link.encode()).hexdigest()
    guid = normalized_text(str(entry.get("id") or entry.get("guid") or ""))
    if guid: return "guid:"+hashlib.sha256(guid.encode()).hexdigest()
    title = normalized_text(str(entry.get("title","")))
    day = (entry_published(entry) or "")[:10]
    return "text:"+hashlib.sha256(f"{title}|{day}".encode()).hexdigest()

def entry_text(entry: dict[str, Any], field: str) -> str | None:
    value = entry.get(field)
    if value is None: return None
    if isinstance(value,list):
        value = value[0] if value else ""
        if isinstance(value,dict): value = value.get("value","")
    if isinstance(value,dict): value = value.get("value","")
    return str(value).strip() or None

def json_default(value: Any) -> str:
    return value.isoformat() if hasattr(value,"isoformat") else str(value)

def parse_retry_after(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, int((retry_at - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None

def save_entries(connection: Any, source: dict[str, Any], entries: list[dict[str, Any]],
                 fetched_at: str) -> tuple[int,int]:
    new_articles = duplicates = 0
    with connection:
        for entry in entries:
            title = entry_text(entry,"title") or "(untitled)"
            link = entry_text(entry,"link")
            summary = entry_text(entry,"summary") or entry_text(entry,"description")
            content = entry_text(entry,"content") or summary
            key = make_dedupe_key(entry)
            before = connection.total_changes
            connection.execute("""
                INSERT OR IGNORE INTO articles
                    (dedupe_key,title,link,summary,content,published_at,first_seen_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?,?)
            """,(key,title,link,summary,content,entry_published(entry),fetched_at,fetched_at))
            inserted = connection.total_changes > before
            row = connection.execute("SELECT id FROM articles WHERE dedupe_key=?",(key,)).fetchone()
            article_id = int(row["id"])
            if inserted: new_articles += 1
            else:
                duplicates += 1
                connection.execute("""UPDATE articles SET last_seen_at=?,
                    summary=COALESCE(?,summary),content=COALESCE(?,content),
                    published_at=COALESCE(?,published_at) WHERE id=?""",
                    (fetched_at,summary,content,entry_published(entry),article_id))
            guid = entry_text(entry,"id") or entry_text(entry,"guid") or link or key
            raw = json.dumps(entry,ensure_ascii=False,default=json_default,sort_keys=True)
            connection.execute("""
                INSERT INTO article_sources
                    (article_id,source_id,source_guid,source_url,fetched_at,raw_entry_json)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(article_id,source_id,source_guid) DO UPDATE SET
                    source_url=excluded.source_url,fetched_at=excluded.fetched_at,
                    raw_entry_json=excluded.raw_entry_json
            """,(article_id,source["id"],guid,link,fetched_at,raw))
            index_article(connection, article_id)
    return new_articles,duplicates

async def collect_one(client: httpx.AsyncClient, semaphore: asyncio.Semaphore,
                      connection: Any, source: dict[str, Any]) -> dict[str, Any]:
    async with semaphore:
        attempted_at = utc_now()
        retry_due = connection.execute("SELECT next_attempt_at FROM sources WHERE id=?", (source["id"],)).fetchone()
        if retry_due and retry_due["next_attempt_at"] and retry_due["next_attempt_at"] > attempted_at:
            LOGGER.info("source=%s deferred until %s after prior failure", source["id"], retry_due["next_attempt_at"])
            return {"state":"deferred","entries":0,"new":0,"duplicates":0}
        response = None
        retry_after_seconds = None
        entries, new_articles, duplicates = [], 0, 0
        state, error = "network_error", None
        try:
            response = await client.get(source["url"],headers=conditional_headers(connection,source["id"]))
            if response.status_code == 304: state = "not_modified"
            else:
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                entries = list(parsed.entries)
                if not entries or not (parsed.version or parsed.feed):
                    state,error = "not_a_feed","Response did not contain parseable feed entries"
                else:
                    new_articles,duplicates = save_entries(connection,source,entries,attempted_at)
                    state = "ok_with_parse_warning" if parsed.bozo else "ok"
                    if parsed.bozo: error = f"Recoverable feed parse warning: {parsed.bozo_exception}"[:500]
        except httpx.HTTPStatusError as exc:
            response,state,error = exc.response,"http_error",f"HTTP {exc.response.status_code}"
            if exc.response.status_code in {429, 503}:
                retry_after_seconds = parse_retry_after(exc.response.headers.get("Retry-After"), datetime.now(timezone.utc))
        except httpx.TimeoutException as exc: state,error = "timeout",f"{type(exc).__name__}: request timed out"
        except httpx.TransportError as exc: state,error = "transport_error",f"{type(exc).__name__}: {exc}"[:500]
        except Exception as exc: state,error = "collector_error",f"{type(exc).__name__}: {exc}"[:500]
        record_source_result(connection,source["id"],attempted_at=attempted_at,state=state,
            http_status=response.status_code if response is not None else None,error=error,
            entries=len(entries),new_articles=new_articles,duplicates=duplicates,
            etag=response.headers.get("ETag") if response is not None else None,
            last_modified=response.headers.get("Last-Modified") if response is not None else None,
            retry_after_seconds=retry_after_seconds)
        LOGGER.info("source=%s state=%s http=%s entries=%d new=%d duplicates=%d error=%s",
            source["id"],state,response.status_code if response is not None else "-",
            len(entries),new_articles,duplicates,error or "-")
        return {"state":state,"entries":len(entries),"new":new_articles,"duplicates":duplicates}

async def collect_cycle(client: httpx.AsyncClient, connection: Any,
                        sources: list[dict[str, Any]], spacing_seconds: float = 0) -> dict[str,int]:
    enabled = [s for s in sources if s.get("enabled") and s.get("scope")=="association_football"]
    skipped = sum(bool(s.get("enabled")) for s in sources)-len(enabled)
    if skipped: LOGGER.warning("Skipped %d enabled source(s) outside association_football scope",skipped)
    if not enabled:
        LOGGER.warning("No enabled association-football sources configured")
        return {"sources":0,"ok":0,"failed":0,"entries":0,"new":0,"duplicates":0}
    semaphore = asyncio.Semaphore(1)
    results = []
    for index, source in enumerate(enabled):
        results.append(await collect_one(client,semaphore,connection,source))
        if spacing_seconds and index < len(enabled) - 1:
            await asyncio.sleep(spacing_seconds)
    totals = {"sources":len(results),
        "ok":sum(r["state"] in {"ok","ok_with_parse_warning","not_modified"} for r in results),
        "failed":sum(r["state"] not in {"ok","ok_with_parse_warning","not_modified","deferred"} for r in results),
        "deferred":sum(r["state"] == "deferred" for r in results),
        "entries":sum(r["entries"] for r in results),"new":sum(r["new"] for r in results),
        "duplicates":sum(r["duplicates"] for r in results)}
    LOGGER.info("cycle summary sources=%d ok=%d failed=%d entries=%d new=%d duplicates=%d",
        totals["sources"],totals["ok"],totals["failed"],totals["entries"],totals["new"],totals["duplicates"])
    return totals

async def run_collector(connection: Any,sources: list[dict[str, Any]],poll_seconds: int,once: bool, db_path: Any = None) -> None:
    timeout = httpx.Timeout(25.0,connect=10.0)
    headers = {"User-Agent":USER_AGENT,
        "Accept":"application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5"}
    async with httpx.AsyncClient(timeout=timeout,follow_redirects=True,headers=headers) as client:
        while True:
            started = time.monotonic()
            enabled_count = sum(bool(s.get("enabled")) and s.get("scope")=="association_football" for s in sources)
            spacing = poll_seconds / enabled_count if enabled_count > 1 and not once else 0
            await collect_cycle(client,connection,sources,spacing_seconds=spacing)
            await analyze_pending(connection)
            if not once and db_path is not None:
                from .telegram_review import notify_review_queue
                await notify_review_queue(connection, db_path)
            await run_autopublish_cycle(connection)
            cleanup_old_content(connection)
            if once: return
            delay = max(1,poll_seconds-int(time.monotonic()-started))
            LOGGER.info("next collection cycle in %d seconds",delay)
            await asyncio.sleep(delay)
