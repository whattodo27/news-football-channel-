from __future__ import annotations
import os
from pathlib import Path
from typing import Any
import yaml

def load_sources(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        document = yaml.safe_load(stream) or {}
    sources = document.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources.yaml must contain a 'sources' list")
    result, seen = [], set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError(f"sources[{index}] must be a mapping")
        source_id = str(source.get("id", "")).strip()
        if not source_id or source_id in seen:
            raise ValueError(f"sources[{index}] has a missing or duplicate id: {source_id!r}")
        seen.add(source_id)
        for field in ("name", "url", "independence_group"):
            if not str(source.get(field, "")).strip():
                raise ValueError(f"source {source_id!r} is missing {field!r}")
        if not str(source["url"]).startswith(("https://", "http://")):
            raise ValueError(f"source {source_id!r} has an invalid URL")
        source.setdefault("enabled", False)
        source.setdefault("language", "unknown")
        source.setdefault("publisher", source["name"])
        source.setdefault("source_type", "editorial")
        source.setdefault("scope", "association_football")
        source.setdefault("tournaments", [])
        source.setdefault("trust_tier", "unrated")
        source.setdefault("source_priority", 0)
        source.setdefault("image_ai_allowed", False)
        source.setdefault("image_reuse_allowed", False)
        result.append(source)
    return result

def runtime_paths() -> tuple[Path, Path, int]:
    sources_path = Path(os.getenv("NEWSBOT_SOURCES", "/app/sources.yaml"))
    db_path = Path(os.getenv("NEWSBOT_DB", "/data/newsbot.sqlite3"))
    poll_seconds = int(os.getenv("NEWSBOT_POLL_SECONDS", "3600"))
    if poll_seconds < 30:
        raise ValueError("NEWSBOT_POLL_SECONDS must be at least 30")
    return sources_path, db_path, poll_seconds
