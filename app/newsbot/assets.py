from __future__ import annotations

import argparse
import hashlib
import logging
import re
import shutil
import sqlite3
from pathlib import Path

from .config import asset_root, runtime_paths
from .db import connect
from .workflow import utc_now

LOGGER = logging.getLogger(__name__)
MAX_ASSET_BYTES = 15 * 1024 * 1024
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}


def _asset_root() -> Path:
    return asset_root()


def _safe_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower()).strip("_")
    if not key:
        raise ValueError("Asset key must contain letters or digits")
    return key


def _detect_image(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise ValueError(f"Image file does not exist: {path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ASSET_BYTES:
        raise ValueError("Image must be between 1 byte and 15 MB")
    with path.open("rb") as image:
        signature = image.read(8)
    if signature.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif signature == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    else:
        raise ValueError("Only JPEG and PNG images are supported")
    return mime, EXTENSIONS[mime]


def _alias_norm(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", value.casefold())


def _register_team_alias(db: sqlite3.Connection, asset_id: int, alias: str) -> None:
    display = alias.strip()
    normalized = _alias_norm(display)
    if not normalized:
        return
    row = db.execute("SELECT asset_id FROM media_asset_aliases WHERE alias_norm=?", (normalized,)).fetchone()
    if row:
        if int(row["asset_id"]) != asset_id:
            # An ambiguous short name must not resolve to the wrong team's crest.
            with db:
                db.execute("DELETE FROM media_asset_aliases WHERE alias_norm=?", (normalized,))
        return
    with db:
        db.execute("INSERT INTO media_asset_aliases(asset_id,alias,alias_norm) VALUES(?,?,?)",
                   (asset_id, display, normalized))


def find_flag(db: sqlite3.Connection, team_name: str) -> sqlite3.Row | None:
    normalized = _alias_norm(team_name)
    row = db.execute(
        """SELECT f.* FROM country_flags f JOIN country_flag_aliases a ON a.country_code=f.country_code
           WHERE f.enabled=1 AND a.alias_norm=? LIMIT 1""",
        (normalized,),
    ).fetchone()
    if row:
        with db:
            db.execute("UPDATE country_flags SET last_used_at=? WHERE country_code=?",
                       (utc_now(), row["country_code"]))
    return row


COUNTRY_TRANSLATIONS = {
    "ARG": ("Аргентина", "Сборная Аргентины"), "AUS": ("Австралия", "Сборная Австралии"),
    "AUT": ("Австрия", "Сборная Австрии"), "BRA": ("Бразилия", "Сборная Бразилии"),
    "CAN": ("Канада", "Сборная Канады"), "CHN": ("Китай", "Сборная Китая"),
    "CRI": ("Коста-Рика", "Сборная Коста-Рики"), "DEU": ("Германия", "Сборная Германии"),
    "EGY": ("Египет", "Сборная Египта"), "ENG": ("Англия", "Сборная Англии"),
    "ESP": ("Испания", "Сборная Испании"), "FRA": ("Франция", "Сборная Франции"),
    "HKG": ("Гонконг", "Сборная Гонконга"), "ITA": ("Италия", "Сборная Италии"),
    "JPN": ("Япония", "Сборная Японии"), "KOR": ("Южная Корея", "Сборная Южной Кореи"),
    "MAR": ("Марокко", "Сборная Марокко"), "MCO": ("Монако", "Сборная Монако"),
    "MEX": ("Мексика", "Сборная Мексики"), "MYS": ("Малайзия", "Сборная Малайзии"),
    "NZL": ("Новая Зеландия", "Сборная Новой Зеландии"), "PRT": ("Португалия", "Сборная Португалии"),
    "SAU": ("Саудовская Аравия", "Сборная Саудовской Аравии"),
    "TUN": ("Тунис", "Сборная Туниса"), "THA": ("Таиланд", "Сборная Таиланда"),
    "USA": ("США", "Сборная США"), "ARE": ("ОАЭ", "Сборная ОАЭ"),
    "ZAF": ("ЮАР", "Сборная ЮАР"),
}
FCLOGO_PERMISSION = (
    "FCLOGO written permission supplied by user on 2026-10-05 covers the supplied image files "
    "(team cards, club logos, and flag image assets) for public or monetized Telegram posts. "
    "FCLOGO is recorded as the file provider, not as owner of national symbols. Attribution is optional."
)


def _register_flag_alias(db: sqlite3.Connection, code: str, alias: str) -> None:
    display = alias.strip()
    normalized = _alias_norm(display)
    if not normalized:
        return
    row = db.execute("SELECT country_code FROM country_flag_aliases WHERE alias_norm=?", (normalized,)).fetchone()
    if row:
        if row["country_code"] != code:
            with db:
                db.execute("DELETE FROM country_flag_aliases WHERE alias_norm=?", (normalized,))
        return
    with db:
        db.execute("INSERT INTO country_flag_aliases(country_code,alias,alias_norm) VALUES(?,?,?)",
                   (code, display, normalized))


def _find_flags_yaml(root: Path) -> Path | None:
    """Locate flags.yaml in the FCLOGO tree, tolerating a double-nested countries dir."""
    for candidate in (root / "countries" / "flags.yaml",
                      root / "countries" / "countries" / "flags.yaml"):
        if candidate.is_file():
            return candidate
    return None


def _import_country_flags(db: sqlite3.Connection, flags_yaml: Path) -> tuple[int, int]:
    import yaml

    base = flags_yaml.parent  # relative flag paths resolve against this directory
    manifest = yaml.safe_load(flags_yaml.read_text(encoding="utf-8")) or []
    copied = skipped = 0
    for row in manifest:
        code = str(row.get("nation") or "").upper()
        relative = row.get("flag2") or row.get("flag")
        if not code or not relative:
            skipped += 1
            continue
        source = base / relative
        if not source.is_file():
            skipped += 1
            continue
        name = Path(str(relative)).stem.replace("-", " ").title()
        mime, extension = _detect_image(source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        existing = db.execute("SELECT country_code FROM country_flags WHERE country_code=?", (code,)).fetchone()
        target_dir = _asset_root() / "flags"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{code.lower()}{extension}"
        if not existing:
            shutil.copyfile(source, target)
            try:
                with db:
                    db.execute(
                        """INSERT INTO country_flags(country_code,name,file_path,mime_type,sha256,source_url,
                                  license,credit,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (code, name, str(target), mime, digest, "https://fclogo.top/logos",
                         FCLOGO_PERMISSION, "FCLOGO", utc_now()),
                    )
            except Exception:
                target.unlink(missing_ok=True)
                raise
            copied += 1
        aliases = [code, name, *COUNTRY_TRANSLATIONS.get(code, ()), f"{name} national team",
                   f"national team of {name}"]
        for alias in aliases:
            _register_flag_alias(db, code, alias)
    return copied, skipped


def _source_info(directory: Path) -> dict[str, object]:
    import yaml

    path = directory / "info" / "info.yaml"
    if not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    records = document if isinstance(document, list) else [document]
    return next((x for x in records if isinstance(x, dict) and x.get("data") == "sourceInfo"), {})


def _logo_records(directory: Path) -> list[dict[str, object]]:
    import yaml

    path = directory / "logo.yaml"
    if not path.is_file():
        return []
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [x for x in (document if isinstance(document, list) else [document])
            if isinstance(x, dict) and x.get("data") == "logo"]


def _pick_team_png(directory: Path) -> dict[str, object] | None:
    records = _logo_records(directory)
    current = [x for x in records if not x.get("isDoubtful") and not x.get("isOutdated")]
    preferred = [x for x in current if x.get("style") == "color"]
    for record in [*preferred, *current]:
        relative = record.get("pngPath")
        if relative and (directory / str(relative)).is_file():
            return record
    return None


def _team_aliases(directory: Path, source_info: dict[str, object], logo: dict[str, object]) -> list[str]:
    aliases: list[str] = []
    info = source_info.get("info")
    if isinstance(info, list):
        for item in info:
            if isinstance(item, dict):
                aliases.extend(str(item[k]) for k in ("fullName", "localName", "shortName") if item.get(k))
    folder_name = re.sub(r"^\d+[_-]?", "", directory.name).strip()
    aliases.append(folder_name)
    versions = logo.get("verName")
    if isinstance(versions, list):
        aliases.extend(str(value) for value in versions)
    return list(dict.fromkeys(x.strip() for x in aliases if x.strip()))


def import_fclogo_collection(db: sqlite3.Connection, root: Path) -> dict[str, int]:
    """Import current, non-doubtful team PNGs and country flags from a permissioned local copy."""
    root = root.resolve()
    logos_root = root / "logos"
    flags_yaml = _find_flags_yaml(root)
    if not logos_root.is_dir() or flags_yaml is None:
        raise ValueError("Folder must contain logos/ and countries/flags.yaml from the local FCLOGO collection")
    imported = duplicates = skipped = 0
    directories = sorted([*logos_root.glob("*/clubs/*"), *logos_root.glob("*/team/*")])
    for directory in directories:
        if not directory.is_dir():
            continue
        logo = _pick_team_png(directory)
        if not logo:
            skipped += 1
            continue
        source_info = _source_info(directory)
        organization = directory.relative_to(logos_root).parts[0].lower()
        source_id = str(source_info.get("sourceID") or directory.name)
        key = _safe_key(f"{organization}-{source_id}") or organization
        names = _team_aliases(directory, source_info, logo)
        name = next((str(x.get("shortName")) for x in (source_info.get("info") or [])
                     if isinstance(x, dict) and x.get("shortName")), None) if isinstance(source_info.get("info"), list) else None
        name = name or names[0] if names else directory.name
        reference = str(logo.get("reference") or source_info.get("websiteURL") or "https://fclogo.top/logos")
        source = directory / str(logo["pngPath"])
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        try:
            add_asset(db, source, "crest", name, FCLOGO_PERMISSION, key, reference, "FCLOGO", names)
            imported += 1
        except ValueError as exc:
            existing = db.execute("SELECT id FROM media_assets WHERE sha256=?", (digest,)).fetchone()
            if existing:
                for alias in names:
                    _register_team_alias(db, int(existing["id"]), alias)
                duplicates += 1
            else:
                LOGGER.warning("skipping team logo path=%s error=%s", source.relative_to(root), exc)
                skipped += 1
    imported_flags, skipped_flags = _import_country_flags(db, flags_yaml)
    return {"team_imported": imported, "team_duplicates": duplicates, "team_skipped": skipped,
            "flags_imported": imported_flags, "flags_skipped": skipped_flags}


def add_asset(
    db: sqlite3.Connection,
    source_path: Path,
    asset_type: str,
    name: str,
    license_name: str,
    asset_key: str | None = None,
    source_url: str | None = None,
    credit: str | None = None,
    aliases: list[str] | None = None,
) -> int:
    if asset_type not in {"background", "crest"}:
        raise ValueError("Asset type must be background or crest")
    if not name.strip():
        raise ValueError("Asset name is required")
    if not license_name.strip():
        raise ValueError("License or usage permission is required")
    key = _safe_key(asset_key)
    if asset_type == "crest" and not key:
        raise ValueError("A crest requires a team key, for example zenit")
    if asset_type == "background" and key:
        raise ValueError("Background assets do not use a team key")

    mime, extension = _detect_image(source_path)
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    duplicate = db.execute("SELECT id FROM media_assets WHERE sha256=?", (digest,)).fetchone()
    if duplicate:
        raise ValueError(f"This image is already in the library as asset {duplicate['id']}")

    target_dir = _asset_root() / ("backgrounds" if asset_type == "background" else "crests")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{key or 'background'}-{digest[:16]}{extension}"
    if target.exists():
        raise ValueError("Asset file already exists but is not registered; inspect it before importing")
    shutil.copyfile(source_path, target)
    try:
        with db:
            cursor = db.execute(
                """INSERT INTO media_assets
                   (asset_type,asset_key,name,file_path,mime_type,sha256,source_url,license,credit,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (asset_type, key, name.strip(), str(target), mime, digest,
                 source_url.strip() or None if source_url else None, license_name.strip(),
                 credit.strip() or None if credit else None, utc_now()),
            )
            asset_id = int(cursor.lastrowid)
            if asset_type == "crest":
                values = [name, key or "", *(aliases or [])]
                seen_aliases = set()
                for alias in values:
                    display = alias.strip()
                    normalized = _alias_norm(display)
                    if not normalized or normalized in seen_aliases:
                        continue
                    seen_aliases.add(normalized)
                    _register_team_alias(db, asset_id, display)
            return asset_id
    except Exception:
        target.unlink(missing_ok=True)
        raise


def import_background_directory(
    db: sqlite3.Connection,
    root: Path,
    license_name: str,
    credit: str | None = None,
    source_url: str | None = None,
) -> dict[str, int]:
    """Import direct JPEG/PNG children idempotently into the persistent media library."""
    if not license_name.strip():
        raise ValueError("License or usage permission is required")
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Background folder does not exist: {root}")

    imported = duplicates = skipped = 0
    for source in sorted(path for path in root.iterdir() if path.is_file()):
        try:
            _detect_image(source)
        except ValueError as exc:
            LOGGER.warning("skipping background path=%s error=%s", source.name, exc)
            skipped += 1
            continue
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        existing = db.execute("SELECT id FROM media_assets WHERE sha256=?", (digest,)).fetchone()
        if existing:
            duplicates += 1
            continue
        name = source.stem.replace("_", " ").replace("-", " ").strip().title()
        add_asset(db, source, "background", name, license_name,
                  source_url=source_url, credit=credit)
        imported += 1
    return {"imported": imported, "duplicates": duplicates, "skipped": skipped}

def list_assets(db: sqlite3.Connection, asset_type: str | None = None) -> list[sqlite3.Row]:
    if asset_type and asset_type not in {"background", "crest"}:
        raise ValueError("Asset type must be background or crest")
    if asset_type:
        return db.execute("SELECT * FROM media_assets WHERE asset_type=? ORDER BY asset_key,name", (asset_type,)).fetchall()
    return db.execute("SELECT * FROM media_assets ORDER BY asset_type,asset_key,name").fetchall()


def find_crest(db: sqlite3.Connection, team_name: str) -> sqlite3.Row | None:
    from .editorial import team_lookup_forms

    forms = team_lookup_forms(team_name)
    if not forms:
        return None
    marks = ",".join("?" for _ in forms)
    row = db.execute(
        f"""SELECT a.* FROM media_assets a
           LEFT JOIN media_asset_aliases x ON x.asset_id=a.id
           WHERE a.asset_type='crest' AND a.enabled=1 AND x.alias_norm IN ({marks})
           ORDER BY a.id LIMIT 1""",
        forms,
    ).fetchone()
    if row:
        with db:
            db.execute("UPDATE media_assets SET last_used_at=? WHERE id=?", (utc_now(), row["id"]))
        return row
    # Transliterated substring fallback over registered aliases (e.g. «Рома» -> "AS Roma").
    latin = [f for f in forms if re.fullmatch(r"[0-9a-z]{4,}", f)]
    if not latin:
        return None
    rows = db.execute(
        """SELECT a.*, x.alias_norm AS an FROM media_assets a
           JOIN media_asset_aliases x ON x.asset_id=a.id
           WHERE a.asset_type='crest' AND a.enabled=1""",
    ).fetchall()
    for lat in latin:
        hits = sorted((r for r in rows if lat in (r["an"] or "")), key=lambda r: len(r["an"] or ""))
        if hits:
            with db:
                db.execute("UPDATE media_assets SET last_used_at=? WHERE id=?", (utc_now(), hits[0]["id"]))
            return hits[0]
    return None


def choose_background(db: sqlite3.Connection, asset_id: int | None = None) -> sqlite3.Row | None:
    if asset_id is not None:
        row = db.execute(
            "SELECT * FROM media_assets WHERE id=? AND asset_type='background' AND enabled=1", (asset_id,)
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM media_assets WHERE asset_type='background' AND enabled=1 ORDER BY random() LIMIT 1"
        ).fetchone()
    if row and Path(row["file_path"]).is_file():
        with db:
            db.execute("UPDATE media_assets SET last_used_at=? WHERE id=?", (utc_now(), row["id"]))
        return row
    return None


def remove_asset(db: sqlite3.Connection, asset_id: int) -> None:
    row = db.execute("SELECT file_path FROM media_assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        raise ValueError(f"Asset {asset_id} does not exist")
    path = Path(row["file_path"]).resolve()
    root = _asset_root().resolve()
    if root not in path.parents:
        raise ValueError("Refusing to delete a file outside the managed asset library")
    path.unlink(missing_ok=True)
    with db:
        db.execute("DELETE FROM media_assets WHERE id=?", (asset_id,))


def _main() -> None:
    parser = argparse.ArgumentParser(description="Manage reusable NewsBot backgrounds and football crests.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--add", type=Path, metavar="IMAGE", help="Import a JPEG or PNG from /imports or /logos")
    action.add_argument("--list", action="store_true", help="List registered assets")
    action.add_argument("--remove", type=int, metavar="ASSET_ID", help="Remove an asset and its managed file")
    action.add_argument("--import-fclogo", type=Path, metavar="DIR", help="Import permitted team PNGs and country flags from a local FCLOGO copy")
    action.add_argument("--import-backgrounds", type=Path, metavar="DIR", help="Import JPEG/PNG backgrounds from a folder into the persistent library")
    parser.add_argument("--type", choices=("background", "crest"))
    parser.add_argument("--name")
    parser.add_argument("--team-key", help="Stable team key for a crest, e.g. zenit")
    parser.add_argument("--aliases", help="Semicolon-separated team names/aliases, including common Russian spelling")
    parser.add_argument("--license", dest="license_name", help="License or written permission basis")
    parser.add_argument("--credit", help="Required credit line, if applicable")
    parser.add_argument("--source-url", help="Original asset page or permission record URL")
    args = parser.parse_args()
    _, db_path, _ = runtime_paths()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = connect(db_path)
    try:
        if args.add:
            if not all((args.type, args.name, args.license_name)):
                parser.error("--add requires --type, --name and --license")
            asset_id = add_asset(db, args.add, args.type, args.name, args.license_name,
                                 args.team_key, args.source_url, args.credit,
                                 [value.strip() for value in (args.aliases or "").split(";") if value.strip()])
            print(f"Добавлен ресурс #{asset_id}: {args.name} ({args.type})")
        elif args.remove is not None:
            remove_asset(db, args.remove)
            print(f"Удалён ресурс #{args.remove}")
        elif args.import_backgrounds is not None:
            if not args.license_name:
                parser.error("--import-backgrounds requires --license")
            counts = import_background_directory(db, args.import_backgrounds, args.license_name,
                                                 args.credit, args.source_url)
            print("Импорт фонов: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        elif args.import_fclogo is not None:
            counts = import_fclogo_collection(db, args.import_fclogo)
            print("Импорт FCLOGO: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        else:
            rows = list_assets(db)
            for row in rows:
                print(f"{row['id']}\t{row['asset_type']}\t{row['asset_key'] or '-'}\t{row['name']}\t{row['license']}\t{row['file_path']}")
            if not rows:
                print("Библиотека изображений пока пуста.")
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        db.close()


if __name__ == "__main__":
    _main()
