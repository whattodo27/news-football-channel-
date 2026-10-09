from __future__ import annotations

import logging
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import yaml

LOGGER = logging.getLogger(__name__)
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
    "relevance": {
        "football_min_score": 0.70,
        "not_football_auto_skip": 0.92,
    },
}

_rules_cache: dict[str, Any] | None = None
_rules_cache_path: Path | None = None
_rules_cache_mtime: float | None = None


def _editorial_config_path() -> Path:
    configured = os.getenv("NEWSBOT_EDITORIAL_CONFIG")
    path = Path(configured) if configured else Path("/app/editorial.yaml")
    if not configured and not path.is_file():
        path = Path(__file__).resolve().parents[2] / "editorial.yaml"
    return path


def _validate_rules(document: dict[str, Any]) -> dict[str, Any]:
    rules = {
        "interesting_news": {**DEFAULTS["interesting_news"], **(document.get("interesting_news") or {})},
        "match_results": {**DEFAULTS["match_results"], **(document.get("match_results") or {})},
        "relevance": {**DEFAULTS["relevance"], **(document.get("relevance") or {})},
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
    football_min = float(rules["relevance"]["football_min_score"])
    auto_skip = float(rules["relevance"]["not_football_auto_skip"])
    if not 0.0 <= football_min <= 1.0:
        raise ValueError("relevance.football_min_score must be between 0 and 1")
    if not 0.0 <= auto_skip <= 1.0:
        raise ValueError("relevance.not_football_auto_skip must be between 0 and 1")
    if auto_skip < football_min:
        raise ValueError("relevance.not_football_auto_skip must be >= football_min_score")
    rules["relevance"]["football_min_score"] = football_min
    rules["relevance"]["not_football_auto_skip"] = auto_skip
    return rules


def load_rules(*, force: bool = False) -> dict[str, Any]:
    """Load editorial.yaml once and reuse until the file mtime changes.

    A temporarily broken YAML keeps the last good cache so a running cycle
    does not crash mid-loop.
    """
    global _rules_cache, _rules_cache_path, _rules_cache_mtime
    path = _editorial_config_path()
    mtime: float | None = None
    if path.is_file():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
    if (
        not force
        and _rules_cache is not None
        and _rules_cache_path == path
        and _rules_cache_mtime == mtime
    ):
        return _rules_cache
    if not path.is_file():
        rules = {
            "interesting_news": {**DEFAULTS["interesting_news"]},
            "match_results": {**DEFAULTS["match_results"]},
            "relevance": {**DEFAULTS["relevance"]},
        }
        _rules_cache, _rules_cache_path, _rules_cache_mtime = rules, path, mtime
        return rules
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            document = yaml.safe_load(stream) or {}
        if not isinstance(document, dict):
            raise ValueError("editorial.yaml must contain a mapping")
        rules = _validate_rules(document)
    except Exception as exc:
        if _rules_cache is not None:
            LOGGER.warning(
                "editorial.yaml reload failed (%s); keeping cached rules",
                type(exc).__name__,
            )
            return _rules_cache
        raise
    _rules_cache, _rules_cache_path, _rules_cache_mtime = rules, path, mtime
    return rules


def relevance_thresholds(rules: dict[str, Any] | None = None) -> tuple[float, float]:
    rules = rules or load_rules()
    relevance = rules["relevance"]
    return float(relevance["football_min_score"]), float(relevance["not_football_auto_skip"])


def min_independent_publishers(lane: str, rules: dict[str, Any] | None = None) -> int:
    rules = rules or load_rules()
    if lane not in {"interesting_news", "match_results"}:
        raise ValueError(f"Unknown publication lane: {lane}")
    return int(rules[lane]["min_independent_publishers"])


def _fold_competition(value: str) -> str:
    """NFKD-fold diacritics so Brasileirão matches Brasileirao."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-zа-яё]+", " ", stripped).strip()


def _competition_matches(candidate: str, names: list[str]) -> bool:
    normalized = _fold_competition(candidate)
    return bool(normalized and any(_fold_competition(alias) == normalized for alias in names))


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


@contextmanager
def immediate_transaction(db: sqlite3.Connection) -> Iterator[None]:
    """Serialize writers with BEGIN IMMEDIATE (required for daily-limit checks)."""
    if db.in_transaction:
        raise RuntimeError("immediate_transaction requires a connection with no open transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


def ensure_story_approval_slot(db: sqlite3.Connection, rules: dict[str, Any] | None = None) -> None:
    """Raise if the Moscow-day interesting_news quota is already full.

    Call only inside an IMMEDIATE transaction held by the approval path so the
    subsequent state transition cannot race another concurrent approval.
    """
    limit = int((rules or load_rules())["interesting_news"]["daily_limit"])
    start, end = _today_window()
    count = int(db.execute(
        """SELECT COUNT(*) FROM events WHERE publication_lane='interesting_news' AND
           ((state='published' AND published_at>=? AND published_at<?) OR
            (state='approved' AND approved_at>=? AND approved_at<?) OR
            (state IN ('publishing','retry_wait') AND approved_at>=? AND approved_at<?))""",
        (start, end, start, end, start, end),
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
