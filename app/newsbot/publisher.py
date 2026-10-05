from __future__ import annotations
import asyncio, html, logging, mimetypes, os, re, sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
import httpx
from .workflow import utc_now

LOGGER = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/"
MAX_TEXT = 4096

def _post_content(db: sqlite3.Connection, event_id: int, *, require_approved: bool = True, max_text: int = MAX_TEXT) -> str:
    row = db.execute("SELECT state,draft_text,ai_state,next_attempt_at,importance_score FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None: raise ValueError(f"Event {event_id} does not exist")
    if require_approved and row["state"] not in {"approved", "retry_wait"}: raise ValueError(f"Event must be approved or due for retry (state={row['state']})")
    if require_approved and row["state"] == "retry_wait" and row["next_attempt_at"] and row["next_attempt_at"] > utc_now():
        raise ValueError(f"Retry is scheduled for {row['next_attempt_at']}")
    if row["ai_state"] != "complete" or not (row["draft_text"] or "").strip(): raise ValueError("Event has no completed Russian draft")
    if require_approved and int(row["importance_score"] or 0) < 10: raise ValueError("Event importance is below the 10/10 publication threshold")
    # Source URLs remain internal provenance; public channel captions contain only the editorial copy.
    raw_draft = re.sub(r"https?://\S+|www\.\S+", "", str(row["draft_text"])).strip()
    escaped = html.escape(raw_draft)
    if len(escaped) > max_text:
        low, high = 0, len(raw_draft)
        while low < high:
            middle = (low + high + 1) // 2
            if len(html.escape(raw_draft[:middle].rstrip())) <= max_text - 1:
                low = middle
            else:
                high = middle - 1
        escaped = html.escape(raw_draft[:low].rstrip()) + "…"
    return escaped

async def _send(text: str, token: str, chat_id: str, image_path: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0)) as client:
        base_url = API.format(token=token)
        if image_path:
            url = base_url + "sendPhoto"
            mime_type = mimetypes.guess_type(image_path)[0]
            if mime_type not in {"image/jpeg", "image/png"}:
                raise ValueError("Selected photo must be JPEG or PNG")
            with open(image_path, "rb") as image:
                response = await client.post(url, data={"chat_id":chat_id,"caption":text,"parse_mode":"HTML"},
                    files={"photo":(os.path.basename(image_path), image, mime_type)})
        else:
            url = base_url + "sendMessage"
            response = await client.post(url, json={"chat_id":chat_id,"text":text,
                "parse_mode":"HTML","disable_web_page_preview":True})
        response.raise_for_status()
        result = response.json()
    if not result.get("ok") or not isinstance(result.get("result"), dict): raise ValueError("Telegram did not confirm the message")
    return result["result"]

def publish_event(db: sqlite3.Connection, event_id: int) -> None:
    token, chat_id = os.getenv("BOT_TOKEN"), os.getenv("TELEGRAM_CHANNEL_ID")
    if not token: raise ValueError("BOT_TOKEN is not configured")
    if not chat_id: raise ValueError("TELEGRAM_CHANNEL_ID is not configured")
    _post_content(db, event_id)  # Validate approval and the Russian draft before requesting source-photo selection.
    image_row = db.execute(
        "SELECT image_path,image_origin,image_generated_at,match_content_type,match_status FROM events WHERE id=?",
        (event_id,),
    ).fetchone()
    image_path = image_row["image_path"] if image_row else None
    if image_row and image_row["match_content_type"] == "match_result":
        from .scorecard import generate_scorecard
        image_path = generate_scorecard(db, event_id)
    else:
        valid_source_photo = bool(image_row and image_row["image_origin"] and not image_row["image_generated_at"])
        if not valid_source_photo or not image_path or not os.path.isfile(image_path):
            from .image_selection import select_event_image
            image_path = select_event_image(db, event_id, force=not valid_source_photo)
    text = _post_content(db, event_id, max_text=1024)
    prior = db.execute("SELECT attempt_count FROM events WHERE id=?", (event_id,)).fetchone()
    with db:
        db.execute("UPDATE events SET state='publishing',state_reason=NULL,attempt_count=attempt_count+1,updated_at=? WHERE id=?", (utc_now(), event_id))
    try:
        result = asyncio.run(_send(text, token, chat_id, image_path))
    except httpx.HTTPStatusError as exc:
        attempts = int(prior["attempt_count"] or 0) + 1
        state, next_at = "failed", None
        if attempts < 5:
            try: delay = max(60, int(exc.response.headers.get("Retry-After", "300")))
            except ValueError: delay = 300 * 2 ** (attempts - 1)
            state, next_at = "retry_wait", (datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat(timespec="seconds")
        with db:
            db.execute("UPDATE events SET state=?,state_reason=?,next_attempt_at=?,updated_at=? WHERE id=?",
                       (state, f"Telegram HTTP {exc.response.status_code}", next_at, utc_now(), event_id))
        raise ValueError(f"Telegram returned HTTP {exc.response.status_code}; event state={state}") from None
    except httpx.TransportError as exc:
        # A lost response has an ambiguous delivery result; avoid accidental duplicate posts.
        with db:
            db.execute("UPDATE events SET state='publishing',state_reason='Ответ Telegram потерян; проверьте канал перед повтором',updated_at=? WHERE id=?", (utc_now(), event_id))
        LOGGER.error("Telegram delivery outcome uncertain for event=%d error=%s", event_id, type(exc).__name__)
        raise ValueError("Telegram response was lost; inspect the channel before retrying") from None
    except Exception as exc:
        with db:
            db.execute("UPDATE events SET state='approved',state_reason=?,updated_at=? WHERE id=?",
                       (f"Ошибка публикации: {type(exc).__name__}", utc_now(), event_id))
        raise ValueError(f"Telegram publication failed ({type(exc).__name__})") from None
    message_id = str(result.get("message_id", ""))
    returned_chat = str(result.get("chat", {}).get("id", chat_id))
    with db:
        db.execute("""UPDATE events SET state='published',state_reason=NULL,published_at=?,telegram_chat_id=?,
            telegram_message_id=?,next_attempt_at=NULL,updated_at=? WHERE id=?""",
            (utc_now(), returned_chat, message_id, utc_now(), event_id))
    LOGGER.info("Telegram published event=%d message_id=%s", event_id, message_id)
    from .retention import discard_event_image
    discard_event_image(db, event_id)
    print(f"Опубликовано событие {event_id}; сообщение Telegram {message_id}")


def preview_event(db: sqlite3.Connection, event_id: int) -> str:
    row = db.execute(
        """SELECT image_path,image_origin,image_generated_at,image_license,image_credit,image_description,
                  match_content_type FROM events WHERE id=?""", (event_id,)
    ).fetchone()
    if row and row["match_content_type"] == "match_result":
        from .scorecard import generate_scorecard
        image_path = generate_scorecard(db, event_id)
        row = db.execute(
            "SELECT image_path,image_origin,image_generated_at,image_license,image_credit,image_description,match_content_type "
            "FROM events WHERE id=?", (event_id,)
        ).fetchone()
    else:
        image_path = row["image_path"] if row else None
    has_image = bool(image_path and os.path.isfile(image_path))
    body = _post_content(db, event_id, require_approved=False, max_text=1024 if has_image else MAX_TEXT)
    if has_image:
        details = [f"Изображение: {row['image_description'] or row['image_origin'] or image_path}",
                   f"Файл: {image_path}"]
        if row["image_license"]: details.append(f"Лицензия: {row['image_license']}")
        if row["image_credit"]: details.append(f"Автор/кредит: {row['image_credit']}")
        return body + "\n\n" + "\n".join(details)
    return body + "\n\nИзображение ещё не выбрано. После одобрения запустите --select-image EVENT_ID."
