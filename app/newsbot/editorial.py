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
    env_limit = os.getenv("NEWSBOT_AUTOPUBLISH_DAILY_LIMIT", "").strip()
    if env_limit:
        limit = int(env_limit)
        rules["interesting_news"]["daily_limit"] = limit
    score = int(rules["interesting_news"]["min_interest_score"])
    if not 1 <= limit <= 20:
        raise ValueError("interesting_news.daily_limit must be between 1 and 20")
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
    return re.sub(r"[^0-9a-zа-яё]+\s*", " ", stripped).strip()


# Cyrillic -> Latin transliteration for team names, so a Russian RPL/RSS spelling
# («Рома», «Марсель») can resolve against the Latin crest/flag aliases in the library.
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# Russian spellings that a mechanical transliteration cannot recover (French/Italian
# club names, abbreviations). Keys are the normalized Cyrillic form.
_TEAM_LATIN_OVERRIDES = {
    "марсель": ["marseille"],
    "ювентус": ["juventus"],
    "псж": ["paris saint germain", "paris sg", "psg"],
    "пари сен-жермен": ["paris saint germain", "paris sg"],
    "интер": ["inter milan", "inter"],
    "милан": ["ac milan", "milan"],
    "труа": ["troyes"],
    "тройё": ["troyes"],
    "тулуза": ["toulouse"],
    "аякс": ["ajax"],
    "бавария": ["bayern munich", "fc bayern"],
    "боруссия": ["borussia dortmund"],
    "реал": ["real madrid"],
    "барселона": ["fc barcelona", "barcelona"],
    "атлетико": ["atletico madrid"],
    "севилья": ["sevilla"],
    "вильярреал": ["villarreal"],
    "челси": ["chelsea"],
    "арсенал": ["arsenal"],
    "ливерпуль": ["liverpool"],
    "манчестер сити": ["manchester city"],
    "манчестер юнайтед": ["manchester united"],
    "тоттенхэм": ["tottenham hotspur"],
    "порту": ["fc porto", "porto"],
    "бенфика": ["benfica"],
    "спортинг": ["sporting cp", "sporting"],
    "лацио": ["lazio"],
    "наполи": ["napoli"],
    "фиорентина": ["fiorentina"],
}


def transliterate(value: str) -> str:
    """Transliterate Cyrillic letters to Latin; non-Cyrillic characters pass through."""
    return "".join(_CYR.get(ch, ch) for ch in value.casefold())


def fold_team(value: str) -> str:
    """Normalize a team name for comparison: casefold, transliterate, strip marks/punct.

    When a canonical override exists (French/Italian names, abbreviations), that wins so
    «Труа» and «Тройё» both fold to «troyes» and «Марсель» folds to «marseille».
    """
    raw = str(value or "").strip()
    cyr_key = re.sub(r"[^0-9a-zа-яё]+", "", raw.casefold())
    overrides = _TEAM_LATIN_OVERRIDES.get(cyr_key)
    if overrides:
        first = re.sub(r"[^0-9a-z]+", "", transliterate(overrides[0]))
        if first:
            return first
    translit = transliterate(raw)
    decomposed = unicodedata.normalize("NFKD", translit)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-zа-яё]+", "", stripped)


def team_lookup_forms(value: str | None) -> list[str]:
    """All normalized comparison forms for a team name (Cyrillic + transliterated + overrides)."""
    raw = str(value or "").strip()
    if not raw:
        return []
    forms = [fold_team(raw)]
    translit_norm = re.sub(r"[^0-9a-z]+", "", transliterate(raw))
    if translit_norm and translit_norm not in forms:
        forms.append(translit_norm)
    cyr_key = re.sub(r"[^0-9a-zа-яё]+", "", raw.casefold())
    for latin in _TEAM_LATIN_OVERRIDES.get(cyr_key, []):
        norm = re.sub(r"[^0-9a-z]+", "", transliterate(latin))
        if norm and norm not in forms:
            forms.append(norm)
    return [f for f in forms if f]


def canonical_competition(competition: str | None, rules: dict[str, Any] | None = None) -> str:
    """Map a competition string to its selected competition's canonical name, else the folded raw.

    «РПЛ», «Российская Премьер-Лига» and «RPL» all fold to the canonical «Российская Премьер-Лига»
    entry name, so a match reported under different spellings deduplicates to one identity.
    """
    rules = rules or load_rules()
    candidate = str(competition or "").strip()
    for item in rules["match_results"].get("selected_competitions", []):
        names = [str(item["name"]), *(str(alias) for alias in item.get("aliases", []))]
        if _competition_matches(candidate, names):
            return _fold_competition(str(item["name"]))
    return _fold_competition(candidate)


def match_fingerprint(home: str, away: str, home_score: int, away_score: int,
                      competition: str | None) -> str:
    """Canonical identity for a completed match, immune to home/away order and spelling.

    Teams are sorted so «Зенит 3:2 Краснодар» and «Краснодар 2:3 Зенит» produce the same key.
    The day is deliberately NOT part of the key: the same fixture is often reported under
    different days (night matches, timezone/late coverage), so dedup must compare dates
    within a tolerance window instead of on an exact day.
    """
    import hashlib
    a, b = sorted((fold_team(home), fold_team(away)))
    if a == fold_team(home):
        sa, sb = home_score, away_score
    else:
        sa, sb = away_score, home_score
    comp = canonical_competition(competition, rules=None)
    material = f"{a}|{b}|{sa}|{sb}|{comp}"
    return hashlib.sha256(material.encode()).hexdigest()


def _competition_matches(candidate: str, names: list[str]) -> bool:
    if not names:
        return False
    normalized = _fold_competition(candidate)
    latin = _fold_competition(transliterate(candidate))
    for alias in names:
        folded = _fold_competition(alias)
        if folded in (normalized, latin) or _fold_competition(transliterate(alias)) in (normalized, latin):
            return True
    return False


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
