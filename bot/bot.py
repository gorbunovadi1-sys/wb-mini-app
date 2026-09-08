import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

logging.basicConfig(level=logging.INFO)


def build_bot(token: str) -> Bot:
    return Bot(token=token)


def build_dispatcher(mini_app_url: str) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть кабинет", web_app=WebAppInfo(url=mini_app_url))
        ]])
        await message.answer(
            "Кабинет «Техника для жизни» — продажи, реклама и прибыль по WB.",
            reply_markup=kb,
        )

    return dp


async def _main():
    """Standalone entrypoint: `python -m bot.bot` for local testing outside the API process."""
    from dotenv import load_dotenv
    load_dotenv()
    token = os.environ["BOT_TOKEN"]
    mini_app_url = os.environ["MINI_APP_URL"]
    bot = build_bot(token)
    dp = build_dispatcher(mini_app_url)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_main())
