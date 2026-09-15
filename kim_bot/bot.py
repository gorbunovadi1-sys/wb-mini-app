import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message, TelegramObject

from . import config, fulfillment_sheet, reconciliation, stock

log = logging.getLogger("kim_bot.bot")


class AccessControlMiddleware(BaseMiddleware):
    """Refuses every update from a user not on config.ALLOWED_USER_IDS and
    not sent inside a chat on config.ALLOWED_CHAT_IDS (e.g. the managers
    group, where anyone she's added to the group can use the bot without
    being allowlisted individually) — without this, anyone who finds the
    bot's username on Telegram could read her stock/order data or overwrite
    the baseline via /template."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        user_ok = user and user.id in config.ALLOWED_USER_IDS
        chat_ok = chat and chat.id in config.ALLOWED_CHAT_IDS
        if not (user_ok or chat_ok):
            if isinstance(event, Message):
                await event.answer("🚫 Доступ к этому боту ограничен.")
            return
        return await handler(event, data)

COMMANDS = [
    BotCommand(command="start", description="Что умеет бот"),
    BotCommand(command="template", description="Шаблон для загрузки остатка"),
    BotCommand(command="stock", description="Текущий остаток по артикулам"),
    BotCommand(command="sverka", description="Автосверка отгрузок и остатков сейчас"),
    BotCommand(command="baseline_from_sheet", description="Взять стартовый остаток из Google-таблицы ФФ"),
    BotCommand(command="chatid", description="Id этого чата"),
]


async def set_commands(bot: Bot):
    await bot.set_my_commands(COMMANDS)

_START_TEXT = (
    "👋 Бот для склада фулфилмента.\n\n"
    "Что умеет:\n"
    "📦 Держит остаток по каждому артикулу — пришли .xlsx с остатком (кнопка /template даст шаблон), "
    "дальше я сам вычитаю отгрузки и прибавляю возвраты по WB и Ozon каждый день.\n"
    "⏰ Следит за сборкой FBS — если заказ не ушёл на сборку 8 часов, шлю сюда и в группу отбивку "
    "(и повторяю, пока не соберут).\n\n"
    "/template — шаблон для загрузки остатка\n"
    "/stock — текущий остаток по артикулам\n"
    "/sverka — автосверка отгрузок и остатков (факт из API vs журнал ФФ) прямо сейчас\n"
    "/chatid — id этого чата (для настройки уведомлений)"
)


def build_bot(token: str) -> Bot:
    return Bot(token=token)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(AccessControlMiddleware())
    dp.callback_query.outer_middleware(AccessControlMiddleware())

    @dp.message(CommandStart())
    async def start(message: Message):
        await message.answer(_START_TEXT)

    @dp.message(Command("chatid"))
    async def chatid(message: Message):
        await message.answer(f"id этого чата: `{message.chat.id}`", parse_mode="Markdown")

    @dp.message(Command("template"))
    async def template(message: Message):
        from aiogram.types import BufferedInputFile
        xlsx_bytes = stock.build_empty_template()
        await message.answer_document(
            BufferedInputFile(xlsx_bytes, filename="ostatok_sklada.xlsx"),
            caption="Заполни B1 (дата остатка) и строки с 4-й (Артикул, Остаток), пришли файл обратно сюда.",
        )

    @dp.message(Command("stock"))
    async def stock_cmd(message: Message):
        balances = await asyncio.to_thread(stock.compute_balances)
        if not balances:
            await message.answer("Остаток ещё не загружен — пришли файл через /template.")
            return
        lines = [f"• {article}: {qty}" for article, qty in sorted(balances.items())]
        await message.answer("📦 Текущий остаток:\n\n" + "\n".join(lines))

    @dp.message(Command("baseline_from_sheet"))
    async def baseline_from_sheet_cmd(message: Message):
        try:
            as_of_date, qty_by_article = await asyncio.to_thread(fulfillment_sheet.fetch_baseline_from_sheet)
        except ValueError as e:
            await message.answer(str(e))
            return
        except Exception:
            log.exception("Failed to fetch baseline from fulfillment sheet")
            await message.answer("Не смогла прочитать Google-таблицу — попробуй позже.")
            return
        await asyncio.to_thread(stock.replace_baseline, as_of_date, qty_by_article)
        await message.answer(f"Стартовый остаток взят из Google-таблицы на {as_of_date}: {len(qty_by_article)} артикулов ✓")

    @dp.message(Command("sverka"))
    async def sverka_cmd(message: Message):
        from aiogram.types import BufferedInputFile
        working = await message.answer("Собираю сверку…")
        try:
            report_bytes = await asyncio.to_thread(reconciliation.build_report)
        except Exception:
            log.exception("Failed to build reconciliation report on demand")
            await working.edit_text("Не получилось собрать сверку — проверь логи.")
            return
        await working.delete()
        await message.answer_document(
            BufferedInputFile(report_bytes, filename=f"sverka_{message.date.date().isoformat()}.xlsx"),
            caption="Автосверка отгрузок и остатков",
        )

    @dp.message(F.document)
    async def receive_baseline(message: Message):
        file_info = await message.bot.get_file(message.document.file_id)
        file_io = await message.bot.download_file(file_info.file_path)
        try:
            as_of_date, qty_by_article = await asyncio.to_thread(stock.parse_template, file_io.read())
        except ValueError as e:
            await message.answer(str(e))
            return
        except Exception:
            log.exception("Failed to parse stock baseline upload")
            await message.answer("Не смогла прочитать файл — пришли именно тот .xlsx, что я присылала через /template.")
            return

        await asyncio.to_thread(stock.replace_baseline, as_of_date, qty_by_article)
        await message.answer(f"Остаток обновлён на {as_of_date}: {len(qty_by_article)} артикулов ✓")

    return dp


async def _main():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    token = os.environ["KIM_BOT_TOKEN"]
    bot = build_bot(token)
    dp = build_dispatcher()
    await set_commands(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_main())
