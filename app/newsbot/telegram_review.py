from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from .db import connect
from .workflow import approve_event, skip_event, utc_now

LOGGER = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_REVIEW_NOTIFICATIONS_PER_CYCLE = 10


def _api_url(token: str, method: str) -> str:
    return API.format(token=token, method=method)


async def _api(client: httpx.AsyncClient, token: str, method: str,
               payload: dict[str, Any] | None = None) -> Any:
    response = await client.post(_api_url(token, method), json=payload or {})
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
    return body.get("result")


def _prepare_event_image(db_path: str, event_id: int) -> str | None:
    db = connect(Path(db_path))
    try:
        row = db.execute(
            "SELECT match_content_type,image_path FROM events WHERE id=? AND state='awaiting_review'",
            (event_id,),
        ).fetchone()
        if not row:
            return None
        if row["image_path"] and Path(row["image_path"]).is_file():
            return str(row["image_path"])
        if row["match_content_type"] == "match_result":
            from .scorecard import generate_scorecard
            return generate_scorecard(db, event_id)
        from .image_selection import select_event_image
        return select_event_image(db, event_id)
    finally:
        db.close()


def _review_text(db: sqlite3.Connection, event_id: int) -> str:
    row = db.execute(
        """SELECT title,draft_text,publication_lane,interest_score,match_content_type,
                  match_home_team,match_away_team,match_home_score,match_away_score,
                  match_competition FROM events WHERE id=? AND state='awaiting_review'""",
        (event_id,),
    ).fetchone()
    if not row:
        raise ValueError("Событие больше не ожидает проверки")
    is_result = row["publication_lane"] == "match_results"
    kind = "Результат матча" if is_result else f"Интересная новость · {row['interest_score']}/10"
    parts = [f"<b>Проверка #{event_id} · {kind}</b>"]
    if is_result:
        parts.append(
            f"<b>{html.escape(str(row['match_home_team']))} "
            f"{row['match_home_score']}:{row['match_away_score']} "
            f"{html.escape(str(row['match_away_team']))}</b>\n"
            f"{html.escape(str(row['match_competition']))}"
        )
    else:
        parts.append(f"<b>{html.escape(str(row['title'] or ''))}</b>")
    if row["draft_text"]:
        parts.append(html.escape(str(row["draft_text"])[:2500]))
    source_rows = db.execute(
        """SELECT DISTINCT s.publisher,a.link FROM event_articles ea
           JOIN articles a ON a.id=ea.article_id
           JOIN article_sources ax ON ax.article_id=a.id
           JOIN sources s ON s.id=ax.source_id
           WHERE ea.event_id=? ORDER BY s.publisher,a.link LIMIT 5""",
        (event_id,),
    ).fetchall()
    if source_rows:
        links = []
        seen = set()
        for source in source_rows:
            publisher = str(source["publisher"] or "Источник")
            url = str(source["link"] or "")
            if url.startswith("https://"):
                item = f'<a href="{html.escape(url, quote=True)}">{html.escape(publisher)}</a>'
            else:
                item = html.escape(publisher)
            if item not in seen:
                seen.add(item)
                links.append(item)
        parts.append("Источники для проверки: " + ", ".join(links))
    parts.append("Нажатие «Опубликовать» отправит пост в канал. Автопубликация выключена.")
    return "\n\n".join(parts)[:3900]


async def _send_review(client: httpx.AsyncClient, token: str, chat_id: str,
                       text: str, event_id: int, image_path: str | None) -> dict[str, Any]:
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"review:yes:{event_id}"},
        {"text": "❌ Отклонить", "callback_data": f"review:no:{event_id}"},
    ]]}
    result = await _api(client, token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "reply_markup": keyboard,
    })
    if image_path and Path(image_path).is_file():
        mime = mimetypes.guess_type(image_path)[0]
        if mime in {"image/jpeg", "image/png"}:
            try:
                with open(image_path, "rb") as image:
                    response = await client.post(_api_url(token, "sendPhoto"),
                        data={"chat_id": chat_id, "caption": f"Предпросмотр изображения для события #{event_id}"},
                        files={"photo": (Path(image_path).name, image, mime)})
                response.raise_for_status()
                body = response.json()
                if not body.get("ok"):
                    LOGGER.warning("Telegram rejected review photo event=%d", event_id)
            except Exception as exc:
                LOGGER.warning("could not send review photo event=%d error=%s", event_id, type(exc).__name__)
    return result


async def notify_review_queue(db: sqlite3.Connection, db_path: Path) -> int:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_REVIEW_CHAT_ID", "").strip()
    if not token or not chat_id:
        return 0
    rows = db.execute(
        """SELECT id,image_path FROM events
           WHERE state='awaiting_review' AND review_notified_at IS NULL
           ORDER BY CASE publication_lane WHEN 'match_results' THEN 0 ELSE 1 END,
                    interest_score DESC,created_at,id LIMIT ?""",
        (MAX_REVIEW_NOTIFICATIONS_PER_CYCLE,),
    ).fetchall()
    sent = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0)) as client:
        for row in rows:
            event_id = int(row["id"])
            image_path = row["image_path"]
            if not image_path or not Path(image_path).is_file():
                try:
                    image_path = await asyncio.to_thread(_prepare_event_image, str(db_path), event_id)
                except Exception as exc:
                    LOGGER.warning("review preview image unavailable event=%d error=%s", event_id, type(exc).__name__)
                    image_path = None
            try:
                text = _review_text(db, event_id)
                if not image_path:
                    text += "\n\n⚠️ Фото пока не подготовлено; бот повторит подбор при публикации."
                result = await _send_review(client, token, chat_id, text, event_id, image_path)
            except Exception as exc:
                LOGGER.warning("could not send private review event=%d error=%s", event_id, type(exc).__name__)
                continue
            with db:
                db.execute(
                    """UPDATE events SET review_notified_at=?,review_message_id=?,updated_at=?
                       WHERE id=? AND state='awaiting_review' AND review_notified_at IS NULL""",
                    (utc_now(), str(result.get("message_id", "")), utc_now(), event_id),
                )
            sent += 1
            LOGGER.info("private editorial review sent event=%d", event_id)
    return sent


def _apply_decision(db_path: str, event_id: int, decision: str) -> str:
    db = connect(Path(db_path))
    try:
        row = db.execute("SELECT state FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise ValueError("Событие не найдено в базе")
        if decision == "no":
            if row["state"] != "awaiting_review":
                raise ValueError(f"Событие уже обработано (состояние: {row['state']})")
            skip_event(db, event_id, "Отклонено редактором в личном Telegram")
            return f"Событие #{event_id} отклонено."
        if row["state"] == "awaiting_review":
            approve_event(db, event_id)
        elif row["state"] != "approved":
            raise ValueError(f"Событие уже обработано (состояние: {row['state']})")
        from .publisher import publish_event
        asyncio.run(publish_event(db, event_id))
        return f"Событие #{event_id} опубликовано в канале."
    finally:
        db.close()


async def _handle_update(client: httpx.AsyncClient, token: str, configured_chat: str,
                         db_path: Path, update: dict[str, Any]) -> None:
    message = update.get("message")
    if isinstance(message, dict):
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()
        chat_id = str(chat.get("id", ""))
        command = text.split(maxsplit=1)[0] if text else ""
        if chat.get("type") == "private" and command in {"/start", "/review_id"}:
            if not configured_chat:
                await _api(client, token, "sendMessage", {"chat_id": chat_id,
                    "text": f"ID этого личного чата: <code>{html.escape(chat_id)}</code>. "
                            "Укажи его в TELEGRAM_REVIEW_CHAT_ID в файле .env на VPS, затем перезапусти сервис."})
            elif chat_id == configured_chat:
                await _api(client, token, "sendMessage", {"chat_id": chat_id,
                    "text": "FM LIVE: личная проверка включена. Бот пришлёт новости с кнопками «Опубликовать» и «Отклонить»."})
        return

    callback = update.get("callback_query")
    if not isinstance(callback, dict) or not configured_chat:
        return
    callback_message = callback.get("message") or {}
    chat = callback_message.get("chat") or {}
    sender = callback.get("from") or {}
    chat_id = str(chat.get("id", ""))
    if chat.get("type") != "private" or chat_id != configured_chat or str(sender.get("id", "")) != configured_chat:
        return
    data = str(callback.get("data") or "").split(":")
    if len(data) != 3 or data[0] != "review" or data[1] not in {"yes", "no"} or not data[2].isdigit():
        return
    event_id = int(data[2])
    decision = data[1]
    await _api(client, token, "answerCallbackQuery", {
        "callback_query_id": callback["id"],
        "text": "Запускаю публикацию…" if decision == "yes" else "Отклоняю новость…",
    })
    try:
        await _api(client, token, "editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": callback_message.get("message_id"),
            "reply_markup": {"inline_keyboard": []},
        })
    except Exception as exc:
        LOGGER.info("could not clear review buttons event=%d error=%s", event_id, type(exc).__name__)
    try:
        result = await asyncio.to_thread(_apply_decision, str(db_path), event_id, decision)
        await _api(client, token, "sendMessage", {"chat_id": chat_id, "text": result})
    except Exception as exc:
        LOGGER.exception("private review action failed event=%d", event_id)
        await _api(client, token, "sendMessage", {"chat_id": chat_id,
            "text": f"Не удалось завершить действие для события {event_id} ({type(exc).__name__}). "
                    "Проверь состояние события и канал перед повтором публикации."})


async def run_review_bot(db_path: Path) -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        LOGGER.info("private Telegram review is disabled: BOT_TOKEN is not configured")
        return
    chat_id = os.getenv("TELEGRAM_REVIEW_CHAT_ID", "").strip()
    LOGGER.info("private Telegram review polling started configured_chat=%s", bool(chat_id))
    offset: int | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0)) as client:
        while True:
            try:
                payload: dict[str, Any] = {"timeout": 20,
                    "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    payload["offset"] = offset
                updates = await _api(client, token, "getUpdates", payload)
                for update in updates or []:
                    offset = int(update.get("update_id", 0)) + 1
                    await _handle_update(client, token, chat_id, db_path, update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("private Telegram review polling error=%s", type(exc).__name__)
                await asyncio.sleep(5)