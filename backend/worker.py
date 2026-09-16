"""Standalone background-sync process — a separate Railway service from the
FastAPI/bot "web" service, sharing the same Postgres. Runs only the
scheduled jobs that talk to Ozon/WB (backend/scheduled_jobs.py): promo
checks, margin checks, dimension checks, and the WB/Ozon sales-cache
refresh.

Why this exists (2026-09-13): the app crashed three separate times in one
night, each traced to a different specific mechanism, but all sharing one
root cause — the live Mini App API, the Telegram bot, and this background
sync all ran in the same process on the same Railway instance. When Ozon
rate-limits an account for hours, these jobs keep failing/retrying, and
because everything shared one process and one restart lifecycle, a stuck
or crash-looping sync job took the whole app down with it, including parts
that have nothing to do with syncing. Splitting sync into its own process
means a bad night for Ozon/WB degrades data freshness, not uptime — `web`
(backend/ai_engine_app.py) stays up regardless of what this process is
doing.

Does NOT run `Dispatcher.start_polling` — only `web` polls Telegram for
updates. Two processes both calling `getUpdates` on the same bot token
would each see "Conflict: terminated by other getUpdates request" from
Telegram. This process only sends outbound notifications (bot.send_message
via the job functions), which doesn't need polling."""
import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .db import init_db
from .scheduled_jobs import (
    _refresh_ozon_caches,
    _refresh_wb_caches,
    _run_dimension_checks,
    _run_margin_checks,
    _run_promo_checks,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")


async def main():
    init_db()

    token = os.environ.get("AI_ENGINE_BOT_TOKEN")
    bot = None
    if token:
        from .ai_engine_bot import build_bot
        bot = build_bot(token)
    else:
        log.warning("AI_ENGINE_BOT_TOKEN not set — jobs will run but can't send Telegram notifications")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(_run_promo_checks, "interval", minutes=10, args=[bot])
    scheduler.add_job(_run_margin_checks, "interval", hours=1, args=[bot])
    scheduler.add_job(_run_dimension_checks, "interval", hours=24, args=[bot])
    scheduler.add_job(_refresh_wb_caches, "interval", hours=3)
    scheduler.add_job(_refresh_ozon_caches, "interval", hours=3)
    scheduler.start()
    log.info("Worker started: акции every 10 min, маржа every hour, габариты once a day, WB+Ozon sales cache every 3 hours")

    # Same immediate first-pass kickoff `web` used to do at its own startup —
    # no reason to wait up to 3h for the first real data after a deploy.
    #
    # Sequential, NOT asyncio.gather — found live (2026-09-14): the worker
    # was stuck in a tight crash-restart loop for ~24h straight after every
    # deploy, dying every ~30s during this exact startup kickoff, always
    # right around cabinet 8 (26k+ postings) — no Python traceback ever
    # shown for the actual death, consistent with an OOM kill from running
    # WB's report fetch and Ozon's largest cabinet's fetch fully
    # concurrently right at cold start. Sequential halved peak memory here.
    #
    # WB is skipped at kickoff entirely (2026-09-16) — it kept crash-looping
    # again even sequential. Unlike Ozon's cache (hot-window + backfill
    # chunks, bounded per cycle, see ozon_sales_cache.refresh), WB's own
    # cache refresh is all-or-nothing: one throttled call per report chunk
    # (WB's finance API is hard-limited to 1/min — see margin.fetch_rows),
    # routinely 50+ chunks for a real cabinet, so one full attempt can run
    # to an hour and a single failure anywhere in it loses the whole run's
    # progress, nothing partial gets saved. Running that on every automatic
    # restart meant a crash-looping worker (WB ConnectionReset errors,
    # 2026-09-15/16, cabinet 7 needing 54 chunks) kept retrying the exact
    # same expensive fetch from scratch every ~10 minutes, never giving the
    # rest of the worker (акции/маржа/габариты) a stable window to run at
    # all. WB's cache still refreshes on its own 3h interval below — this
    # only removes it from the kickoff path that reruns on every restart.
    # A real fix (an incremental design for WB like Ozon's) is a separate,
    # bigger task — this just stops the crash loop.
    await _refresh_ozon_caches(skip_if_fresh=True)
    log.info("Initial Ozon sales cache refresh done (WB skipped at kickoff — refreshes on its own 3h interval instead)")

    await asyncio.Event().wait()  # run forever — the scheduler does its work on its own tasks


if __name__ == "__main__":
    asyncio.run(main())
