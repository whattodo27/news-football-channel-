from __future__ import annotations
import argparse, asyncio, logging, os
from datetime import datetime, timezone
from .collector import run_collector
from .config import load_sources, runtime_paths
from .db import connect, sync_sources
from .workflow import index_untracked_articles
from .retention import cleanup_old_content

def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")

async def main() -> None:
    parser = argparse.ArgumentParser(description="Collect RSS items and run private Telegram editorial review.")
    parser.add_argument("--once",action="store_true",help="collect one cycle and exit")
    args = parser.parse_args()
    sources_path,db_path,poll_seconds = runtime_paths()
    sources = load_sources(sources_path)
    enabled = [s for s in sources if s.get("enabled") and s.get("scope")=="association_football"]
    configure_logging()
    logger = logging.getLogger("newsbot")
    logger.info("starting news service feeds=%d database=%s interval=%ds",len(enabled),db_path,poll_seconds)
    connection = connect(db_path)
    sync_sources(connection,sources,datetime.now(timezone.utc).isoformat(timespec="seconds"))
    from .publisher import recover_stale_publishing
    recovered = recover_stale_publishing(connection)
    if recovered:
        logger.info("recovered %d stale publishing event(s) on startup", recovered)
    indexed = index_untracked_articles(connection)
    if indexed: logger.info("indexed %d existing articles into the review queue",indexed)
    cleanup_old_content(connection)
    # Deferred: mem_limit 256m / _gradient_background Pillow cost; get_source unrated (fanat1k);
    # collect_cycle semaphore 2–4 for parallel RSS.
    review_task = None
    if not args.once and os.getenv("BOT_TOKEN"):
        from .telegram_review import run_review_bot
        review_task = asyncio.create_task(run_review_bot(db_path))
    try:
        await run_collector(connection,sources,poll_seconds,args.once,db_path)
    finally:
        if review_task:
            review_task.cancel()
            try:
                await review_task
            except asyncio.CancelledError:
                pass
        connection.close()

if __name__ == "__main__": asyncio.run(main())
