from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULTS: dict[str, Any] = {
    "interesting_news": {"daily_limit": 2, "min_interest_score": 7, "min_independent_publishers": 1},
    "match_results": {
        "mode": "selected",
        "min_independent_publishers": 2,
        "selected_competitions": [],
        "active_tournament": "",
        "tournament_aliases": [],
    },
}


def load_rules() -> dict[str, Any]:
    configured = os.getenv("NEWSBOT_EDITORIAL_CONFIG")
    path = Path(configured) if configured else Path("/app/editorial.yaml")
    if not configured and not path.is_file():
        path = Path(__file__).resolve().parents[2] / "editorial.yaml"
    if not path.is_file():
        return DEFAULTS
    with path.open("r", encoding="utf-8-sig") as stream:
        document = yaml.safe_load(stream) or {}
    if not isinstance(document, dict):
        raise ValueError("editorial.yaml must contain a mapping")
    rules = {
        "interesting_news": {**DEFAULTS["interesting_news"], **(document.get("interesting_news") or {})},
        "match_results": {**DEFAULTS["match_results"], **(document.get("match_results") or {})},
    }
    limit = int(rules["interesting_news"]["daily_limit"])
    score = int(rules["interesting_news"]["min_interest_score"])
    if limit not in {1, 2}:
        raise ValueError("interesting_news.daily_limit must be 1 or 2")
    if not 0 <= score <= 10:
        raise ValueError("interesting_news.min_interest_score must be between 0 and 10")
    for lane in ("interesting_news", "match_results"):
        minimum = int(rules[lane]["min_independent_publishers"])
        if minimum not in {1, 2}:
            raise ValueError(f"{lane}.min_independent_publishers must be 1 or 2")
    match_rules = rules["match_results"]
    if match_rules["mode"] not in {"selected", "tournament"}:
        raise ValueError("match_results.mode must be selected or tournament")
    selected = match_rules.get("selected_competitions") or []
    if not isinstance(selected, list):
        raise ValueError("match_results.selected_competitions must be a list")
    for item in selected:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError("Each selected competition needs a name")
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            raise ValueError("Competition aliases must be a list of strings")
    if match_rules["mode"] == "tournament" and not str(match_rules["active_tournament"]).strip():
        raise ValueError("match_results.active_tournament is required in tournament mode")
    aliases = match_rules.get("tournament_aliases") or []
    if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
        raise ValueError("match_results.tournament_aliases must be a list of strings")
    return rules


def min_independent_publishers(lane: str, rules: dict[str, Any] | None = None) -> int:
    rules = rules or load_rules()
    if lane not in {"interesting_news", "match_results"}:
        raise ValueError(f"Unknown publication lane: {lane}")
    return int(rules[lane]["min_independent_publishers"])


def _competition_matches(candidate: str, names: list[str]) -> bool:
    value = " ".join(candidate.casefold().split())
    normalized = re.sub(r"[^0-9a-zа-яё]+", " ", value).strip()
    return bool(normalized and any(
        re.sub(r"[^0-9a-zа-яё]+", " ", alias.casefold()).strip() == normalized
        for alias in names
    ))


def match_result_eligible(competition: str | None, rules: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    rules = rules or load_rules()
    settings = rules["match_results"]
    candidate = str(competition or "").strip()
    for item in settings.get("selected_competitions", []):
        names = [str(item["name"]), *(str(alias) for alias in item.get("aliases", []))]
        if _competition_matches(candidate, names):
            return True, None
    if settings["mode"] == "tournament":
        active = str(settings.get("active_tournament") or "").strip()
        aliases = [active, *(str(alias) for alias in settings.get("tournament_aliases", []))]
        if _competition_matches(candidate, aliases):
            return True, None
        return False, f"Режим турнира «{active}»: соревнование не входит в выбранный список"
    return False, f"Турнир «{candidate or 'не указан'}» не включён в список публикуемых соревнований"

def _today_window() -> tuple[str, str]:
    now = datetime.now(MOSCOW)
    start = datetime.combine(now.date(), time.min, tzinfo=MOSCOW).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def ensure_story_approval_slot(db: sqlite3.Connection, rules: dict[str, Any] | None = None) -> None:
    limit = int((rules or load_rules())["interesting_news"]["daily_limit"])
    start, end = _today_window()
    count = int(db.execute(
        """SELECT COUNT(*) FROM events WHERE publication_lane='interesting_news' AND
           ((state='published' AND published_at>=? AND published_at<?) OR
            (state='approved' AND approved_at>=? AND approved_at<?))""",
        (start, end, start, end),
    ).fetchone()[0])
    if count >= limit:
        raise ValueError(f"Дневной лимит интересных новостей исчерпан ({count}/{limit}, Europe/Moscow)")


def ensure_story_publish_slot(db: sqlite3.Connection, event_id: int, rules: dict[str, Any] | None = None) -> None:
    limit = int((rules or load_rules())["interesting_news"]["daily_limit"])
    start, end = _today_window()
    count = int(db.execute(
        """SELECT COUNT(*) FROM events WHERE publication_lane='interesting_news' AND state='published'
           AND published_at>=? AND published_at<? AND id!=?""",
        (start, end, event_id),
    ).fetchone()[0])
    if count >= limit:
        raise ValueError(f"Дневной лимит интересных новостей исчерпан ({count}/{limit}, Europe/Moscow)")
