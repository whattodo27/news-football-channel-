from __future__ import annotations

import base64
import logging
import os
import sqlite3

import httpx

from .config import generated_images_dir

LOGGER = logging.getLogger(__name__)

# Provider: Recraft by default (the project's configured image provider). Its Images
# API is OpenAI-compatible (POST /images/generations returning b64_json), but the
# model names differ ("recraftv3", "recraft20b" — NOT "recraft-v4.1-flash") and it
# returns WebP, not JPEG/PNG. An OpenAI-compatible third-party endpoint can still be
# used by setting IMAGE_GEN_API_KEY / IMAGE_GEN_MODEL / IMAGE_GEN_BASE_URL explicitly.
DEFAULT_BASE_URL = "https://external.api.recraft.ai/v1"
DEFAULT_MODEL = "recraftv3"
DEFAULT_STYLE = "realistic_image"
DEFAULT_SIZE = "1365x1024"

# Known-bad model aliases seen in config, mapped to a valid Recraft model.
_MODEL_ALIASES = {
    "recraft-v4.1-flash": "recraftv3",
    "recraft-v4-flash": "recraftv3",
    "recraft-v3": "recraftv3",
    "recraftv4": "recraftv3",
}


def _is_recraft(base_url: str) -> bool:
    return "recraft" in base_url


def _settings() -> tuple[str, str, str, str, str]:
    key = os.getenv("IMAGE_GEN_API_KEY", "").strip() or os.getenv("RECRAFT_API_KEY", "").strip()
    model = os.getenv("IMAGE_GEN_MODEL", "").strip() or os.getenv("IMAGE_MODEL", "").strip() or DEFAULT_MODEL
    base_raw = os.getenv("IMAGE_GEN_BASE_URL", "").strip()
    base_url = (base_raw or DEFAULT_BASE_URL).rstrip("/")
    model = _MODEL_ALIASES.get(model, model)
    style = os.getenv("IMAGE_GEN_STYLE", "").strip() or DEFAULT_STYLE
    size = os.getenv("IMAGE_GEN_SIZE", "").strip() or DEFAULT_SIZE
    if not key:
        raise ValueError("IMAGE_GEN_API_KEY (или RECRAFT_API_KEY) не настроен — генерация фото недоступна")
    return key, model, base_url, style, size


def _prompt(title: str, draft: str) -> str:
    context = " ".join((title or "", draft or "")).strip()
    return (
        "Реалистичная редакционная фотография о футбольном событии: живое действие "
        "на стадионе или тренировке, естественное освещение, кинематографичная композиция. "
        "Без какого-либо текста, логотипов, брендов, водяных знаков и узнаваемых лиц. "
        f"Контекст: {context[:400]}"
    )


def generate_ai_photo(db: sqlite3.Connection, event_id: int) -> str:
    """Generate a realistic editorial football photo via the configured model.

    Returns the saved file path. Raises ValueError with a short reason when the
    provider is not configured or the call fails. Does not modify the database;
    the caller composites the returned photo into a card.
    """
    key, model, base_url, style, size = _settings()
    row = db.execute("SELECT title,draft_text FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise ValueError(f"Event {event_id} does not exist")
    prompt = _prompt(str(row["title"] or ""), str(row["draft_text"] or ""))
    url = f"{base_url}/images/generations"
    payload = {"model": model, "prompt": prompt, "n": 1, "size": size,
               "response_format": "b64_json"}
    if _is_recraft(base_url):
        payload["style"] = style
    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"Image generation failed (HTTP {exc.response.status_code})") from None
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(f"Image generation failed ({type(exc).__name__})") from None

    if not isinstance(body, dict):
        raise ValueError("Image generation returned an unexpected response shape")
    item = (body.get("data") or [None])[0]
    if not isinstance(item, dict):
        raise ValueError("Image generation response has no data entry")
    b64 = item.get("b64_json")
    if b64:
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError):
            raise ValueError("Image generation returned invalid base64 data") from None
    else:
        image_url = item.get("url")
        if not image_url:
            raise ValueError("Image generation returned neither b64_json nor a url")
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                download = client.get(image_url)
                download.raise_for_status()
                raw = download.content
        except httpx.HTTPError as exc:
            raise ValueError(f"Image generation download failed ({type(exc).__name__})") from None

    # Recraft returns WebP (RIFF....WEBP); generic OpenAI endpoints return JPEG/PNG.
    # PIL opens WebP fine, so it composites onto the card identically to a JPEG.
    if raw.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif raw.startswith(b"\x89PNG"):
        extension = ".png"
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        extension = ".webp"
    else:
        raise ValueError("Image generation returned an unsupported image format")

    output_dir = generated_images_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"event-{event_id}{extension}"
    path.write_bytes(raw)
    LOGGER.info("ai photo generated event=%d model=%s bytes=%d", event_id, model, len(raw))
    return str(path)