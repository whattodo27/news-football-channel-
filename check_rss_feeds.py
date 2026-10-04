#!/usr/bin/env python3
"""Check configured RSS endpoints from the machine/container where this runs."""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

import feedparser
import httpx
import yaml


async def check_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    source: dict[str, Any],
) -> dict[str, Any]:
    async with semaphore:
        result: dict[str, Any] = {
            "id": source.get("id", "<missing-id>"),
            "name": source.get("name", "<unnamed>"),
            "url": source.get("url", ""),
            "enabled": source.get("enabled", False),
        }
        started = time.monotonic()
        try:
            response = await client.get(result["url"])
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            result["status"] = response.status_code
            result["final_url"] = str(response.url)
            result["content_type"] = response.headers.get("content-type", "")
            response.raise_for_status()

            parsed = feedparser.parse(response.content)
            result["entries"] = len(parsed.entries)
            result["sample_title"] = (
                parsed.entries[0].get("title", "").strip()
                if parsed.entries
                else ""
            )
            result["parse_warning"] = bool(parsed.bozo)
            if parsed.bozo:
                result["parse_error"] = str(parsed.bozo_exception)[:240]
            is_xml = "xml" in result["content_type"].lower()
            has_feed_metadata = bool(parsed.version or parsed.feed)
            is_feed_response = is_xml or has_feed_metadata

            # feedparser's bozo flag is diagnostic, not a hard failure: feeds
            # can have recoverable XML quirks and still yield usable entries.
            if parsed.entries and is_feed_response:
                result["state"] = "ok_with_parse_warning" if parsed.bozo else "ok"
                result["ok"] = True
            elif is_feed_response:
                result["state"] = "valid_but_empty"
                result["ok"] = False
                result["note"] = "The response looks like a feed but contains no entries; retry later before disabling it."
            else:
                result["state"] = (
                    "http_challenge"
                    if response.status_code == 202
                    or "html" in result["content_type"].lower()
                    else "not_a_feed"
                )
                result["ok"] = False
                result["note"] = "The HTTP response was successful but did not look like an RSS/Atom feed; it may be a bot challenge or a retired feed URL."
        except httpx.HTTPStatusError as exc:
            result["ok"] = False
            result["state"] = "http_error"
            result["error"] = f"HTTP {exc.response.status_code}"
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after:
                result["retry_after"] = retry_after
        except httpx.TimeoutException:
            result["ok"] = False
            result["state"] = "timeout"
            result["error"] = "timeout"
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        except httpx.TransportError as exc:
            result["ok"] = False
            result["state"] = "transport_error"
            result["error"] = f"transport error: {type(exc).__name__}: {exc}"
            if "ssl" in str(exc).lower() or "certificate" in str(exc).lower():
                result["note"] = "TLS verification failed. Do not disable certificate verification; fix the endpoint/certificate or exclude the feed."
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        except httpx.HTTPError as exc:
            result["ok"] = False
            result["state"] = "network_error"
            result["error"] = f"network error: {type(exc).__name__}: {exc}"
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        except Exception as exc:  # Keep checking other feeds if one response is malformed.
            result["ok"] = False
            result["state"] = "check_error"
            result["error"] = f"check error: {type(exc).__name__}: {exc}"
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return result


async def main_async(sources_path: Path) -> int:
    with sources_path.open("r", encoding="utf-8") as file:
        document = yaml.safe_load(file) or {}
    sources = document.get("sources", [])
    if not isinstance(sources, list) or not sources:
        raise SystemExit("No source entries found under the 'sources' key.")

    # This is an inventory check, so allow slow official endpoints more time.
    # The production collector can use a shorter per-request timeout.
    timeout = httpx.Timeout(25.0, connect=10.0)
    headers = {
        "User-Agent": (
            "NewsBot-RSS-Collector/1.0 "
            "(+https://t.me/vasiliyd27; contact: mailto:galaxtv1@gmail.com)"
        )
    }
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        results = await asyncio.gather(
            *(check_one(client, semaphore, source) for source in sources)
        )

    print(f"Checked {len(results)} feed URLs from {sources_path}\n")
    for item in results:
        state = item.get("state", "ok" if item.get("ok") else "fail").upper()
        print(f"[{state}] {item['id']} — {item['name']}")
        print(f"  enabled={item['enabled']}  status={item.get('status', '-')}  entries={item.get('entries', '-')}  elapsed={item.get('elapsed_seconds', '-')}s  content-type={item.get('content_type', '-')}")
        print(f"  url={item['url']}")
        if item.get("final_url") and item["final_url"] != item["url"]:
            print(f"  final_url={item['final_url']}")
        if item.get("sample_title"):
            print(f"  sample={item['sample_title']}")
        if item.get("error"):
            print(f"  error={item['error']}")
        if item.get("parse_error"):
            print(f"  parse_error={item['parse_error']}")
        if item.get("parse_warning"):
            print("  note=Feedparser reported a recoverable parse warning; entries are still usable.")
        if item.get("note"):
            print(f"  note={item['note']}")
        print()

    passed = sum(bool(item.get("ok")) for item in results)
    print(f"Summary: {passed}/{len(results)} feeds returned feed-like content with entries. Empty feeds, challenges, HTTP errors, TLS errors, and timeouts are reported separately.")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path("sources.yaml"),
        help="Path to sources.yaml (default: ./sources.yaml)",
    )
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(arguments.sources)))
