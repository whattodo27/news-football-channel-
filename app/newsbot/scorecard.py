from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from .assets import choose_background, find_crest, find_flag
from .config import source_images_dir
from .workflow import utc_now

LOGGER = logging.getLogger(__name__)
SIZE = (1080, 1350)
# Deferred: mem_limit 256m — optimize _gradient_background pixel loop if OOM under load.
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
VISION_PROMPT = """Ты арт-директор Telegram-канала FM LIVE. Проверь готовую футбольную карточку.
Оцени иерархию текста, читаемость счёта и названий, размер и заметность эмблем, отступы,
баланс композиции и контраст. Карточка должна содержать только сведения о матче и бренд FM LIVE;
не добавляй NewsBot, кредиты фотобанкам или длинные пояснения. Это тестовое изображение: не проверяй истинность матча.
Флаги должны оставаться прямоугольными; не предлагай обрезать флаги в круг. Картинка —
готовый растр, поэтому не предлагай CSS, HTML или браузерные свойства. Предлагай только
конкретные правки композиции и размеров, применимые к Pillow. Не хвали из вежливости.
Верни только JSON: visual_score, logo_visibility, text_readability и spacing (целые 1..10),
issues и recommendations (короткие массивы строк), approved (boolean)."""


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size=size)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    while size > 20:
        font = _font(size, bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
        size -= 2
    return _font(size, bold)


def _gradient_background() -> Image.Image:
    image = Image.new("RGB", SIZE)
    pixels = image.load()
    stops = ((4, 17, 31), (7, 38, 49), (4, 48, 40))
    for y in range(SIZE[1]):
        t = y / (SIZE[1] - 1)
        if t < 0.55:
            start, end, local_t = stops[0], stops[1], t / 0.55
        else:
            start, end, local_t = stops[1], stops[2], (t - 0.55) / 0.45
        color = tuple(round(a * (1 - local_t) + b * local_t) for a, b in zip(start, end))
        for x in range(SIZE[0]):
            pixels[x, y] = color
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((690, -360, 1420, 370), fill=(42, 192, 128, 25))
    draw.ellipse((-440, 870, 390, 1700), fill=(24, 174, 111, 22))
    for offset in range(-500, 1500, 110):
        draw.line((offset, 0, offset + 700, 1350), fill=(255, 255, 255, 5), width=2)
    return image.convert("RGBA")


def _background(db: sqlite3.Connection) -> tuple[Image.Image, sqlite3.Row | None]:
    asset = choose_background(db)
    if asset:
        try:
            with Image.open(asset["file_path"]) as source:
                fitted = ImageOps.fit(source.convert("RGB"), SIZE, method=Image.Resampling.LANCZOS)
                return fitted.convert("RGBA"), asset
        except (OSError, ValueError) as exc:
            LOGGER.warning("invalid match-card background asset=%s error=%s", asset["id"], type(exc).__name__)
    return _gradient_background(), None


def _fit_name(draw: ImageDraw.ImageDraw, name: str, max_width: int, max_lines: int = 2) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    words = " ".join(name.split()).split()
    lines: list[str] = []
    current = ""
    for word in words:
        attempt = f"{current} {word}".strip()
        if draw.textbbox((0, 0), attempt, font=_font(54, True))[2] <= max_width:
            current = attempt
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    while len(lines) > max_lines:
        lines[-2] = lines[-2] + " " + lines[-1]
        lines.pop()
    return [(line, _fit_font(draw, line, max_width, 54)) for line in lines]


def _is_white_matte(pixel: tuple[int, ...]) -> bool:
    if len(pixel) < 4 or pixel[3] == 0:
        return False
    red, green, blue = pixel[:3]
    return min(red, green, blue) >= 224 and max(red, green, blue) - min(red, green, blue) <= 28


def _transparent_outer_matte(source: Image.Image) -> Image.Image:
    """Remove only near-white pixels connected to the image border, preserving inner whites."""
    image = source.convert("RGBA")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    seeds = [(x, 0) for x in range(width)] + [(x, height - 1) for x in range(width)]
    seeds += [(0, y) for y in range(1, height - 1)] + [(width - 1, y) for y in range(1, height - 1)]
    for point in seeds:
        if _is_white_matte(image.getpixel(point)):
            ImageDraw.floodfill(image, point, (0, 0, 0, 0), thresh=36)
    alpha = image.getchannel("A")
    bounds = alpha.getbbox()
    return image.crop(bounds) if bounds else image


def _rounded_alpha(image: Image.Image, radius: int = 18) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width - 1, image.height - 1),
                                           radius=radius, fill=255)
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    return image


def _draw_crest(canvas: Image.Image, db: sqlite3.Connection, team: str, center_x: int, center_y: int) -> sqlite3.Row | None:
    asset = find_crest(db, team)
    if asset:
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.ellipse((center_x - 158, center_y - 158, center_x + 158, center_y + 158),
                     fill=(3, 15, 27, 150), outline=(211, 255, 235, 95), width=3)
        draw.ellipse((center_x - 144, center_y - 144, center_x + 144, center_y + 144),
                     outline=(255, 255, 255, 28), width=2)
        try:
            with Image.open(asset["file_path"]) as source:
                logo = _transparent_outer_matte(source)
                logo.thumbnail((256, 256), Image.Resampling.LANCZOS)
                canvas.alpha_composite(logo, (center_x - logo.width // 2, center_y - logo.height // 2))
                return asset
        except (OSError, ValueError) as exc:
            LOGGER.warning("invalid crest asset=%s error=%s", asset["id"], type(exc).__name__)

    flag = find_flag(db, team)
    if flag:
        try:
            with Image.open(flag["file_path"]) as source:
                flag_image = _transparent_outer_matte(source)
                flag_image.thumbnail((260, 162), Image.Resampling.LANCZOS)
                flag_image = _rounded_alpha(flag_image, 16)
                width, height = flag_image.size
                draw = ImageDraw.Draw(canvas, "RGBA")
                draw.rounded_rectangle(
                    (center_x - width // 2 - 10, center_y - height // 2 - 10,
                     center_x + width // 2 + 10, center_y + height // 2 + 10),
                    radius=24, fill=(3, 15, 27, 125))
                canvas.alpha_composite(flag_image, (center_x - width // 2, center_y - height // 2))
                return flag
        except (OSError, ValueError) as exc:
            LOGGER.warning("invalid country flag code=%s error=%s", flag["country_code"], type(exc).__name__)

    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.ellipse((center_x - 158, center_y - 158, center_x + 158, center_y + 158),
                 fill=(3, 15, 27, 150), outline=(211, 255, 235, 95), width=3)
    initials = "".join(part[0] for part in re.findall(r"[^\W_]+", team, flags=re.UNICODE)[:2]).upper() or "FC"
    draw.text((center_x, center_y), initials, font=_font(82, True), anchor="mm",
              fill=(255, 255, 255, 245), stroke_width=2, stroke_fill=(0, 0, 0, 80))
    return None

def generate_scorecard(db: sqlite3.Connection, event_id: int, force: bool = False) -> str:
    event = db.execute(
        """SELECT id,title,image_path,image_origin,match_content_type,match_home_team,match_away_team,
                  match_home_score,match_away_score,match_competition,match_status
           FROM events WHERE id=?""", (event_id,)
    ).fetchone()
    if not event:
        raise ValueError(f"Event {event_id} does not exist")
    if event["match_content_type"] != "match_result" or not event["match_home_team"] or not event["match_away_team"]:
        raise ValueError("Event does not contain a structured match result")
    if event["match_home_score"] is None or event["match_away_score"] is None:
        raise ValueError("Match result has no complete score")
    if event["match_status"] not in {"final", "extra_time"}:
        raise ValueError("Only completed matches can use a result card")
    if event["image_origin"] == "scorecard" and event["image_path"] and Path(event["image_path"]).is_file() and not force:
        return event["image_path"]

    canvas, background_asset = _background(db)
    shade = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    for y in range(SIZE[1]):
        alpha = int(10 + 42 * y / (SIZE[1] - 1))
        shade_draw.line((0, y, SIZE[0], y), fill=(0, 7, 14, alpha))
    canvas = Image.alpha_composite(canvas, shade)
    draw = ImageDraw.Draw(canvas, "RGBA")

    draw.rounded_rectangle((78, 62, 1002, 132), radius=34, fill=(11, 28, 36, 220),
                           outline=(212, 255, 235, 100), width=3)
    draw.text((540, 97), "FM LIVE", font=_font(31, True),
              anchor="mm", fill=(216, 255, 237, 255))
    draw.text((540, 185), "ИТОГ МАТЧА", font=_font(24, True), anchor="mm",
              fill=(103, 224, 166, 255))
    competition = " ".join(str(event["match_competition"] or "ФУТБОЛЬНЫЙ МАТЧ").split()).upper()[:70]
    comp_font = _fit_font(draw, competition, 900, 39)
    draw.text((540, 244), competition, font=comp_font, anchor="mm", fill=(255, 255, 255, 255),
              stroke_width=1, stroke_fill=(0, 0, 0, 80))

    home_crest = _draw_crest(canvas, db, str(event["match_home_team"]), 286, 485)
    away_crest = _draw_crest(canvas, db, str(event["match_away_team"]), 794, 485)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle((500, 453, 580, 517), radius=30, fill=(1, 14, 24, 185),
                           outline=(255, 255, 255, 55), width=2)
    draw.text((540, 485), "VS", font=_font(23, True), anchor="mm", fill=(190, 232, 214, 255))

    for team, center_x in ((str(event["match_home_team"]), 286), (str(event["match_away_team"]), 794)):
        lines = _fit_name(draw, team, 450)
        start_y = 690 - 31 * (len(lines) - 1)
        for index, (line, font) in enumerate(lines):
            draw.text((center_x, start_y + index * 66), line, font=font, anchor="mm",
                      fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 100))

    draw.rounded_rectangle((200, 790, 880, 975), radius=56, fill=(2, 13, 22, 128),
                           outline=(255, 255, 255, 42), width=2)
    score = f"{int(event['match_home_score'])} : {int(event['match_away_score'])}"
    score_font = _fit_font(draw, score, 740, 164)
    draw.text((540, 886), score, font=score_font, anchor="mm", fill=(255, 255, 255, 255),
              stroke_width=2, stroke_fill=(0, 0, 0, 80))
    status = {"final": "ФИНАЛ", "extra_time": "ПОСЛЕ ДОПОЛНИТЕЛЬНОГО ВРЕМЕНИ"}[str(event["match_status"])]
    status_font = _fit_font(draw, status, 850, 30)
    draw.rounded_rectangle((225, 1070, 855, 1156), radius=43, fill=(104, 235, 164, 242))
    draw.text((540, 1113), status, font=status_font, anchor="mm", fill=(4, 31, 23, 255))

    output_dir = source_images_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"event-{event_id}.jpg"
    canvas.convert("RGB").save(path, "JPEG", quality=92, optimize=True, progressive=True)
    if path.stat().st_size > 15 * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise ValueError("Generated match card exceeds 15 MB")

    license_parts = []
    credits = []
    for asset in (background_asset, home_crest, away_crest):
        if not asset:
            continue
        if asset["license"] and asset["license"] not in license_parts:
            license_parts.append(asset["license"])
        if asset["credit"] and asset["credit"] not in credits:
            credits.append(asset["credit"])
    with db:
        db.execute(
            """UPDATE events SET image_path=?,image_origin='scorecard',image_description=?,
                      image_license=?,image_credit=?,image_selected_at=?,image_selection_reason=?,
                      image_selection_model='local-card-renderer',image_generated_at=NULL,updated_at=?
               WHERE id=?""",
            (str(path), f"Карточка результата: {event['match_home_team']} — {event['match_away_team']}",
             "; ".join(license_parts) or None, "; ".join(credits) or None, utc_now(),
             "Собрано локально из фоновой библиотеки, обрезанных эмблем и прозрачных флагов",
             utc_now(), event_id),
        )
    LOGGER.info("generated match scorecard event=%d background_asset=%s bytes=%d",
                event_id, background_asset["id"] if background_asset else "gradient", path.stat().st_size)
    return str(path)


def _soft_shadow(image: Image.Image, radius: int = 12, alpha: int = 90) -> Image.Image:
    """Pad an RGBA image with a soft dark halo for contrast on busy backgrounds."""
    pad = radius
    size = (image.width + 2 * pad, image.height + 2 * pad)
    silhouette = image.getchannel("A").filter(ImageFilter.GaussianBlur(radius))
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    dark = Image.new("RGBA", size, (0, 0, 0, alpha))
    dark.putalpha(silhouette)
    shadow.alpha_composite(dark)
    shadow.alpha_composite(image, (pad, pad))
    return shadow


def _headline_layout(draw: ImageDraw.ImageDraw, headline: str) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    """Layout a short teaser headline: one big line if it fits, else a two-line wrap."""
    font = _fit_font(draw, headline, 840, 88, bold=True)
    if font.size >= 48:
        return [(headline, font)]
    return _fit_name(draw, headline, 840, max_lines=2)


def _resolve_event_visual(db: sqlite3.Connection, title: str) -> tuple[str | None, str | None]:
    """Find one crest or flag whose alias appears in the title (word-boundary match).

    Prefers a club crest over a national flag (more specific); within each, the
    longest matching alias wins. Returns (file_path, kind) or (None, None). At most
    one visual is ever returned -- never both.
    """
    text = title.casefold()
    boundary = r"(?<![0-9a-zа-яё]){}(?![0-9a-zа-яё])"

    def best_match(query: str) -> tuple[str | None, int]:
        path, length = None, 0
        for row in db.execute(query).fetchall():
            alias = str(row["alias"] or "").strip().casefold()
            if len(alias) < 4:
                continue
            if re.search(boundary.format(re.escape(alias)), text) and len(alias) > length:
                path, length = str(row["file_path"]), len(alias)
        return path, length

    crest, _ = best_match(
        "SELECT a.file_path AS file_path, x.alias AS alias "
        "FROM media_assets a JOIN media_asset_aliases x ON x.asset_id = a.id "
        "WHERE a.asset_type = 'crest' AND a.enabled = 1")
    if crest:
        return crest, "crest"
    flag, _ = best_match(
        "SELECT f.file_path AS file_path, x.alias AS alias "
        "FROM country_flags f JOIN country_flag_aliases x ON x.country_code = f.country_code "
        "WHERE f.enabled = 1")
    if flag:
        return flag, "flag"
    return None, None


def generate_news_card(db: sqlite3.Connection, event_id: int,
                       photo_path: str | None = None, photo_origin: str | None = None) -> str:
    """Render an interesting_news card: random background + headline (+ photo or crest/flag).

    If ``photo_path`` is given it is composited ON TOP of the background as the main
    image; otherwise a crest/flag resolved from the title is overlaid, and if neither
    resolves only the background + headline are drawn. No score line, no brand.
    """
    event = db.execute("SELECT id,title,image_credit,image_license FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        raise ValueError(f"Event {event_id} does not exist")
    from .headline import generate_headline
    headline = generate_headline(db, event_id)
    if not headline:
        raise ValueError(f"Event {event_id} has no headline to render; falling back to text-only post")

    canvas, background_asset = _background(db)
    draw = ImageDraw.Draw(canvas, "RGBA")

    # Photo (RSS/AI) composited as a rounded main image in the upper area, or a
    # resolved crest/flag -- never a placeholder when neither is available.
    if photo_path and Path(photo_path).is_file():
        try:
            with Image.open(photo_path) as source:
                photo = ImageOps.fit(source.convert("RGB"), (960, 610),
                                     method=Image.Resampling.LANCZOS).convert("RGBA")
            photo = _rounded_alpha(photo, 24)
            canvas.alpha_composite(photo, (60, 152))
            draw.rounded_rectangle((60, 152, 1020, 762), radius=24,
                                   outline=(255, 255, 255, 40), width=2)
        except (OSError, ValueError) as exc:
            LOGGER.warning("news-card photo overlay failed event=%d error=%s",
                           event_id, type(exc).__name__)
            photo_path = None

    if not photo_path:
        visual_path, kind = _resolve_event_visual(db, str(event["title"] or ""))
        if visual_path:
            try:
                with Image.open(visual_path) as source:
                    visual = _transparent_outer_matte(source)
                if kind == "crest":
                    visual.thumbnail((300, 300), Image.Resampling.LANCZOS)
                else:
                    visual.thumbnail((360, 220), Image.Resampling.LANCZOS)
                    visual = _rounded_alpha(visual, 16)
                visual = _soft_shadow(visual)
                canvas.alpha_composite(visual, (540 - visual.width // 2, 470 - visual.height // 2))
            except (OSError, ValueError) as exc:
                LOGGER.warning("news-card crest/flag overlay failed event=%d error=%s",
                               event_id, type(exc).__name__)

    # Soft bottom scrim for headline legibility -- no hard-edged plate.
    scrim = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    for y in range(720, SIZE[1]):
        alpha = int(135 * (y - 720) / (SIZE[1] - 720))
        scrim_draw.line((0, y, SIZE[0], y), fill=(0, 7, 14, alpha))
    canvas = Image.alpha_composite(canvas, scrim)
    draw = ImageDraw.Draw(canvas, "RGBA")

    lines = _headline_layout(draw, headline)
    for index, (line, font) in enumerate(lines):
        y = 1100 + (index - (len(lines) - 1) / 2) * 74
        draw.text((540, y), line, font=font, anchor="mm",
                  fill=(255, 255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0, 110))

    output_dir = source_images_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"event-{event_id}.jpg"
    canvas.convert("RGB").save(path, "JPEG", quality=92, optimize=True, progressive=True)
    if path.stat().st_size > 15 * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise ValueError("Generated news card exceeds 15 MB")

    reason = {
        "rss": "RSS/Commons фото на фоновой карточке",
        "ai": "ИИ-фото на фоновой карточке",
    }.get(photo_origin, "Текстовая карточка (фон + заголовок)")
    credits: list[str] = []
    licenses: list[str] = []
    if photo_origin == "rss":
        photo_credit = str(event["image_credit"] or "").strip()
        photo_license = str(event["image_license"] or "").strip()
        if photo_credit:
            credits.append(photo_credit)
        if photo_license:
            licenses.append(photo_license)
    if background_asset and background_asset["credit"] and background_asset["credit"] not in credits:
        credits.append(background_asset["credit"])
    if background_asset and background_asset["license"] and background_asset["license"] not in licenses:
        licenses.append(background_asset["license"])
    license_name = "; ".join(licenses) or None
    credit = "; ".join(credits) or None
    generated_at = utc_now() if photo_origin == "ai" else None
    selection_model = "local-card-renderer" if photo_origin != "ai" else \
        f"local-card-renderer + {os.getenv('IMAGE_GEN_MODEL', 'AI')}"
    with db:
        db.execute(
            """UPDATE events SET image_path=?,image_origin='news_card',image_description=?,
                      image_license=?,image_credit=?,image_selected_at=?,image_selection_reason=?,
                      image_selection_model=?,image_generated_at=?,updated_at=? WHERE id=?""",
            (str(path), headline, license_name, credit, utc_now(), reason, selection_model,
             generated_at, utc_now(), event_id),
        )
    LOGGER.info("generated news card event=%d background=%s photo=%s bytes=%d",
                event_id, background_asset["id"] if background_asset else "gradient",
                "yes" if photo_path else "no", path.stat().st_size)
    return str(path)


def review_scorecard(image_path: str | Path) -> dict[str, Any]:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise ValueError("GROQ_API_KEY is required for a vision design review")
    model = os.getenv("VISION_MODEL", "qwen/qwen3.8-27b")
    path = Path(image_path)
    raw = path.read_bytes()
    if len(raw) > 15 * 1024 * 1024:
        raise ValueError("Scorecard image exceeds the safe review size limit")
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": VISION_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "Проверь эту карточку результата. Верни замечания и оценки по заданной JSON-схеме."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
    }
    try:
        response = httpx.post(GROQ_CHAT_URL, headers={"Authorization": f"Bearer {key}"},
                              json=payload, timeout=httpx.Timeout(90.0, connect=15.0))
        response.raise_for_status()
        result = json.loads(response.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        LOGGER.warning("scorecard vision review failed model=%s error=%s", model, type(exc).__name__)
        raise ValueError(f"Vision design review failed ({type(exc).__name__})") from None
    fields = ("visual_score", "logo_visibility", "text_readability", "spacing")
    for field in fields:
        try:
            result[field] = max(1, min(10, int(result[field])))
        except (KeyError, ValueError, TypeError):
            result[field] = 1
    result["issues"] = [str(value)[:250] for value in result.get("issues", [])][:8]
    result["recommendations"] = [str(value)[:250] for value in result.get("recommendations", [])][:8]
    result["approved"] = bool(result.get("approved"))
    result["model"] = model
    result["reviewed_at"] = utc_now()
    return result
