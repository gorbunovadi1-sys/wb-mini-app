import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()  # must run before importing modules below that read env vars at import time

import requests
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject, WebAppInfo

from . import cabinets
from . import cost_price_import
from . import ozon_sales_cache
from . import wb_sales_cache
from .ozon_client import OzonClient
from .wb_client import WBClient

log = logging.getLogger("ai_engine_bot")

MARKETPLACE_LABELS = {"wb": "Wildberries", "ozon": "Ozon"}


def _is_admin(user_id: int) -> bool:
    admin_id = os.environ.get("ADMIN_TELEGRAM_ID")
    return bool(admin_id) and user_id == int(admin_id)


def _kick_off_initial_cache_refresh(marketplace: str, cabinet_id: int, credentials: dict) -> None:
    """Fires the first cache refresh for a freshly-connected cabinet
    immediately in the background, instead of leaving it empty until the
    scheduler's next 3h tick (_refresh_ozon_caches/_refresh_wb_caches in
    ai_engine_app.py) — otherwise a user who opens Аналитика right after
    connecting can wait up to 3 hours (or hit the slow, uncached live-fetch
    fallback) before seeing any data. Fire-and-forget: runs on its own
    asyncio task so it never blocks the bot's "подключён ✓" reply, and any
    failure here just gets logged — the scheduled job will pick this
    cabinet up and retry regardless.

    Builds its own client rather than reusing the one from the interactive
    onboarding flow: for Ozon specifically, that one was built with the
    default max_retries=8, fine for the single foreground credentials-check
    call it made, but too patient (worst case ~165s per call, tying up a
    shared thread-pool worker) for a background job — max_retries=3 here
    matches what _refresh_ozon_caches already uses for the same reason
    (see ai_engine_app.py, and the crash this avoided on 2026-09-12)."""

    async def _run():
        try:
            if marketplace == "ozon":
                client = OzonClient(credentials["client_id"], credentials["api_key"], max_retries=3)
                await asyncio.to_thread(ozon_sales_cache.refresh, client, cabinet_id)
            else:
                client = WBClient(credentials["api_key"])
                await asyncio.to_thread(wb_sales_cache.refresh, client, cabinet_id)
            log.info(f"Initial {marketplace} cache refresh done for newly-connected cabinet {cabinet_id}")
        except Exception:
            log.exception(f"Initial {marketplace} cache refresh failed for newly-connected cabinet {cabinet_id} — scheduled job will retry")

    asyncio.create_task(_run())


class AccessControlMiddleware(BaseMiddleware):
    """Blocks every update from a blocked or expired-subscription user before
    it reaches any handler — the admin is always exempt. Central enforcement
    point so individual handlers don't each need their own check."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user and not _is_admin(user.id):
            try:
                cabinets.check_access(user.id)
            except cabinets.AccessDenied as e:
                text = (
                    "🚫 Доступ приостановлен. Обратитесь к администратору."
                    if e.reason == "blocked"
                    else "⏳ Срок подписки истёк. Обратитесь к администратору, чтобы продлить доступ."
                )
                if isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(text)
                return
        return await handler(event, data)


class Onboarding(StatesGroup):
    entering_wb_key = State()
    entering_ozon_client_id = State()
    entering_ozon_api_key = State()
    awaiting_cost_prices_file = State()


def build_bot(token: str) -> Bot:
    return Bot(token=token)


def _cabinets_kb(user_id: int, mini_app_url: str = None) -> InlineKeyboardMarkup:
    rows = []
    my = cabinets.list_cabinets(user_id)
    if mini_app_url and my:
        rows.append([InlineKeyboardButton(text="📊 Открыть кабинет", web_app=WebAppInfo(url=mini_app_url))])
    if my:
        rows.append([InlineKeyboardButton(text="💰 Внести себестоимость", callback_data="cost_prices_menu")])
    rows.append([InlineKeyboardButton(text="+ Подключить Wildberries", callback_data="connect_wb")])
    rows.append([InlineKeyboardButton(text="+ Подключить Ozon", callback_data="connect_ozon")])
    for c in cabinets.list_cabinets(user_id):
        label = c["display_name"] or MARKETPLACE_LABELS.get(c["marketplace"], c["marketplace"])
        rows.append([InlineKeyboardButton(text=f"✕ Отключить «{label}»", callback_data=f"disconnect_{c['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cabinets_text(user_id: int) -> str:
    my = cabinets.list_cabinets(user_id)
    if not my:
        return "Кабинеты пока не подключены. Выбери маркетплейс, чтобы начать:"
    lines = ["Подключённые кабинеты:"]
    for c in my:
        label = c["display_name"] or MARKETPLACE_LABELS.get(c["marketplace"], c["marketplace"])
        status = "⚠ ошибка подключения" if c["last_error"] else "✓ работает"
        lines.append(f"• {label} ({MARKETPLACE_LABELS.get(c['marketplace'], c['marketplace'])}) — {status}")
    lines.append("\nМожно подключить ещё один кабинет или отключить существующий:")
    return "\n".join(lines)


def build_dispatcher(mini_app_url: str = None) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AccessControlMiddleware())
    dp.callback_query.outer_middleware(AccessControlMiddleware())

    @dp.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        user_id = cabinets.get_or_create_user(
            message.from_user.id, message.from_user.first_name, message.from_user.username,
        )
        await message.answer(
            f"ИИ Движок — управляй продажами на Wildberries и Ozon в одном месте.\n\n{_cabinets_text(user_id)}",
            reply_markup=_cabinets_kb(user_id, mini_app_url),
        )

    @dp.message(Command("help"))
    async def admin_help(message: Message):
        if not _is_admin(message.from_user.id):
            return
        await message.answer(
            "Админ-команды (закрепи это сообщение — зажми и выбери «Закрепить»):\n\n"
            "/admin — список пользователей, кабинетов и статус доступа\n\n"
            "/access <id> <дней> — выдать/продлить доступ\n"
            "  пример: /access 460816761 30\n\n"
            "/block <id> — заблокировать пользователя\n"
            "  пример: /block 460816761\n\n"
            "/unblock <id> — снять блокировку\n"
            "  пример: /unblock 460816761\n\n"
            "/limit <id> <N> — ограничить число кабинетов (0 — снять лимит)\n"
            "  пример: /limit 460816761 1\n\n"
            "id пользователя смотри в выводе /admin."
        )

    @dp.message(Command("admin"))
    async def admin_stats(message: Message):
        if not _is_admin(message.from_user.id):
            return  # silently ignore — don't reveal the command exists to non-admins
        stats = cabinets.get_admin_stats()
        lines = [f"Пользователей: {stats['total_users']}\n"]
        for u in stats["users"]:
            name = u["username"] and f"@{u['username']}" or (u["first_name"] or "без имени")
            if u["is_blocked"]:
                access = "🚫 заблокирован"
            elif u["access_until"]:
                access = f"до {u['access_until'].strftime('%d.%m.%Y')}"
            else:
                access = "безлимит"
            limit_note = f", лимит кабинетов {u['max_cabinets']}" if u.get("max_cabinets") else ""
            header = f"👤 {name} (id {u['telegram_user_id']}) — {access}{limit_note}"
            if not u["cabinets"]:
                lines.append(f"{header}, кабинетов нет")
                continue
            lines.append(header)
            for c in u["cabinets"]:
                label = c["display_name"] or MARKETPLACE_LABELS.get(c["marketplace"], c["marketplace"])
                synced = c["last_synced_at"].strftime("%d.%m %H:%M") if c["last_synced_at"] else "—"
                lines.append(f"   • {label} ({MARKETPLACE_LABELS.get(c['marketplace'], c['marketplace'])}), синк: {synced}")
        await message.answer("\n".join(lines))

    @dp.message(Command("block"))
    async def block_cmd(message: Message):
        if not _is_admin(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("Использование: /block <telegram_id>")
            return
        ok = cabinets.set_blocked(int(parts[1]), True)
        await message.answer("🚫 Заблокирован." if ok else "Пользователь не найден.")

    @dp.message(Command("unblock"))
    async def unblock_cmd(message: Message):
        if not _is_admin(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("Использование: /unblock <telegram_id>")
            return
        ok = cabinets.set_blocked(int(parts[1]), False)
        await message.answer("✓ Разблокирован." if ok else "Пользователь не найден.")

    @dp.message(Command("access"))
    async def access_cmd(message: Message):
        if not _is_admin(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 3 or not parts[1].lstrip("-").isdigit() or not parts[2].lstrip("-").isdigit():
            await message.answer("Использование: /access <telegram_id> <дней>")
            return
        ok = cabinets.grant_access_days(int(parts[1]), int(parts[2]))
        await message.answer(f"✓ Доступ продлён на {parts[2]} дн." if ok else "Пользователь не найден.")

    @dp.message(Command("limit"))
    async def limit_cmd(message: Message):
        if not _is_admin(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 3 or not parts[1].lstrip("-").isdigit() or not parts[2].lstrip("-").isdigit():
            await message.answer("Использование: /limit <telegram_id> <макс. кабинетов>\n0 — снять лимит (безлимит).")
            return
        n = int(parts[2])
        ok = cabinets.set_max_cabinets(int(parts[1]), n)
        if not ok:
            await message.answer("Пользователь не найден.")
        elif n <= 0:
            await message.answer("✓ Лимит кабинетов снят (безлимит).")
        else:
            await message.answer(f"✓ Лимит кабинетов установлен: {n}.")

    @dp.callback_query(F.data == "connect_wb")
    async def connect_wb(callback: CallbackQuery, state: FSMContext):
        try:
            cabinets.check_cabinet_limit(callback.from_user.id)
        except cabinets.AccessDenied:
            await callback.message.answer("Достигнут лимит подключённых кабинетов для твоего доступа. Обратись к администратору, чтобы расширить.")
            await callback.answer()
            return
        await state.set_state(Onboarding.entering_wb_key)
        await callback.message.answer(
            "Пришли API-ключ Wildberries (личный кабинет WB → Настройки → Доступ к API → "
            "создай ключ с категориями «Статистика», «Реклама», «Финансовые отчеты»).\n\n"
            "Ключ придёт длинной строкой — просто скопируй и вставь целиком."
        )
        await callback.answer()

    @dp.callback_query(F.data == "connect_ozon")
    async def connect_ozon(callback: CallbackQuery, state: FSMContext):
        try:
            cabinets.check_cabinet_limit(callback.from_user.id)
        except cabinets.AccessDenied:
            await callback.message.answer("Достигнут лимит подключённых кабинетов для твоего доступа. Обратись к администратору, чтобы расширить.")
            await callback.answer()
            return
        await state.set_state(Onboarding.entering_ozon_client_id)
        await callback.message.answer(
            "Пришли Client-Id кабинета Ozon (Настройки → Seller API в личном кабинете Ozon) — "
            "это короткое число, например 1234567."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("disconnect_"))
    async def disconnect(callback: CallbackQuery):
        cabinet_id = int(callback.data.split("_", 1)[1])
        user_id = cabinets.get_or_create_user(callback.from_user.id)
        ok = cabinets.deactivate_cabinet(cabinet_id, user_id)
        await callback.answer("Отключено" if ok else "Не найдено")
        await callback.message.edit_text(_cabinets_text(user_id), reply_markup=_cabinets_kb(user_id, mini_app_url))

    @dp.message(Onboarding.entering_wb_key)
    async def receive_wb_key(message: Message, state: FSMContext):
        api_key = message.text.strip()
        checking = await message.answer("Проверяю ключ…")
        client = WBClient(api_key)
        display_name = "Wildberries"
        try:
            info = await asyncio.to_thread(client.get_seller_info)
            display_name = info.get("tradeMark") or info.get("name") or display_name
        except requests.HTTPError:
            await checking.edit_text("Ключ не подошёл — WB его не принял. Проверь и пришли ещё раз.")
            return
        except requests.RequestException as e:
            await checking.edit_text(f"Не получилось достучаться до WB API, попробуй ещё раз чуть позже.\n{e}")
            return

        user_id = cabinets.get_or_create_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
        cabinet_id = cabinets.add_cabinet(user_id, "wb", {"api_key": api_key}, display_name=display_name)
        _kick_off_initial_cache_refresh("wb", cabinet_id, {"api_key": api_key})
        await state.clear()
        await checking.edit_text(f"Кабинет «{display_name}» (Wildberries) подключён ✓")
        await message.answer(_cabinets_text(user_id), reply_markup=_cabinets_kb(user_id, mini_app_url))

    @dp.message(Onboarding.entering_ozon_client_id)
    async def receive_ozon_client_id(message: Message, state: FSMContext):
        client_id = message.text.strip()
        if not client_id.isdigit():
            await message.answer("Client-Id — это просто число, без букв. Пришли ещё раз.")
            return
        await state.update_data(ozon_client_id=client_id)
        await state.set_state(Onboarding.entering_ozon_api_key)
        await message.answer("Теперь пришли Api-Key Ozon (там же, в Seller API) — строка вида a1b2c3d4-5678-90ab-cdef-1234567890ab.")

    @dp.message(Onboarding.entering_ozon_api_key)
    async def receive_ozon_api_key(message: Message, state: FSMContext):
        api_key = message.text.strip()
        data = await state.get_data()
        client_id = data["ozon_client_id"]
        checking = await message.answer("Проверяю ключ…")
        client = OzonClient(client_id, api_key)
        try:
            await asyncio.to_thread(client.check_credentials)
        except requests.HTTPError:
            await checking.edit_text("Ключ не подошёл — Ozon его не принял. Проверь Client-Id и Api-Key, пришли ещё раз (начни с /start).")
            await state.clear()
            return
        except requests.RequestException as e:
            await checking.edit_text(f"Не получилось достучаться до Ozon API, попробуй ещё раз чуть позже.\n{e}")
            return

        display_name = "Ozon"
        try:
            info = await asyncio.to_thread(client.get_seller_info)
            company = info.get("company") or {}
            display_name = company.get("name") or " ".join(filter(None, [company.get("ownership_form"), company.get("legal_name")])) or display_name
        except requests.RequestException:
            log.warning("Could not fetch Ozon seller-info for display name, using default")

        user_id = cabinets.get_or_create_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
        cabinet_id = cabinets.add_cabinet(user_id, "ozon", {"client_id": client_id, "api_key": api_key}, display_name=display_name)
        _kick_off_initial_cache_refresh("ozon", cabinet_id, {"client_id": client_id, "api_key": api_key})
        await state.clear()
        await checking.edit_text(f"Кабинет «{display_name}» (Ozon) подключён ✓")
        await message.answer(_cabinets_text(user_id), reply_markup=_cabinets_kb(user_id, mini_app_url))

    @dp.callback_query(F.data == "cost_prices_menu")
    async def cost_prices_menu(callback: CallbackQuery):
        user_id = cabinets.get_or_create_user(callback.from_user.id)
        my = cabinets.list_cabinets(user_id)
        rows = [
            [InlineKeyboardButton(
                text=(
                    ("✅ " if cabinets.get_cost_prices(c["id"]) else "")
                    + f"{c['display_name'] or MARKETPLACE_LABELS.get(c['marketplace'], c['marketplace'])} ({MARKETPLACE_LABELS.get(c['marketplace'], c['marketplace'])})"
                ),
                callback_data=f"costtpl_{c['id']}",
            )]
            for c in my
        ]
        await callback.message.answer(
            "Выбери кабинет — пришлю Excel-шаблон со всеми товарами:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("costtpl_"))
    async def send_cost_template(callback: CallbackQuery, state: FSMContext):
        cabinet_id = int(callback.data.split("_", 1)[1])
        user_id = cabinets.get_or_create_user(callback.from_user.id)
        cabinet = cabinets.get_cabinet(cabinet_id)
        await callback.answer()
        if not cabinet or cabinet["user_id"] != user_id:
            await callback.message.answer("Кабинет не найден.")
            return

        generating = await callback.message.answer("Собираю шаблон, это может занять минуту…")
        try:
            xlsx_bytes = await asyncio.to_thread(cost_price_import.build_template, cabinet)
        except Exception as e:
            log.exception(f"Failed to build cost-price template for cabinet {cabinet_id}")
            await generating.edit_text(f"Не получилось собрать шаблон: {e}")
            return

        await state.set_state(Onboarding.awaiting_cost_prices_file)
        await state.update_data(cost_prices_cabinet_id=cabinet_id)
        await generating.edit_text(
            "Готово — заполни колонку «Себестоимость за шт (руб)» и пришли файл обратно в этот чат (как документ)."
        )
        label = cabinet["display_name"] or MARKETPLACE_LABELS.get(cabinet["marketplace"], cabinet["marketplace"])
        file = BufferedInputFile(xlsx_bytes, filename=f"sebestoimost_{label}.xlsx")
        await callback.message.answer_document(file)

    @dp.message(Onboarding.awaiting_cost_prices_file, F.document)
    async def receive_cost_prices_file(message: Message, state: FSMContext):
        data = await state.get_data()
        cabinet_id = data.get("cost_prices_cabinet_id")
        if not cabinet_id:
            await message.answer("Не понимаю, для какого кабинета этот файл — начни заново через «Внести себестоимость».")
            await state.clear()
            return

        file_info = await message.bot.get_file(message.document.file_id)
        file_io = await message.bot.download_file(file_info.file_path)
        try:
            prices = cost_price_import.parse_template(file_io.read())
        except Exception as e:
            await message.answer(f"Не смогла прочитать файл — пришли именно тот .xlsx, что я присылала.\n{e}")
            return

        if not prices:
            await message.answer("Не нашла заполненных строк с себестоимостью — проверь файл и пришли снова.")
            return

        cabinets.set_cost_prices(cabinet_id, prices)
        await state.clear()
        await message.answer(f"Обновила себестоимость для {len(prices)} товаров ✓")

    return dp


async def _main():
    """Standalone entrypoint: `python -m backend.ai_engine_bot` for local testing."""
    import os
    logging.basicConfig(level=logging.INFO)
    token = os.environ["AI_ENGINE_BOT_TOKEN"]
    bot = build_bot(token)
    dp = build_dispatcher(os.environ.get("AI_ENGINE_MINI_APP_URL"))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_main())
