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
from .scheduled_jobs import afternoon_followup, check_reviews, daily_stock_summary, morning_alert, sync_article_map, sync_orders, poll_returns

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kim_bot.worker")

# Explicit timezone for every cron job — the ambient system clock differs
# between local dev (this machine runs in MSK) and Railway (containers
# default to UTC), so "hour=9" meant different real times in each without
# this. All her cron times are meant literally as Moscow time.
MSK = "Europe/Moscow"


async def main():
    init_db()

    token = config.BOT_TOKEN or os.environ["KIM_BOT_TOKEN"]
    bot = build_bot(token)
    dp = build_dispatcher()
    await set_commands(bot)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(sync_orders, "interval", minutes=15)
    scheduler.add_job(poll_returns, "interval", hours=4)
    scheduler.add_job(sync_article_map, "interval", hours=6)
    scheduler.add_job(check_reviews, "interval", minutes=30, args=[bot])
    scheduler.add_job(morning_alert, "cron", hour=9, minute=0, timezone=MSK, args=[bot])
    scheduler.add_job(afternoon_followup, "cron", hour=15, minute=0, timezone=MSK, args=[bot])
    scheduler.add_job(daily_stock_summary, "cron", hour=9, minute=0, timezone=MSK, args=[bot])
    scheduler.start()
    log.info(
        "kim_bot started: синк заказов каждые 15 мин, возвраты каждые 4ч, артикулы каждые 6ч, "
        "отзывы каждые 30 мин, отбивка по сборке в 09:00 и 15:00 МСК, сверка остатков в 09:00 МСК"
    )

    await asyncio.gather(
        dp.start_polling(bot),
        _initial_kickoff(bot),
    )


async def _initial_kickoff(bot):
    """First pass right at startup, same reasoning as backend/worker.py's
    kickoff — no reason to wait up to 15/30 min for the first real data.
    Never sends SLA alerts here — those only fire on their fixed 09:00/15:00
    schedule; check_reviews is safe to include since it only ever notifies
    about reviews not already tracked (idempotent)."""
    await sync_article_map()
    await sync_orders()
    await poll_returns()
    await check_reviews(bot)


if __name__ == "__main__":
    asyncio.run(main())
