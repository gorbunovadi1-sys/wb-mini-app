import asyncio
import datetime
import logging

from aiogram.types import BufferedInputFile
from backend.wb_client import WBClient

from . import config, cost_prices, dashboard_builder, digests, kiz_export

log = logging.getLogger("seldereeva_bot.scheduled_jobs")

_last_texts = {}  # cache of today's digest texts, for the rollup job right after


async def _notify_all(bot, text: str):
    for chat_id in config.notify_chat_ids():
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception(f"Failed to notify chat {chat_id}")


async def send_finance_digest(bot):
    text = await asyncio.to_thread(digests.finance_digest_text)
    _last_texts["finance"] = text
    await _notify_all(bot, text)


async def send_ads_digest(bot):
    text = await asyncio.to_thread(digests.ads_digest_text)
    _last_texts["ads"] = text
    await _notify_all(bot, text)


async def send_stock_digest(bot):
    text = await asyncio.to_thread(digests.stock_digest_text)
    _last_texts["stock"] = text
    await _notify_all(bot, text)


async def send_rollup_digest(bot):
    """Runs right after the three individual digests — reuses their text
    from this run if available, computes fresh otherwise (e.g. after a
    restart where the cache is empty)."""
    finance = _last_texts.get("finance") or await asyncio.to_thread(digests.finance_digest_text)
    ads = _last_texts.get("ads") or await asyncio.to_thread(digests.ads_digest_text)
    stock = _last_texts.get("stock") or await asyncio.to_thread(digests.stock_digest_text)
    text = digests.rollup_digest_text(finance, ads, stock)
    await _notify_all(bot, text)


async def send_weekly_dashboard(bot):
    """Понедельник — точная копия присланного дашборда (тот же шаблон/CSS/JS
    из скилла wb-weekly-dashboard, фильтры и раскрывающиеся строки включены)
    живыми данными по WB API, без ручной выгрузки отчёта на рабочий стол.
    См. seldereeva_bot/dashboard_builder.py — там же маппинг полей отчёта."""
    if not config.WB_API_KEY:
        log.info("Понедельничный дашборд пропущен — SELD_WB_API_KEY не настроен")
        return
    client = WBClient(config.WB_API_KEY)
    try:
        html, problems = await asyncio.to_thread(
            dashboard_builder.build_dashboard_html, client, cost_prices.load_cost_prices("wb"),
            "Сельдереева · неделя на WB", 1,
        )
    except Exception:
        log.exception("Не удалось собрать понедельничный дашборд")
        await _notify_all(bot, "⚠️ Не удалось собрать еженедельный дашборд — проверь логи.")
        return

    today = datetime.date.today().isoformat()
    monday, sunday = dashboard_builder.last_complete_report_week(1)
    caption = f"📊 Дашборд Сельдереевой за {monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}"
    if problems:
        # Сверка не сошлась — файл всё равно отправляется (лучше показать с
        # предупреждением, чем молчать), но так, чтобы это было заметно, а не
        # спрятано в логах (см. skill/pitfalls.md: "Ошибки должны быть видимыми").
        caption += f"\n⚠️ Сверка не сошлась: {', '.join(problems)} — цифрам за эту неделю не доверяй, разберусь."
    for chat_id in config.notify_chat_ids():
        try:
            await bot.send_document(
                chat_id, BufferedInputFile(html.encode("utf-8"), filename=f"dashboard_{today}.html"),
                caption=caption,
            )
        except Exception:
            log.exception(f"Failed to send weekly dashboard to chat {chat_id}")


async def send_kiz_export(bot):
    """9:00 — файлы для Честного Знака. Пока не настроено (см.
    kiz_export.py) — шлёт понятное сообщение вместо тихого no-op, чтобы было
    видно, что джоба жива и ждёт открытых вопросов, а не сломана."""
    try:
        withdrawal_bytes, return_bytes = await asyncio.to_thread(kiz_export.build_withdrawal_and_return_files)
    except kiz_export.KizExportNotConfigured as e:
        log.info(f"КИЗ-выгрузка пропущена: {e}")
        return
    except Exception:
        log.exception("КИЗ-выгрузка упала")
        await _notify_all(bot, "⚠️ Не удалось собрать выгрузку КИЗ — проверь логи.")
        return

    from aiogram.types import BufferedInputFile
    import datetime
    today = datetime.date.today().isoformat()
    for chat_id in config.notify_chat_ids():
        try:
            await bot.send_document(chat_id, BufferedInputFile(withdrawal_bytes, filename=f"kiz_vyvod_{today}.xlsx"), caption="КИЗ на вывод из оборота")
            await bot.send_document(chat_id, BufferedInputFile(return_bytes, filename=f"kiz_vozvrat_{today}.xlsx"), caption="КИЗ на возврат в оборот")
        except Exception:
            log.exception(f"Failed to send КИЗ files to chat {chat_id}")
