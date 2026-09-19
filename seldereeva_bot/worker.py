"""Entrypoint: `python -m seldereeva_bot.worker`. Runs the Telegram bot
(polling) and the scheduled jobs in one process — same reasoning as
kim_bot/worker.py: single-brand load, no shared-process crash risk to design
around here."""
import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config
from .bot import build_bot, build_dispatcher, set_commands
from .db import init_db
from .scheduled_jobs import (
    send_ads_digest, send_finance_digest, send_kiz_export, send_rollup_digest,
    send_stock_digest, send_weekly_dashboard,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("seldereeva_bot.worker")


async def main():
    init_db()

    token = config.BOT_TOKEN or os.environ["SELD_BOT_TOKEN"]
    bot = build_bot(token)
    dp = build_dispatcher(config.MINI_APP_URL)
    await set_commands(bot)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_finance_digest, "cron", hour=9, minute=0, args=[bot])
    scheduler.add_job(send_ads_digest, "cron", hour=9, minute=1, args=[bot])
    scheduler.add_job(send_stock_digest, "cron", hour=9, minute=2, args=[bot])
    scheduler.add_job(send_rollup_digest, "cron", hour=9, minute=5, args=[bot])
    scheduler.add_job(send_kiz_export, "cron", hour=9, minute=0, args=[bot])
    scheduler.add_job(send_weekly_dashboard, "cron", day_of_week="mon", hour=8, minute=30, args=[bot])
    scheduler.start()
    log.info(
        "seldereeva_bot started: сводки в 9:00/9:01/9:02, итог в 9:05, КИЗ-выгрузка в 9:00 (если настроена), "
        "дашборд по понедельникам в 8:30"
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
