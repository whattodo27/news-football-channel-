from __future__ import annotations
import logging, os, sqlite3
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from .workflow import utc_now

LOGGER = logging.getLogger(__name__)
API_URL = "https://engine.prod.bria-api.com/v2/image/generate"
IMAGE_DIR = Path("/data/generated_images")


def generate_event_image(db: sqlite3.Connection, event_id: int, force: bool = False) -> str:
    key = os.getenv("BRIA_API_KEY")
    if not key: raise ValueError("BRIA_API_KEY is not configured")
    row = db.execute("SELECT title,draft_text,relevance,relevance_score,ai_state,image_path FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None: raise ValueError(f"Event {event_id} does not exist")
    if row["ai_state"] != "complete" or row["relevance"] != "football" or float(row["relevance_score"] or 0) < 0.70:
        raise ValueError("AI must confirm association-football relevance before image generation")
    if not (row["draft_text"] or "").strip(): raise ValueError("Event has no Russian draft")
    if row["image_path"] and Path(row["image_path"]).is_file() and not force:
        return row["image_path"]
    headline = " ".join(str(row["title"]).split())[:240]
    summary = " ".join(str(row["draft_text"]).split())[:360]
    prompt = ("Create a polished editorial illustration for a Russian association-football news post. "
        "Atmospheric floodlit stadium at dusk, deep navy and emerald palette, realistic sports-photo style, "
        "strong composition with clear negative space, subtle pitch geometry. No text, letters, numbers, "
        "logos, watermarks, recognizable players or club emblems. News context: " + headline + ". " + summary)
    try:
        with httpx.Client(timeout=httpx.Timeout(150.0, connect=15.0), follow_redirects=True) as client:
            response = client.post(API_URL, headers={"api_token":key,"Content-Type":"application/json"},
                json={"prompt":prompt,"aspect_ratio":"4:5","sync":True,"output_type":"png"})
            response.raise_for_status()
            result = response.json().get("result", {})
            image_url = result.get("image_url")
            if not isinstance(image_url, str) or urlsplit(image_url).scheme != "https":
                raise ValueError("Bria returned an invalid image URL")
            image_response = client.get(image_url)
            image_response.raise_for_status()
            if len(image_response.content) > 15 * 1024 * 1024:
                raise ValueError("Generated image exceeds 15 MB")
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"Bria returned HTTP {exc.response.status_code}") from None
    except httpx.TransportError as exc:
        LOGGER.warning("Bria request failed event=%d error=%s", event_id, type(exc).__name__)
        raise ValueError(f"Bria request failed ({type(exc).__name__})") from None
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGE_DIR / f"event-{event_id}.png"
    path.write_bytes(image_response.content)
    with db:
        db.execute("UPDATE events SET image_path=?,image_prompt=?,image_generated_at=?,updated_at=? WHERE id=?",
            (str(path),prompt,utc_now(),utc_now(),event_id))
    LOGGER.info("Bria image generated event=%d bytes=%d", event_id, len(image_response.content))
    return str(path)
