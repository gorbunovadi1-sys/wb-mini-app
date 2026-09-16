import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, Message, TelegramObject

from . import config, fulfillment_sheet, orders, reconciliation, reviews, stock
from .wb_client import WBClient

MARKETPLACE_LABELS = {"wb": "Wildberries", "ozon": "Ozon"}

log = logging.getLogger("kim_bot.bot")


class ReviewEdit(StatesGroup):
    awaiting_text = State()


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
    BotCommand(command="orders", description="Текущие заказы FBS без сборки"),
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
    "⏰ Следит за сборкой FBS — если заказ не ушёл на сборку 8 часов, шлю отбивку в чат фулфилмента "
    "в 09:00 и 15:00.\n"
    "📝 Черновики ответов на отзывы WB — присылаю сюда с кнопками, ничего не публикую без подтверждения.\n\n"
    "/template — шаблон для загрузки остатка\n"
    "/stock — текущий остаток по артикулам\n"
    "/orders — текущие заказы FBS без сборки, по требованию\n"
    "/sverka — автосверка отгрузок и остатков (факт из API vs журнал ФФ) прямо сейчас\n"
    "/chatid — id этого чата (для настройки уведомлений)"
)


def build_bot(token: str) -> Bot:
    return Bot(token=token)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
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

    @dp.message(Command("orders"))
    async def orders_cmd(message: Message):
        pending = await asyncio.to_thread(orders.pending_orders)
        if not pending:
            await message.answer("Нет заказов, ожидающих сборки — всё собрано ✓")
            return
        lines = [
            f"{'🔴' if o['overdue'] else '🟢'} {MARKETPLACE_LABELS.get(o['marketplace'], o['marketplace'])} "
            f"{o['order_id']} (арт. {o['article'] or '—'}) — {o['age']}"
            for o in pending
        ]
        overdue_n = sum(1 for o in pending if o["overdue"])
        await message.answer(
            f"📦 Заказы без сборки ({len(pending)}, из них просрочено {overdue_n}):\n\n" + "\n".join(lines)
        )

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

    @dp.callback_query(F.data.startswith("review_approve_"))
    async def review_approve(callback: CallbackQuery):
        db_id = int(callback.data.rsplit("_", 1)[-1])
        if not config.WB_API_KEY:
            await callback.answer("WB-ключ не настроен", show_alert=True)
            return
        client = WBClient(config.WB_API_KEY)
        try:
            ok = await asyncio.to_thread(reviews.approve_and_post, db_id, client)
        except Exception:
            log.exception(f"Failed to post review answer for draft {db_id}")
            await callback.answer("Ошибка при отправке в WB — попробуй ещё раз", show_alert=True)
            return
        if ok:
            await callback.message.edit_text(callback.message.text + "\n\n✅ Отправлено на WB", reply_markup=None)
        else:
            await callback.answer("Уже обработано или пустой черновик", show_alert=True)
        await callback.answer()

    @dp.callback_query(F.data.startswith("review_skip_"))
    async def review_skip(callback: CallbackQuery):
        db_id = int(callback.data.rsplit("_", 1)[-1])
        await asyncio.to_thread(reviews.skip, db_id)
        await callback.message.edit_text(callback.message.text + "\n\n❌ Пропущено", reply_markup=None)
        await callback.answer()

    @dp.callback_query(F.data.startswith("review_edit_"))
    async def review_edit(callback: CallbackQuery, state: FSMContext):
        db_id = int(callback.data.rsplit("_", 1)[-1])
        await state.set_state(ReviewEdit.awaiting_text)
        await state.update_data(review_db_id=db_id)
        await callback.message.answer("Пришли новый текст ответа для этого отзыва — отправлю его вместо черновика.")
        await callback.answer()

    @dp.message(ReviewEdit.awaiting_text)
    async def receive_review_text(message: Message, state: FSMContext):
        data = await state.get_data()
        db_id = data.get("review_db_id")
        await state.clear()
        if not config.WB_API_KEY:
            await message.answer("WB-ключ не настроен")
            return
        client = WBClient(config.WB_API_KEY)
        try:
            ok = await asyncio.to_thread(reviews.approve_and_post, db_id, client, message.text)
        except Exception:
            log.exception(f"Failed to post edited review answer for draft {db_id}")
            await message.answer("Ошибка при отправке в WB — попробуй ещё раз.")
            return
        await message.answer("✅ Отправлено на WB с твоим текстом" if ok else "Не удалось отправить — черновик уже обработан.")

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
