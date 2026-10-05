from __future__ import annotations
import argparse, asyncio, logging
from datetime import datetime, timezone
from .collector import run_collector
from .config import load_sources, runtime_paths
from .db import connect, sync_sources
from .workflow import index_untracked_articles
from .retention import cleanup_old_content

def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")

async def main() -> None:
    parser = argparse.ArgumentParser(description="Collect RSS items into SQLite; never publishes.")
    parser.add_argument("--once",action="store_true",help="collect one cycle and exit")
    args = parser.parse_args()
    sources_path,db_path,poll_seconds = runtime_paths()
    sources = load_sources(sources_path)
    enabled = [s for s in sources if s.get("enabled") and s.get("scope")=="association_football"]
    configure_logging()
    logger = logging.getLogger("newsbot")
    logger.info("starting collect-only mode feeds=%d database=%s interval=%ds",len(enabled),db_path,poll_seconds)
    connection = connect(db_path)
    sync_sources(connection,sources,datetime.now(timezone.utc).isoformat(timespec="seconds"))
    indexed = index_untracked_articles(connection)
    if indexed: logger.info("indexed %d existing articles into the review queue",indexed)
    cleanup_old_content(connection)
    try: await run_collector(connection,sources,poll_seconds,args.once)
    finally: connection.close()

if __name__ == "__main__": asyncio.run(main())
