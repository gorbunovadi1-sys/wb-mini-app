import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()  # must run before importing modules below that read env vars at import time

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from . import cabinets
from .ozon_client import OzonClient
from .wb_client import WBClient

log = logging.getLogger("ai_engine_bot")

MARKETPLACE_LABELS = {"wb": "Wildberries", "ozon": "Ozon"}


class Onboarding(StatesGroup):
    entering_wb_key = State()
    entering_ozon_client_id = State()
    entering_ozon_api_key = State()


def build_bot(token: str) -> Bot:
    return Bot(token=token)


def _cabinets_kb(user_id: int, mini_app_url: str = None) -> InlineKeyboardMarkup:
    rows = []
    if mini_app_url and cabinets.list_cabinets(user_id):
        rows.append([InlineKeyboardButton(text="📊 Открыть кабинет", web_app=WebAppInfo(url=mini_app_url))])
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

    @dp.callback_query(F.data == "connect_wb")
    async def connect_wb(callback: CallbackQuery, state: FSMContext):
        await state.set_state(Onboarding.entering_wb_key)
        await callback.message.answer(
            "Пришли API-ключ Wildberries (личный кабинет WB → Настройки → Доступ к API → "
            "создай ключ с категориями «Статистика», «Реклама», «Финансовые отчеты»).\n\n"
            "Ключ придёт длинной строкой — просто скопируй и вставь целиком."
        )
        await callback.answer()

    @dp.callback_query(F.data == "connect_ozon")
    async def connect_ozon(callback: CallbackQuery, state: FSMContext):
        await state.set_state(Onboarding.entering_ozon_client_id)
        await callback.message.answer(
            "Пришли Client-Id кабинета Ozon (Настройки → Seller API в личном кабинете Ozon) — "
            "это короткое число, например 1702727."
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
        try:
            await asyncio.to_thread(client.check_credentials)
        except requests.HTTPError:
            await checking.edit_text("Ключ не подошёл — WB его не принял. Проверь и пришли ещё раз.")
            return
        except requests.RequestException as e:
            await checking.edit_text(f"Не получилось достучаться до WB API, попробуй ещё раз чуть позже.\n{e}")
            return

        user_id = cabinets.get_or_create_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
        cabinets.add_cabinet(user_id, "wb", {"api_key": api_key}, display_name="Wildberries")
        await state.clear()
        await checking.edit_text("Кабинет Wildberries подключён ✓")
        await message.answer(_cabinets_text(user_id), reply_markup=_cabinets_kb(user_id, mini_app_url))

    @dp.message(Onboarding.entering_ozon_client_id)
    async def receive_ozon_client_id(message: Message, state: FSMContext):
        client_id = message.text.strip()
        if not client_id.isdigit():
            await message.answer("Client-Id — это просто число, без букв. Пришли ещё раз.")
            return
        await state.update_data(ozon_client_id=client_id)
        await state.set_state(Onboarding.entering_ozon_api_key)
        await message.answer("Теперь пришли Api-Key Ozon (там же, в Seller API) — строка вида 747342b7-1b13-....")

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

        user_id = cabinets.get_or_create_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
        cabinets.add_cabinet(user_id, "ozon", {"client_id": client_id, "api_key": api_key}, display_name="Ozon")
        await state.clear()
        await checking.edit_text("Кабинет Ozon подключён ✓")
        await message.answer(_cabinets_text(user_id), reply_markup=_cabinets_kb(user_id, mini_app_url))

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
