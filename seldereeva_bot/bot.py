import asyncio
import datetime
import logging
from typing import Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject, WebAppInfo,
)
from backend.wb_client import WBClient

from . import actions, config, cost_prices, dashboard_builder, digests, rate_limit

log = logging.getLogger("seldereeva_bot.bot")


class AccessControlMiddleware(BaseMiddleware):
    """Доступ закрыт по умолчанию — отвечает только из чатов, перечисленных
    в config.allowed_chat_ids() (личка Дарьи + явно разрешённые группы вроде
    «Seldereeva WB»). /chatid работает всегда — иначе неоткуда узнать id
    новой группы, чтобы её разрешить."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        chat = getattr(event, "chat", None)
        if chat is None:
            msg = getattr(event, "message", None)
            chat = getattr(msg, "chat", None)
        if chat is None:
            return await handler(event, data)

        text = getattr(event, "text", None)
        if text is None:
            msg = getattr(event, "message", None)
            text = getattr(msg, "text", None) or ""

        if (text or "").startswith("/chatid") or chat.id in config.allowed_chat_ids():
            return await handler(event, data)

        if (text or "").startswith("/start"):
            await event.answer("🔒 Доступ ограничён. Обратитесь к администратору.")
        return None


COMMANDS = [
    BotCommand(command="start", description="Что умеет бот"),
    BotCommand(command="finance", description="Финансы за вчера: /finance [N дней] — окно пошире"),
    BotCommand(command="ads", description="Сводка по рекламе сейчас"),
    BotCommand(command="stock", description="Сводка по остаткам сейчас"),
    BotCommand(command="exclude", description="Исключить фразу из кампании: /exclude id фраза"),
    BotCommand(command="dashboard", description="Дашборд за прошлую отчётную неделю: /dashboard [N недель назад]"),
    BotCommand(command="chatid", description="Id этого чата"),
]

_START_TEXT = (
    "👋 Бот для бренда Сельдереева.\n\n"
    "Каждое утро — сводки в этот чат: финансы, реклама, остатки.\n\n"
    "📊 Подробный дашборд (ABC, кластеры/фразы, управление кампаниями) — кнопка ниже.\n\n"
    "/finance, /ads, /stock — та же сводка по запросу, прямо сейчас\n"
    "/exclude — исключить фразу из рекламной кампании (с подтверждением)\n"
    "/chatid — id этого чата (для настройки уведомлений)"
)


async def set_commands(bot: Bot):
    await bot.set_my_commands(COMMANDS)


def build_bot(token: str) -> Bot:
    return Bot(token=token)


def _confirm_kb(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"seld_confirm:{action_id}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"seld_cancel:{action_id}"),
    ]])


def _week_label(weeks_back: int) -> str:
    monday, sunday = dashboard_builder.last_complete_report_week(weeks_back)
    return f"{monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}"


def _week_list_kb(offset: int) -> InlineKeyboardMarkup:
    """Список: видно сразу 5 недель с датами, один тап — сборка. Самая
    свежая неделя помечена отдельно, чтобы не искать её глазами в столбце
    одинаковых дат."""
    rows = []
    for n in range(offset + 1, offset + 6):
        icon = "🆕" if n == 1 else "📅"
        rows.append([InlineKeyboardButton(text=f"{icon}  {_week_label(n)}", callback_data=f"seld_pweek:{n}")])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="🔝 Сначала", callback_data="seld_pmore:0"))
    nav.append(InlineKeyboardButton(text="Показать ещё 5 ▾", callback_data=f"seld_pmore:{offset + 5}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


_QUEUE_NOTICE = (
    "\n⏳ Прямо сейчас считается другой отчёт (WB даёт 1 запрос в минуту на аккаунт, "
    "параллельно нельзя) — эта команда встала в очередь следом."
)


def _finance_eta_note(days) -> str:
    """WB отдаёт отчёт понедельными кусками по одному запросу в минуту —
    чем шире период, тем больше кусков и тем дольше ждать. Подтверждено
    живьём 2026-09-17: /finance 15 упёрся в 10 отдельных недельных
    отчётов подряд, это реально ~10+ минут, не «1-3», как раньше писалось
    без оглядки на days. days=None — режим «за вчера» (по умолчанию),
    самый быстрый вариант."""
    if days is None:
        return " обычно меньше минуты — один день, отчёт короткий."
    if days <= 8:
        return " обычно 1-3 минуты."
    weekly_chunks = -(-2 * days // 7)  # ceil(2*days/7) — margin.py тянет буфер в days назад от начала периода
    return f" период большой, WB отдаёт его частями по минуте — может занять ~{weekly_chunks}-{weekly_chunks + 3} минут."


async def _run_dashboard_build(message: Message, weeks_back: int):
    if not config.WB_API_KEY:
        await message.answer("SELD_WB_API_KEY не настроен — не могу собрать дашборд.")
        return
    label = _week_label(weeks_back)
    text = f"Собираю дашборд за {label}… обычно 1-3 минуты — WB ограничивает финансовый отчёт одним запросом в минуту."
    if rate_limit.wb_finance_lock.locked():
        text += _QUEUE_NOTICE
    working = await message.answer(text)
    client = WBClient(config.WB_API_KEY)
    try:
        html, problems = await asyncio.to_thread(
            dashboard_builder.build_dashboard_html, client, cost_prices.load_cost_prices("wb"),
            "Сельдереева · неделя на WB", weeks_back,
        )
    except Exception as e:
        log.exception("Не удалось собрать дашборд по запросу")
        await working.edit_text(f"Не получилось собрать дашборд за {label}: {e}")
        return

    today = datetime.date.today().isoformat()
    caption = f"📊 Дашборд за {label}"
    if problems:
        caption += f"\n⚠️ Сверка не сошлась: {', '.join(problems)} — цифрам не доверяй, разберусь."
    await message.answer_document(
        BufferedInputFile(html.encode("utf-8"), filename=f"dashboard_{today}.html"), caption=caption,
    )
    await working.delete()


def build_dispatcher(mini_app_url: Optional[str] = None) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AccessControlMiddleware())
    dp.callback_query.middleware(AccessControlMiddleware())

    @dp.message(CommandStart())
    async def start(message: Message):
        kb = None
        if mini_app_url:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📊 Открыть дашборд", web_app=WebAppInfo(url=mini_app_url)),
            ]])
        await message.answer(_START_TEXT, reply_markup=kb)

    @dp.message(Command("chatid"))
    async def chatid(message: Message):
        await message.answer(f"id этого чата: `{message.chat.id}`", parse_mode="Markdown")

    @dp.message(Command("finance"))
    async def finance_cmd(message: Message):
        parts = (message.text or "").split()
        days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        notice = _QUEUE_NOTICE if rate_limit.wb_finance_lock.locked() else ""
        working = await message.answer(f"Считаю…{_finance_eta_note(days)}{notice}")
        text = await asyncio.to_thread(digests.finance_digest_text, days)
        await working.edit_text(text)

    @dp.message(Command("ads"))
    async def ads_cmd(message: Message):
        notice = _QUEUE_NOTICE if rate_limit.wb_finance_lock.locked() else ""
        working = await message.answer(f"Считаю…{notice}")
        text = await asyncio.to_thread(digests.ads_digest_text)
        await working.edit_text(text)

    @dp.message(Command("stock"))
    async def stock_cmd(message: Message):
        notice = _QUEUE_NOTICE if rate_limit.wb_finance_lock.locked() else ""
        working = await message.answer(f"Считаю…{notice}")
        text = await asyncio.to_thread(digests.stock_digest_text)
        await working.edit_text(text)

    @dp.message(Command("dashboard"))
    async def dashboard_cmd(message: Message):
        if not config.WB_API_KEY:
            await message.answer("SELD_WB_API_KEY не настроен — не могу собрать дашборд.")
            return
        parts = (message.text or "").split()
        if len(parts) > 1 and parts[1].isdigit():
            # /dashboard N — прямой запуск, для тех, кто уже знает номер недели
            await _run_dashboard_build(message, int(parts[1]))
            return
        await message.answer(
            "📊 *Дашборд Сельдереевой*\nВыберите отчётную неделю (пн–вс):",
            reply_markup=_week_list_kb(0), parse_mode="Markdown",
        )

    @dp.callback_query(F.data.startswith("seld_pmore:"))
    async def pmore_cb(callback: CallbackQuery):
        offset = int(callback.data.split(":", 1)[1])
        await callback.message.edit_reply_markup(reply_markup=_week_list_kb(offset))
        await callback.answer()

    @dp.callback_query(F.data.startswith("seld_pweek:"))
    async def pweek_cb(callback: CallbackQuery):
        weeks_back = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await _run_dashboard_build(callback.message, weeks_back)

    @dp.message(Command("exclude"))
    async def exclude_cmd(message: Message):
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            await message.answer("Формат: /exclude <id кампании> <фраза>")
            return
        advert_id, norm_query = int(parts[1]), parts[2]
        action_id = await asyncio.to_thread(
            actions.create_pending_action, "exclude_phrase", f"campaign {advert_id}",
            {"advert_id": advert_id, "norm_query": norm_query}, "chat",
        )
        await message.answer(
            f"Исключить фразу «{norm_query}» из кампании {advert_id}?",
            reply_markup=_confirm_kb(action_id),
        )

    @dp.callback_query(F.data.startswith("seld_confirm:"))
    async def confirm_cb(callback: CallbackQuery):
        action_id = int(callback.data.split(":", 1)[1])
        result = await asyncio.to_thread(actions.confirm_and_execute, action_id)
        await callback.message.edit_text(f"{callback.message.text}\n\n➡️ {result}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("seld_cancel:"))
    async def cancel_cb(callback: CallbackQuery):
        action_id = int(callback.data.split(":", 1)[1])
        await asyncio.to_thread(actions.cancel_action, action_id)
        await callback.message.edit_text(f"{callback.message.text}\n\n➡️ Отменено.")
        await callback.answer()

    @dp.message(F.text.regexp(r"(?i)^(финанс|реклам|остат|дашборд)"))
    async def text_alias_cmd(message: Message):
        """Обычный текст без слэша — «финансы 14», «дашборд» и т.п. Команды
        в Telegram обязаны быть латиницей (/finance), а её из автокомплита
        видно только русское описание — отсюда путаница. Сами обработчики
        ниже не смотрят на первое слово команды, только на число вторым
        словом, так что их можно звать напрямую."""
        word = (message.text or "").split()[0].lower()
        if word.startswith("финанс"):
            await finance_cmd(message)
        elif word.startswith("реклам"):
            await ads_cmd(message)
        elif word.startswith("остат"):
            await stock_cmd(message)
        elif word.startswith("дашборд"):
            await dashboard_cmd(message)

    return dp


async def _main():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    token = config.BOT_TOKEN or os.environ["SELD_BOT_TOKEN"]
    bot = build_bot(token)
    dp = build_dispatcher(config.MINI_APP_URL)
    await set_commands(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_main())
