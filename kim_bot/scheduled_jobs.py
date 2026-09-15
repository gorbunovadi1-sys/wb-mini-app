import asyncio
import datetime
import logging

from aiogram.types import BufferedInputFile

from . import config, fulfillment_sheet, orders, reconciliation, returns, stock
from .ozon_client import OzonClient
from .wb_client import WBClient

log = logging.getLogger("kim_bot.scheduled_jobs")

MARKETPLACE_LABELS = {"wb": "Wildberries", "ozon": "Ozon"}


async def _notify_all(bot, text: str):
    for chat_id in config.notify_chat_ids():
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception(f"Failed to notify chat {chat_id}")


async def _send_document(bot, chat_id: str, file_bytes: bytes, filename: str, caption: str):
    try:
        await bot.send_document(chat_id, BufferedInputFile(file_bytes, filename=filename), caption=caption)
    except Exception:
        log.exception(f"Failed to send document to chat {chat_id}")


def _build_wb_client():
    return WBClient(config.WB_API_KEY) if config.WB_API_KEY else None


def _build_ozon_client():
    return OzonClient(config.OZON_CLIENT_ID, config.OZON_API_KEY) if (config.OZON_CLIENT_ID and config.OZON_API_KEY) else None


def _sync_since() -> str:
    """How far back order/return polling needs to look — the baseline date
    if she's uploaded one (everything since then feeds the stock balance),
    else a fixed fallback so a fresh install still backfills something."""
    return stock.get_baseline_date() or (datetime.date.today() - datetime.timedelta(days=45)).isoformat()


async def poll_orders_and_alert(bot):
    """Runs every 15 min: refreshes order/posting status from both
    marketplaces (full backfill since the baseline date every time — WB's
    /orders doesn't carry status, so this isn't just "new" orders, see
    orders.sync_wb_orders), then sends (repeating, per project decision)
    alerts for anything still not sent to assembly past the SLA."""
    wb = _build_wb_client()
    ozon = _build_ozon_client()
    since_date = await asyncio.to_thread(_sync_since)

    if wb:
        try:
            await asyncio.to_thread(orders.sync_wb_orders, wb, since_date)
        except Exception:
            log.exception("WB order sync failed")
    if ozon:
        try:
            await asyncio.to_thread(orders.sync_ozon_orders, ozon, since_date)
        except Exception:
            log.exception("Ozon order sync failed")

    if config.is_quiet_hours_now():
        log.info(f"Within quiet hours ({config.QUIET_HOURS_START_MSK}:00–{config.QUIET_HOURS_END_MSK}:00 MSK) — skipping SLA alert check")
        return

    alerts = await asyncio.to_thread(orders.due_alerts)
    if not alerts:
        return
    lines = [
        f"• {MARKETPLACE_LABELS.get(a['marketplace'], a['marketplace'])} заказ {a['order_id']}"
        f" (арт. {a['article'] or '—'}) — {a['age_hours']} ч. без сборки"
        for a in alerts
    ]
    text = f"⏰ Просрочка сборки FBS ({len(alerts)}):\n\n" + "\n".join(lines)
    await _notify_all(bot, text)


async def poll_returns():
    """Runs every few hours — returns aren't time-critical for the SLA
    alert, only for the daily stock balance."""
    wb = _build_wb_client()
    ozon = _build_ozon_client()
    date_from = await asyncio.to_thread(_sync_since)

    if wb:
        try:
            await asyncio.to_thread(returns.sync_wb_returns, wb, date_from)
        except Exception:
            log.exception("WB returns sync failed")
    if ozon:
        try:
            await asyncio.to_thread(returns.sync_ozon_returns, ozon, date_from)
        except Exception:
            log.exception("Ozon returns sync failed")


async def sync_article_map():
    """Runs every few hours: refreshes the WB-article mapping from her
    fulfillment team's own Google Sheet (see fulfillment_sheet.py) — silent,
    no notification, just keeps articles.canonical_article() current."""
    try:
        await asyncio.to_thread(fulfillment_sheet.sync_article_map)
    except Exception:
        log.exception("Article map sync from fulfillment sheet failed")


async def daily_stock_summary(bot):
    """Runs once a day: recomputes the balance for every article, caches it,
    then sends the автосверка (Сводка/Остатки/Расхождения — API fact vs her
    fulfillment team's manual journal) to the managers chat only, per her
    call — this one doesn't go to личка/фулфилмент."""
    if not config.MANAGERS_CHAT_ID:
        log.warning("KIM_MANAGERS_CHAT_ID not set — skipping daily stock summary")
        return
    balances = await asyncio.to_thread(stock.compute_balances)
    if balances:
        await asyncio.to_thread(stock.save_daily_snapshot, balances)

    today = datetime.date.today().isoformat()
    try:
        report_bytes = await asyncio.to_thread(reconciliation.build_report)
    except Exception:
        log.exception("Failed to build reconciliation report")
        return
    await _send_document(
        bot, config.MANAGERS_CHAT_ID, report_bytes, f"sverka_{today}.xlsx",
        caption=f"📦 Автосверка отгрузок и остатков на {today}",
    )
