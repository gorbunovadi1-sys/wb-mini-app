import asyncio
import datetime
import logging

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from . import config, fulfillment_sheet, orders, reconciliation, returns, reviews, stock
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


async def sync_orders():
    """Runs every 15 min: refreshes order/posting status from both
    marketplaces (full backfill since the baseline date every time — WB's
    /orders doesn't carry status, so this isn't just "new" orders, see
    orders.sync_wb_orders). No alerting here — see morning_alert/
    afternoon_followup for the twice-daily alert schedule."""
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


def _format_alert_lines(alerts: list[dict]) -> str:
    return "\n".join(
        f"• {MARKETPLACE_LABELS.get(a['marketplace'], a['marketplace'])} заказ {a['order_id']}"
        f" (арт. {a['article'] or '—'}) — {a['age']} без сборки"
        for a in alerts
    )


async def morning_alert(bot):
    """09:00 MSK: everything currently overdue. Marks these as "alerted
    today" so afternoon_followup knows which ones to re-check at 15:00.
    Goes only to the fulfillment chat, per her call — not managers/личка."""
    if not config.FULFILLMENT_CHAT_ID:
        log.warning("KIM_FULFILLMENT_CHAT_ID not set — skipping morning SLA alert")
        return
    alerts = await asyncio.to_thread(orders.overdue_orders)
    if not alerts:
        return
    await asyncio.to_thread(orders.mark_alerted, [(a["marketplace"], a["order_id"]) for a in alerts])
    text = f"⏰ Просрочка сборки FBS, утренняя сводка ({len(alerts)}):\n\n" + _format_alert_lines(alerts)
    await bot.send_message(config.FULFILLMENT_CHAT_ID, text)


async def afternoon_followup(bot):
    """15:00 MSK: of this morning's overdue orders, which are STILL not
    sent to assembly — deliberately not a fresh full scan, per her call:
    orders that only crossed the SLA after the morning digest wait for
    tomorrow's 09:00 rather than triggering a same-day alert of their own.
    Goes only to the fulfillment chat, same as the morning digest."""
    if not config.FULFILLMENT_CHAT_ID:
        return
    alerts = await asyncio.to_thread(orders.still_overdue_recently)
    if not alerts:
        return
    text = f"⏰ Всё ещё не собраны с утренней отбивки ({len(alerts)}):\n\n" + _format_alert_lines(alerts)
    await bot.send_message(config.FULFILLMENT_CHAT_ID, text)


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


def _review_keyboard(db_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=f"review_approve_{db_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"review_edit_{db_id}"),
        InlineKeyboardButton(text="❌ Пропустить", callback_data=f"review_skip_{db_id}"),
    ]])


async def check_reviews(bot):
    """Runs every 30 min: pulls new unanswered WB reviews, drafts a reply
    with Claude for each, and sends it to her personal chat for approval —
    nothing gets posted to WB until she taps «Отправить» (or edits first)."""
    wb = _build_wb_client()
    if not wb or not config.PERSONAL_CHAT_ID:
        return
    try:
        drafts = await asyncio.to_thread(reviews.sync_reviews, wb)
    except Exception:
        log.exception("WB review sync failed")
        return

    for d in drafts:
        stars = "⭐" * (d["rating"] or 0)
        header = f"📝 Новый отзыв {stars} — {d['product_name'] or d['article'] or '—'}\n\n«{d['review_text'] or '(без текста)'}»\n— {d['author_name'] or 'аноним'}"
        if d["draft_reply"]:
            text = f"{header}\n\nЧерновик ответа:\n{d['draft_reply']}"
        else:
            text = f"{header}\n\n⚠️ Не удалось сгенерировать черновик (нет ANTHROPIC_API_KEY или ошибка) — напиши текст вручную кнопкой «Изменить»."
        try:
            await bot.send_message(config.PERSONAL_CHAT_ID, text, reply_markup=_review_keyboard(d["db_id"]))
        except Exception:
            log.exception(f"Failed to send review draft {d['db_id']}")


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
