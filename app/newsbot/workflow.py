from __future__ import annotations
import argparse, asyncio, hashlib, json, re, sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

TIERS = ("official", "established", "specialist")
from .editorial import ensure_story_approval_slot, load_rules, match_result_eligible
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def event_day(value: str | None, fallback: str) -> str:
    parsed = None
    if value:
        try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try: parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError): pass
    if parsed:
        if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    return fallback[:10]

def event_key(title: str, day: str) -> str:
    text = re.sub(r"<[^>]*>", " ", unescape(title)).casefold()
    text = " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())
    return hashlib.sha256(f"{day}|{text}".encode()).hexdigest()

def editorial_review_ready(row: sqlite3.Row) -> bool:
    if row["ai_state"] != "complete" or row["relevance"] != "football" or float(row["relevance_score"] or 0) < 0.70:
        return False
    if row["publication_lane"] == "match_results":
        if row["match_content_type"] != "match_result" or row["match_status"] not in {"final", "extra_time"}:
            return False
        if not row["match_home_team"] or not row["match_away_team"] or not row["match_competition"] or row["match_home_score"] is None or row["match_away_score"] is None:
            return False
        return match_result_eligible(row["match_competition"], load_rules())[0]
    return int(row["interest_score"] or 0) >= int(load_rules()["interesting_news"]["min_interest_score"])

def editorial_failure_reason(row: sqlite3.Row) -> str:
    if row["ai_state"] != "complete":
        return "Ожидает AI-оценки футбольной релевантности и редакционного типа"
    if row["relevance"] != "football" or float(row["relevance_score"] or 0) < 0.70:
        return "Нужна ручная проверка футбольной релевантности"
    if row["publication_lane"] == "match_results":
        eligible, reason = match_result_eligible(row["match_competition"], load_rules())
        if not eligible:
            return reason or "Матч не подходит под активный режим турнира"
        return "Не подтверждены команды, турнир, счёт или статус завершённого матча"
    minimum = int(load_rules()["interesting_news"]["min_interest_score"])
    return f"Интересность ниже порога редакционной очереди {minimum}/10"

def refresh_confirmations(db: sqlite3.Connection, eid: int, stamp: str | None = None) -> int:
    count = int(db.execute("""SELECT COUNT(DISTINCT s.independence_group)
        FROM event_articles ea JOIN article_sources a ON a.article_id=ea.article_id
        JOIN sources s ON s.id=a.source_id
        WHERE ea.event_id=? AND s.trust_tier IN ('official','established','specialist')""", (eid,)).fetchone()[0])
    row = db.execute(
        "SELECT state,ai_state,relevance,relevance_score,interest_score,publication_lane,match_content_type,match_home_team,match_away_team,match_home_score,match_away_score,match_competition,match_status FROM events WHERE id=?",
        (eid,),
    ).fetchone()
    if row and row["state"] in {"awaiting_verification", "awaiting_review"}:
        ai_ready = editorial_review_ready(row)
        ready_for_review = count >= 2 and ai_ready
        state = "awaiting_review" if ready_for_review else "awaiting_verification"
        if ready_for_review:
            reason = None
        elif count < 2:
            reason = "Нужно подтверждение от независимого издателя"
        else:
            reason = editorial_failure_reason(row)
        with db:
            db.execute(
                "UPDATE events SET confirmation_count=?,state=?,state_reason=?,updated_at=? WHERE id=?",
                (count, state, reason, stamp or utc_now(), eid),
            )
    elif row:
        with db:
            db.execute("UPDATE events SET confirmation_count=?,updated_at=? WHERE id=?",
                       (count, stamp or utc_now(), eid))
    return count

def index_article(db: sqlite3.Connection, article_id: int) -> int:
    article = db.execute("SELECT title,published_at,first_seen_at FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article: raise ValueError(f"Article {article_id} does not exist")
    day = event_day(article["published_at"], article["first_seen_at"])
    key, now = event_key(article["title"], day), utc_now()
    with db:
        db.execute("INSERT OR IGNORE INTO events(event_key,title,event_date,created_at,updated_at) VALUES(?,?,?,?,?)",
                   (key, article["title"], day, now, now))
        eid = int(db.execute("SELECT id FROM events WHERE event_key=?", (key,)).fetchone()[0])
        db.execute("INSERT OR IGNORE INTO event_articles(event_id,article_id) VALUES(?,?)", (eid, article_id))
        refresh_confirmations(db, eid, now)
    return eid

def repair_review_queue(db: sqlite3.Connection) -> int:
    """Reconcile editorial queue states with the current score and tournament mode."""
    rows = db.execute("SELECT * FROM events WHERE state IN ('awaiting_review','awaiting_verification') AND ai_state='complete'").fetchall()
    repaired = 0
    rules = load_rules()
    with db:
        for row in rows:
            ready = int(row["confirmation_count"] or 0) >= 2 and editorial_review_ready(row)
            target_state = "awaiting_review" if ready else "awaiting_verification"
            if ready:
                reason = None
            elif int(row["confirmation_count"] or 0) < 2:
                reason = "Нужно подтверждение от независимого издателя"
            else:
                reason = editorial_failure_reason(row)
            if row["state"] == target_state and row["state_reason"] == reason:
                continue
            db.execute("UPDATE events SET state=?,state_reason=?,updated_at=? WHERE id=?",
                       (target_state, reason, utc_now(), int(row["id"])))
            repaired += 1
    return repaired

def index_untracked_articles(db: sqlite3.Connection) -> int:
    repair_review_queue(db)
    ids = db.execute("SELECT a.id FROM articles a LEFT JOIN event_articles ea ON ea.article_id=a.id WHERE ea.article_id IS NULL ORDER BY a.id").fetchall()
    for row in ids: index_article(db, int(row[0]))
    return len(ids)

def list_events(db: sqlite3.Connection, state: str | None = None,
                lane: str | None = None) -> list[sqlite3.Row]:
    query = """SELECT e.*,COUNT(ea.article_id) AS article_count,GROUP_CONCAT(DISTINCT s.publisher) AS publishers
        FROM events e LEFT JOIN event_articles ea ON ea.event_id=e.id
        LEFT JOIN article_sources a ON a.article_id=ea.article_id LEFT JOIN sources s ON s.id=a.source_id"""
    clauses, params = [], []
    if state:
        clauses.append("e.state=?")
        params.append(state)
    if lane:
        clauses.append("e.publication_lane=?")
        params.append(lane)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " GROUP BY e.id ORDER BY CASE e.publication_lane WHEN 'match_results' THEN 0 ELSE 1 END, e.interest_score DESC, e.created_at DESC"
    return db.execute(query, params).fetchall()

def approve_event(db: sqlite3.Connection, eid: int, priority: str | None = None, official_statement: bool = False) -> None:
    if priority is not None and priority not in {"routine", "major_match"}:
        raise ValueError("Priority must be routine or major_match")
    row = db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not row: raise ValueError(f"Event {eid} does not exist")
    if row["state"] not in {"awaiting_review", "awaiting_verification"}:
        raise ValueError(f"Event is in state {row['state']}; only unreviewed events can be approved")
    if not editorial_review_ready(row):
        if row["publication_lane"] == "match_results":
            raise ValueError("Match result is not fully verified or is outside the active tournament mode")
        minimum = int(load_rules()["interesting_news"]["min_interest_score"])
        raise ValueError(f"News interest is below the {minimum}/10 publication threshold")
    priority = priority or row["news_priority"]
    official = db.execute("""SELECT 1 FROM event_articles ea JOIN article_sources a ON a.article_id=ea.article_id
        JOIN sources s ON s.id=a.source_id WHERE ea.event_id=? AND s.source_type='official' LIMIT 1""", (eid,)).fetchone()
    if int(row["confirmation_count"]) < 2 and not (official_statement and official):
        raise ValueError("Need two independent publishers, or explicitly verify the organization's own official statement")
    if row["publication_lane"] == "interesting_news":
        ensure_story_approval_slot(db)
    with db:
        db.execute("""UPDATE events SET state='approved',state_reason=NULL,news_priority=?,
            official_statement_verified=?,approved_at=?,updated_at=? WHERE id=?""",
            (priority, int(official_statement), utc_now(), utc_now(), eid))

def skip_event(db: sqlite3.Connection, eid: int, reason: str) -> None:
    reason = reason.strip()
    if not reason: raise ValueError("A skip reason is required")
    row = db.execute("SELECT state FROM events WHERE id=?", (eid,)).fetchone()
    if not row: raise ValueError(f"Event {eid} does not exist")
    if row["state"] in {"published", "failed", "skipped"}:
        raise ValueError(f"Event is already in terminal state {row['state']}")
    with db:
        db.execute("UPDATE events SET state='skipped',state_reason=?,updated_at=? WHERE id=?", (reason, utc_now(), eid))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and review collected football news events.")
    parser.add_argument("--db", default="/data/newsbot.sqlite3")
    parser.add_argument("--state", help="Filter list by event state")
    parser.add_argument("--lane", choices=("interesting_news", "match_results"), help="Filter by editorial queue")
    parser.add_argument("--approve", type=int, metavar="EVENT_ID")
    parser.add_argument("--priority", choices=("routine", "major_match"),
                        help="Override the AI-assessed event priority")
    parser.add_argument("--analyze", action="store_true", help="Run AI assessment now for pending events")
    parser.add_argument("--publish", type=int, metavar="EVENT_ID", help="Publish one manually approved event to Telegram")
    parser.add_argument("--preview", type=int, metavar="EVENT_ID", help="Preview the Telegram post without sending it")
    parser.add_argument("--review-scorecard", type=int, metavar="EVENT_ID", help="Regenerate a result card and review its visual design with the vision model")
    parser.add_argument("--select-image", type=int, metavar="EVENT_ID", help="Find and select a permitted photo for an approved event")
    parser.add_argument("--official-statement", action="store_true",
                        help="Confirm an organization's own statement from an official source")
    parser.add_argument("--skip", type=int, metavar="EVENT_ID")
    parser.add_argument("--reason", help="Required reason when skipping an event")
    args = parser.parse_args()
    from pathlib import Path
    from .db import connect
    db = connect(Path(args.db))
    try:
        index_untracked_articles(db)
        if args.analyze:
            from .intelligence import analyze_pending
            analyzed, merged = asyncio.run(analyze_pending(db, force=True))
            print(f"AI обработал событий: {analyzed}; объединено: {merged}")
        elif args.select_image is not None:
            from .image_selection import select_event_image
            print(f"Изображение выбрано: {select_event_image(db, args.select_image)}")
        elif args.review_scorecard is not None:
            from .scorecard import generate_scorecard, review_scorecard
            image_path = generate_scorecard(db, args.review_scorecard, force=True)
            print(json.dumps(review_scorecard(image_path), ensure_ascii=False, indent=2))
        elif args.preview is not None:
            from .publisher import preview_event
            print(preview_event(db, args.preview))
            row = db.execute("SELECT match_content_type,image_origin,image_path FROM events WHERE id=?", (args.preview,)).fetchone()
            if row and row["image_path"] and (row["match_content_type"] == "match_result" or row["image_origin"] == "news_card"):
                from .scorecard import review_scorecard
                try:
                    review = review_scorecard(row["image_path"])
                    print("\nVision-проверка оформления:")
                    print(json.dumps(review, ensure_ascii=False, indent=2))
                except ValueError as exc:
                    print(f"Vision-проверка не выполнена: {exc}")
        elif args.publish is not None:
            from .publisher import publish_event
            publish_event(db, args.publish)
        elif args.approve is not None:
            approve_event(db, args.approve, args.priority, args.official_statement)
            print(f"Approved event {args.approve} ({args.priority})")
        elif args.skip is not None:
            skip_event(db, args.skip, args.reason or "")
            print(f"Skipped event {args.skip}")
        else:
            rows = list_events(db, args.state, args.lane)
            for row in rows:
                score = "-" if row["relevance_score"] is None else f"{row['relevance_score']:.2f}"
                imp = "-" if row["importance_score"] is None else f"{row['importance_score']}/10"
                print(f"{row['id']}\t{row['state']}\t{row['ai_state']}\t{row['relevance']}:{score}\t"
                      f"importance={imp} ({row['importance']})/{row['news_priority']}\tconfirmations={row['confirmation_count']}\t"
                      f"{row['publishers'] or '-'}\tlane={row['publication_lane']} interest={row['interest_score'] if row['interest_score'] is not None else '-'}/10\t{row['title']}")
                if row["draft_text"]: print(f"  Черновик: {row['draft_text']}")
                if row["state_reason"]: print(f"  Причина: {row['state_reason']}")
            if not rows: print("Очередь новостей пока пуста.")
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        db.close()

if __name__ == "__main__": main()
