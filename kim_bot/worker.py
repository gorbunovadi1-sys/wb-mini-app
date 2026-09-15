"""Entrypoint: `python -m kim_bot.worker`. Runs the Telegram bot (polling)
and the scheduled jobs in one process — unlike backend/worker.py's split
from the main app, there's no shared-process crash risk to design around
here: this bot serves one cabinet, not many, so the load that caused those
past incidents (see project memory) doesn't apply."""
import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config
from .bot import build_bot, build_dispatcher, set_commands
from .db import init_db
from .scheduled_jobs import daily_stock_summary, poll_orders_and_alert, poll_returns, sync_article_map

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kim_bot.worker")


async def main():
    init_db()

    token = config.BOT_TOKEN or os.environ["KIM_BOT_TOKEN"]
    bot = build_bot(token)
    dp = build_dispatcher()
    await set_commands(bot)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_orders_and_alert, "interval", minutes=15, args=[bot])
    scheduler.add_job(poll_returns, "interval", hours=4)
    scheduler.add_job(sync_article_map, "interval", hours=6)
    scheduler.add_job(daily_stock_summary, "cron", hour=6, minute=0, args=[bot])
    scheduler.start()
    log.info("kim_bot started: заказы/сборка каждые 15 мин, возвраты каждые 4ч, артикулы каждые 6ч, сверка в 6:00")

    await asyncio.gather(
        dp.start_polling(bot),
        _initial_kickoff(bot),
    )


async def _initial_kickoff(bot):
    """First pass right at startup, same reasoning as backend/worker.py's
    kickoff — no reason to wait up to 15 min for the first real data."""
    await sync_article_map()
    await poll_orders_and_alert(bot)
    await poll_returns()


if __name__ == "__main__":
    asyncio.run(main())
