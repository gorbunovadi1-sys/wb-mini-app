import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import cabinets
from . import margin
from . import ozon_dimensions
from . import ozon_margin
from . import ozon_price_monitor
from . import ozon_prices
from . import ozon_pricing
from . import ozon_promo_guard
from . import ozon_promotions
from . import ozon_promotions_detail
from . import wb_ads
from . import wb_prices
from .db import init_db
from .ozon_client import OzonClient
from .telegram_auth import parse_init_data_user, validate_init_data
from .wb_client import WBClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai_engine_app")

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend_engine")
DEV_MODE = os.environ.get("DEV_MODE", "1") == "1"  # skips Telegram signature check when set

app = FastAPI(title="ИИ Движок API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def _check_one_cabinet(cabinet: dict, bot, semaphore: "asyncio.Semaphore"):
    async with semaphore:
        try:
            # Both make blocking HTTP calls to Ozon — run off the event loop
            # so one slow/hanging cabinet can't stall the bot or API for
            # everyone else (matters once this scales past a handful of cabinets).
            new_negative = await asyncio.to_thread(ozon_price_monitor.check_cabinet, cabinet)
        except Exception:
            new_negative = []
            log.exception(f"Price check failed for cabinet {cabinet['id']}")
        try:
            removed_from_promo = await asyncio.to_thread(ozon_promo_guard.check_and_clean_cabinet, cabinet)
        except Exception:
            removed_from_promo = []
            log.exception(f"Promo guard failed for cabinet {cabinet['id']}")

    if new_negative:
        lines = [f"• {n['name']} ({n['offer_id']}): {n['profit']} ₽" for n in new_negative]
        text = (
            f"⚠️ В кабинете «{cabinet['display_name'] or cabinet['id']}» "
            f"{len(new_negative)} товар(ов) стали убыточными:\n\n" + "\n".join(lines)
        )
        try:
            await bot.send_message(cabinet["telegram_user_id"], text)
        except Exception:
            log.exception(f"Failed to notify user for cabinet {cabinet['id']}")

    if removed_from_promo:
        lines = [
            f"• {r['title']} ({r['offer_id']}) — акция «{r['action_title']}», цена по акции {r['action_price']} ₽, прибыль была бы {r['profit']} ₽"
            for r in removed_from_promo
        ]
        text = (
            f"🚫 В кабинете «{cabinet['display_name'] or cabinet['id']}» автоматически убрано "
            f"из невыгодных акций {len(removed_from_promo)} товар(ов):\n\n" + "\n".join(lines)
        )
        try:
            await bot.send_message(cabinet["telegram_user_id"], text)
        except Exception:
            log.exception(f"Failed to notify user for cabinet {cabinet['id']}")


async def _run_price_checks(bot):
    """Runs every 10 minutes for every active Ozon cabinet: (1) notifies the
    owner about products that newly became loss-making (ozon_price_monitor
    keeps a per-cabinet snapshot so already-known problems aren't re-reported
    every cycle), and (2) automatically removes products from a promotion
    when Ozon's own auto-add put them in at a losing price, notifying what
    was removed and why. Cabinets are checked concurrently (capped at 5 in
    flight) instead of one at a time so the total run time doesn't grow
    linearly with the number of cabinets."""
    cabs = cabinets.list_all_active_cabinets(marketplace="ozon")
    semaphore = asyncio.Semaphore(5)
    await asyncio.gather(*[_check_one_cabinet(c, bot, semaphore) for c in cabs])


@app.on_event("startup")
async def on_startup():
    init_db()

    ai_engine_token = os.environ.get("AI_ENGINE_BOT_TOKEN")
    if ai_engine_token:
        from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonWebApp, WebAppInfo
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from .ai_engine_bot import build_bot, build_dispatcher

        mini_app_url = os.environ.get("AI_ENGINE_MINI_APP_URL")
        bot = build_bot(ai_engine_token)
        if mini_app_url:
            # Pins a persistent Web App button in the message input bar (next
            # to the attachment icon) so it doesn't get buried by chat history.
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=mini_app_url))
            )

        # Slash-command menu (the "/" popup) — /admin only shows up for the
        # admin's own chat, everyone else only ever sees /start.
        await bot.set_my_commands(
            [BotCommand(command="start", description="Открыть меню")],
            scope=BotCommandScopeDefault(),
        )
        admin_id = os.environ.get("ADMIN_TELEGRAM_ID")
        if admin_id:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Открыть меню"),
                    BotCommand(command="admin", description="Пользователи и кабинеты"),
                    BotCommand(command="access", description="Выдать доступ: /access id дней"),
                    BotCommand(command="block", description="Заблокировать: /block id"),
                    BotCommand(command="unblock", description="Разблокировать: /unblock id"),
                    BotCommand(command="help", description="Памятка по всем командам"),
                ],
                scope=BotCommandScopeChat(chat_id=int(admin_id)),
            )

        dp = build_dispatcher(mini_app_url)
        asyncio.create_task(dp.start_polling(bot))
        log.info("AI Engine bot polling started")

        # AsyncIOScheduler (not BackgroundScheduler) so the job runs on the
        # same event loop as the bot — needed to call bot.send_message safely.
        scheduler = AsyncIOScheduler()
        scheduler.add_job(_run_price_checks, "interval", minutes=10, args=[bot])
        scheduler.start()
        log.info("Price monitor scheduled every 10 minutes")
    else:
        log.info("AI_ENGINE_BOT_TOKEN not set — bot polling not started")


def _resolve_user_id(x_telegram_init_data: Optional[str], telegram_id: Optional[int]) -> int:
    """Resolves the caller's internal user_id. In production, trusts Telegram's
    signed initData. In DEV_MODE, accepts a ?telegram_id= query param instead,
    since there's no real Telegram context when testing the API directly.
    Blocked or expired-subscription users are rejected here — this is the
    single choke point every data route goes through via Depends/direct call."""
    if DEV_MODE:
        if telegram_id is None:
            raise HTTPException(status_code=400, detail="DEV_MODE: pass ?telegram_id=<your Telegram id>")
        tg_id = telegram_id
    else:
        ai_bot_token = os.environ.get("AI_ENGINE_BOT_TOKEN")
        if not x_telegram_init_data or not validate_init_data(x_telegram_init_data, bot_token=ai_bot_token):
            raise HTTPException(status_code=401, detail="invalid Telegram init data")
        tg_user = parse_init_data_user(x_telegram_init_data)
        if not tg_user.get("id"):
            raise HTTPException(status_code=401, detail="no user in init data")
        tg_id = tg_user["id"]

    try:
        cabinets.check_access(tg_id)
    except cabinets.AccessDenied as e:
        detail = "Доступ заблокирован" if e.reason == "blocked" else "Срок подписки истёк"
        raise HTTPException(status_code=403, detail=detail)

    if DEV_MODE:
        return cabinets.get_or_create_user(tg_id)
    return cabinets.get_or_create_user(tg_id, tg_user.get("first_name"), tg_user.get("username"))


def _build_client(cabinet: dict):
    creds = cabinet["credentials"]
    if cabinet["marketplace"] == "wb":
        return WBClient(creds["api_key"])
    if cabinet["marketplace"] == "ozon":
        return OzonClient(creds["client_id"], creds["api_key"])
    raise ValueError(f"unknown marketplace {cabinet['marketplace']}")


def _owned_cabinet_or_404(cabinet_id: int, user_id: int) -> dict:
    cabinet = cabinets.get_cabinet(cabinet_id)
    if not cabinet or cabinet["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="cabinet not found")
    return cabinet


@app.get("/api/me/cabinets")
def get_my_cabinets(x_telegram_init_data: Optional[str] = Header(default=None), telegram_id: Optional[int] = None):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    return {"cabinets": cabinets.list_cabinets(user_id)}


@app.get("/api/cabinets/{cabinet_id}/margin")
def get_cabinet_margin(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
    days: int = 30,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    days = max(7, min(days, 180))
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    client = _build_client(cabinet)
    cost_prices = cabinets.get_cost_prices(cabinet_id)
    try:
        if cabinet["marketplace"] == "wb":
            return margin.build_margin_summary(
                client=client, cost_prices=cost_prices, days=days, date_from=date_from, date_to=date_to,
            )
        return ozon_margin.build_margin_summary(
            client=client, cost_prices=cost_prices, days=days, date_from=date_from, date_to=date_to,
        )
    except Exception as e:
        log.exception(f"Failed to build margin for cabinet {cabinet_id}")
        raise HTTPException(status_code=502, detail=f"upstream marketplace API error: {e}")


@app.get("/api/cabinets/{cabinet_id}/cost-prices")
def get_cabinet_cost_prices(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    _owned_cabinet_or_404(cabinet_id, user_id)
    return cabinets.get_cost_prices(cabinet_id)


@app.post("/api/cabinets/{cabinet_id}/cost-prices")
def set_cabinet_cost_prices(
    cabinet_id: int,
    body: dict,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    _owned_cabinet_or_404(cabinet_id, user_id)
    prices = body.get("prices", {})
    if not prices:
        raise HTTPException(status_code=400, detail="prices dict is empty")
    cabinets.set_cost_prices(cabinet_id, prices)
    return {"status": "saved"}


@app.get("/api/cabinets/{cabinet_id}/ozon/prices")
def get_cabinet_ozon_prices(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return {"items": ozon_prices.get_price_list(client=client)}


@app.post("/api/cabinets/{cabinet_id}/ozon/prices")
def set_cabinet_ozon_prices(
    cabinet_id: int,
    body: dict,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    updates = body.get("updates", [])
    if not updates:
        raise HTTPException(status_code=400, detail="updates list is empty")
    return {"status": "done", "results": ozon_prices.update_prices(updates, client=client)}


@app.get("/api/cabinets/{cabinet_id}/ozon/pricing")
def get_cabinet_ozon_pricing(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return {"items": ozon_pricing.get_pricing_list(client, cabinet_id)}


@app.get("/api/cabinets/{cabinet_id}/wb/pricing")
def get_cabinet_wb_pricing(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "wb":
        raise HTTPException(status_code=400, detail="not a WB cabinet")
    client = _build_client(cabinet)
    return {"items": wb_prices.get_price_list(client, cabinet_id)}


@app.get("/api/cabinets/{cabinet_id}/ozon/promotions")
def get_cabinet_ozon_promotions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    return ozon_promotions.get_cached(cabinet_id=str(cabinet_id))


@app.post("/api/cabinets/{cabinet_id}/ozon/promotions/refresh")
def refresh_cabinet_ozon_promotions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return ozon_promotions.refresh_promotions(client=client, cabinet_id=str(cabinet_id))


@app.get("/api/cabinets/{cabinet_id}/ozon/actions")
def get_cabinet_ozon_actions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return {"actions": ozon_promotions_detail.list_actions(client)}


@app.get("/api/cabinets/{cabinet_id}/ozon/actions/{action_id}")
def get_cabinet_ozon_action_detail(
    cabinet_id: int,
    action_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return ozon_promotions_detail.get_action_detail(client, cabinet_id, action_id)


@app.post("/api/cabinets/{cabinet_id}/ozon/actions/{action_id}/add")
def add_cabinet_ozon_action_products(
    cabinet_id: int,
    action_id: int,
    body: dict,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    items = body.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="items list is empty")
    client = _build_client(cabinet)
    return ozon_promotions_detail.add_products(client, action_id, items)


@app.post("/api/cabinets/{cabinet_id}/ozon/actions/{action_id}/remove")
def remove_cabinet_ozon_action_products(
    cabinet_id: int,
    action_id: int,
    body: dict,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    product_ids = body.get("product_ids", [])
    if not product_ids:
        raise HTTPException(status_code=400, detail="product_ids list is empty")
    client = _build_client(cabinet)
    return ozon_promotions_detail.remove_products(client, action_id, product_ids)


@app.get("/api/cabinets/{cabinet_id}/settings")
def get_cabinet_settings(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    _owned_cabinet_or_404(cabinet_id, user_id)
    return cabinets.get_cabinet_settings(cabinet_id)


@app.post("/api/cabinets/{cabinet_id}/settings")
def update_cabinet_settings(
    cabinet_id: int,
    body: dict,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    _owned_cabinet_or_404(cabinet_id, user_id)
    return cabinets.update_cabinet_settings(cabinet_id, body)


@app.get("/api/cabinets/{cabinet_id}/ads")
def get_cabinet_ads(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
    days: int = 30,
):
    days = max(7, min(days, 90))
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    client = _build_client(cabinet)
    if cabinet["marketplace"] == "wb":
        return wb_ads.get_campaigns_summary(client, days=days)
    raise HTTPException(
        status_code=400,
        detail="Реклама Ozon требует отдельные ключи Ozon Performance API — ещё не подключены",
    )


@app.get("/api/cabinets/{cabinet_id}/ozon/dimensions")
def get_cabinet_ozon_dimensions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    return ozon_dimensions.get_cached(cabinet_id=str(cabinet_id))


@app.post("/api/cabinets/{cabinet_id}/ozon/dimensions/refresh")
def refresh_cabinet_ozon_dimensions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return ozon_dimensions.refresh_dimensions(client=client, cabinet_id=str(cabinet_id))


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
