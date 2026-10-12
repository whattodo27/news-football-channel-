from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx

from .workflow import utc_now

LOGGER = logging.getLogger(__name__)

# Headline teaser generation for interesting_news cards. Reuses the existing Groq
# filter model (openai/gpt-oss-120b via FILTER_MODEL) rather than adding a second
# provider. Model output is cached on the event so previews/republishes do not
# re-call the API, and stale cache is dropped after a few days.
API_URL = "https://api.groq.com/openai/v1/chat/completions"
HEADLINE_TTL_DAYS = 3
HEADLINE_MAX_ATTEMPTS = 3
HEADLINE_BACKOFF_SECONDS = 1.0
HEADLINE_MAX_WORDS = 9
HEADLINE_FALLBACK_WORDS = 9

SYSTEM_PROMPT = (
    "Ты редактор заголовков футбольного Telegram-канала. Составь короткий, но полностью "
    "понятный заголовок новости: по нему сразу должно быть ясно, что именно произошло. "
    "Назови главного героя (команду или игрока) и его конкретное действие или главный факт — "
    "не прячь суть за интригой. Обычно 3–8 слов на русском, в тоне спортивного комментатора. "
    "Заголовок должен быть самодостаточным и однозначным: читатель, увидевший только его, "
    "понимает суть. Не обрезай слово посередине. Без эмодзи, без кавычек-ёлочек и без точки "
    "в конце. Примеры: «Роналду отстранён от сборной Португалии», «Месси установил рекорд MLS», "
    "«Зенит разгромил Краснодар со счётом 3:2». Верни ровно заголовок, без пояснений и обрамления."
)


def _key_and_model() -> tuple[str | None, str]:
    return os.getenv("GROQ_API_KEY") or None, os.getenv("FILTER_MODEL", "openai/gpt-oss-120b")


def _normalize(text: str) -> str:
    """Strip wrappers/punctuation and hard-cap the word count (no forced trailing ellipsis)."""
    text = text.strip().strip("«»\"'“”‘’").strip()
    text = " ".join(text.split())
    words = [w for w in text.split() if w]
    if len(words) > HEADLINE_MAX_WORDS:
        words = words[:HEADLINE_MAX_WORDS]
    return " ".join(words).rstrip(" ,.;:!-—–…").strip()


def _fallback_headline(title: str, draft: str) -> str | None:
    """Deterministic last-resort teaser: the first few WHOLE words (never mid-word)."""
    source = " ".join((draft or "").split()) or " ".join((title or "").split())
    source = re.sub(r"<[^>]+>", " ", source)  # strip Telegram HTML; fallback is plain text
    words = []
    for word in source.split():
        clean = word.strip("«»\"'“”‘’").strip(" ,.;:!-—–…")
        if not clean or not re.search(r"[0-9a-zа-яё]", clean, re.IGNORECASE):
            continue  # drop pure-emoji / punctuation tokens
        words.append(clean)
    if not words:
        return None
    return " ".join(words[:HEADLINE_FALLBACK_WORDS]).rstrip(" ,.;:!-—–…").strip()


def _request(title: str, draft: str, key: str, model: str) -> str:
    payload = {
        "model": model,
        "temperature": 0.4,
        "max_tokens": 2000,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Заголовок: {title}\nТекст новости: {draft}"},
        ],
    }
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = client.post(
            API_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    content = body["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("headline response had no text")
    return _normalize(content)


def _cache(db: sqlite3.Connection, event_id: int, headline: str) -> None:
    with db:
        db.execute(
            "UPDATE events SET headline_text=?,headline_generated_at=? WHERE id=?",
            (headline, utc_now(), event_id),
        )


def _is_fresh(stamp: str | None) -> bool:
    if not stamp:
        return False
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at) < timedelta(days=HEADLINE_TTL_DAYS)


def generate_headline(db: sqlite3.Connection, event_id: int) -> str | None:
    """Return a short teaser headline for an event, or None if none can be produced.

    Order: cached headline (if fresh) -> model call (with retries) -> deterministic
    whole-word fallback. A None result means the caller should not render a card.
    """
    row = db.execute(
        "SELECT title,draft_text,headline_text,headline_generated_at FROM events WHERE id=?",
        (event_id,),
    ).fetchone()
    if not row:
        return None
    if row["headline_text"] and _is_fresh(row["headline_generated_at"]):
        return str(row["headline_text"])

    title = str(row["title"] or "")
    draft = str(row["draft_text"] or "")
    key, model = _key_and_model()

    if key:
        for attempt in range(HEADLINE_MAX_ATTEMPTS):
            try:
                result = _request(title, draft, key, model)
                if result:
                    _cache(db, event_id, result)
                    return result
            except Exception as exc:  # network, HTTP, malformed output
                LOGGER.warning("headline attempt %d/%d failed event=%d (%s)",
                               attempt + 1, HEADLINE_MAX_ATTEMPTS, event_id, type(exc).__name__)
            if attempt < HEADLINE_MAX_ATTEMPTS - 1:
                time.sleep(HEADLINE_BACKOFF_SECONDS * (2 ** attempt))
    else:
        LOGGER.info("headline model skipped event=%d (GROQ_API_KEY missing); using fallback", event_id)

    fallback = _fallback_headline(title, draft)
    if fallback:
        _cache(db, event_id, fallback)
    return fallback