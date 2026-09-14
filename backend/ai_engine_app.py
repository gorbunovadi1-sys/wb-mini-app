import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests
from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import cabinets
from . import margin
from . import ozon_dimensions
from . import ozon_margin
from . import ozon_prices
from . import ozon_pricing
from . import ozon_promotions
from . import ozon_promotions_detail
from . import ozon_sales_cache
from . import ozon_stock
from . import wb_ads
from . import wb_prices
from . import wb_sales_cache
from . import wb_stock
from .db import init_db
from .ozon_client import OzonClient
from .scheduled_jobs import CABINET_REFRESH_TIMEOUT_SECONDS
from .telegram_auth import parse_init_data_user, validate_init_data
from .wb_client import WBClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai_engine_app")

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend_engine")
DEV_MODE = os.environ.get("DEV_MODE", "1") == "1"  # skips Telegram signature check when set

app = FastAPI(title="ИИ Движок API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def on_startup():
    init_db()

    ai_engine_token = os.environ.get("AI_ENGINE_BOT_TOKEN")
    if ai_engine_token:
        from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonWebApp, WebAppInfo
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
                    BotCommand(command="limit", description="Лимит кабинетов: /limit id N"),
                    BotCommand(command="help", description="Памятка по всем командам"),
                ],
                scope=BotCommandScopeChat(chat_id=int(admin_id)),
            )

        dp = build_dispatcher(mini_app_url)
        asyncio.create_task(dp.start_polling(bot))
        log.info("AI Engine bot polling started")
        # Promo/margin/dimension checks and the WB/Ozon sales-cache refresh
        # used to run from an AsyncIOScheduler right here — moved to the
        # separate `worker` service/process (backend/worker.py) so a stuck
        # or crash-looping sync job can no longer take this live service
        # down with it (see that file's docstring for the incident this
        # fixes). This process no longer runs any of those jobs itself.
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


def _build_client(cabinet: dict, ozon_max_retries: int = 1):
    """Default max_retries=1 (not OzonClient's own default of 8) — every
    caller here is a live HTTP request a Telegram Mini App tab is actually
    waiting on. Only the /margin route used to override this explicitly
    (with the exact same reasoning below); every OTHER live route
    (Цены, price pushes, stocks, promotions, ads, ...) was still building
    clients with the patient 8-retry default, worst case ~165s per call.
    Under Ozon's sustained rate limiting (real, observed for hours on
    2026-09-13) that blocked a request-handling thread long enough,
    repeated across several concurrent tabs/requests, to make the whole
    app unresponsive to Railway's health check and get it killed —
    twice, on the same night, before this was traced back here rather
    than to the background scheduler (which already had its own reduced
    retries and 90s timeout, and turned out not to be the culprit this
    time). A live request has the platform's own reverse-proxy timeout to
    respect regardless — one attempt (a fast, clear failure) is far
    better than ever risking a bare unhelpful 502, and definitely better
    than risking the whole instance. Background/scheduled jobs pass their
    own explicit, more patient value (they can afford to wait, and don't
    block anything a user is looking at)."""
    creds = cabinet["credentials"]
    if cabinet["marketplace"] == "wb":
        return WBClient(creds["api_key"])
    if cabinet["marketplace"] == "ozon":
        return OzonClient(creds["client_id"], creds["api_key"], max_retries=ozon_max_retries)
    raise ValueError(f"unknown marketplace {cabinet['marketplace']}")







@app.get("/api/_debug/list-cabinets")
def debug_list_cabinets(telegram_id: int):
    """TEMPORARY — list every cabinet with id + sync status + Ozon cache
    freshness, to find which one is stuck failing to load. Remove after
    use."""
    if telegram_id != int(os.environ.get("ADMIN_TELEGRAM_ID", "0")):
        raise HTTPException(status_code=403, detail="admin only")
    from .db import SessionLocal
    from .models import Cabinet, User, OzonSalesCache
    out = []
    with SessionLocal() as session:
        rows = session.query(Cabinet, User).join(User, Cabinet.user_id == User.id).filter(Cabinet.is_active.is_(True)).all()
        for c, u in rows:
            entry = {
                "id": c.id,
                "owner_telegram_id": u.telegram_user_id,
                "owner_username": u.username,
                "marketplace": c.marketplace,
                "display_name": c.display_name,
                "last_synced_at": str(c.last_synced_at) if c.last_synced_at else None,
                "last_error": c.last_error,
            }
            if c.marketplace == "ozon":
                cached_row = session.get(OzonSalesCache, c.id)
                if cached_row:
                    entry["cache_period_from"] = cached_row.period_from
                    entry["cache_period_to"] = cached_row.period_to
                    entry["cache_updated_at"] = str(cached_row.updated_at)
                    entry["cache_postings_count"] = len(cached_row.postings) if cached_row.postings else 0
                    entry["cache_accrual_entries_is_null"] = cached_row.accrual_entries is None
                    entry["cache_accrual_entries_count"] = len(cached_row.accrual_entries) if cached_row.accrual_entries else 0
                else:
                    entry["cache"] = None
            out.append(entry)
    return {"cabinets": out}


@app.post("/api/_debug/trigger-cache-refresh")
def debug_trigger_cache_refresh(cabinet_id: int, telegram_id: int, background_tasks: BackgroundTasks):
    """TEMPORARY — manually kick one cabinet's sales-cache refresh right
    now instead of waiting for the next 3h scheduler tick (useful for a
    cabinet stuck on a stale/incompatible cache after a fix ships, without
    needing to restart the whole app to re-trigger the startup refresh).
    Runs via BackgroundTasks (after the response is sent, off the request
    thread) using the same reduced max_retries=3 the scheduled job uses —
    never the interactive-path default of 8 — so a slow cabinet can't tie
    up a worker thread for its full ~165s-per-call patient schedule. Remove
    after use."""
    if telegram_id != int(os.environ.get("ADMIN_TELEGRAM_ID", "0")):
        raise HTTPException(status_code=403, detail="admin only")
    cabinet = cabinets.get_cabinet(cabinet_id)
    if not cabinet:
        raise HTTPException(status_code=404, detail="cabinet not found")

    async def _run():
        try:
            client = _build_client(cabinet, ozon_max_retries=3) if cabinet["marketplace"] == "ozon" else _build_client(cabinet)
            if cabinet["marketplace"] == "ozon":
                # Same 90s ceiling as the scheduled job (CABINET_REFRESH_TIMEOUT_SECONDS)
                # — see that constant's comment for the hang this guards against.
                await asyncio.wait_for(asyncio.to_thread(ozon_sales_cache.refresh, client, cabinet_id), timeout=CABINET_REFRESH_TIMEOUT_SECONDS)
            else:
                await asyncio.to_thread(wb_sales_cache.refresh, client, cabinet_id)
            log.info(f"Manually-triggered cache refresh done for cabinet {cabinet_id}")
        except asyncio.TimeoutError:
            log.warning(f"Manually-triggered cache refresh for cabinet {cabinet_id} exceeded {CABINET_REFRESH_TIMEOUT_SECONDS}s")
        except Exception:
            log.exception(f"Manually-triggered cache refresh failed for cabinet {cabinet_id}")

    background_tasks.add_task(_run)
    return {"status": "started", "cabinet_id": cabinet_id, "marketplace": cabinet["marketplace"]}


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
    # A live (cache-miss) fetch here happens inside an HTTP request a tab is
    # waiting on — the platform's own reverse-proxy timeout kills it well
    # before OzonClient's full patient 8-retry schedule (up to ~165s on one
    # call) finishes, coming back as a bare, unhelpful 502. Even a 2-attempt
    # (~15s) budget was observed still occasionally losing that race in
    # production, so this makes exactly one attempt — no retry sleep at all,
    # just the network round-trip — guaranteeing a fast, clear error instead
    # of ever racing a proxy timeout. Recovering from a real sustained block
    # is entirely the background cache refresh's job (ozon_sales_cache),
    # which uses the full patient schedule on its own separate time budget.
    client = _build_client(cabinet, ozon_max_retries=1)
    cost_prices = cabinets.get_cost_prices(cabinet_id)
    try:
        if cabinet["marketplace"] == "wb":
            cached = wb_sales_cache.get(cabinet_id)
            rows, rows_cover_from = (cached[0], cached[1]) if cached else (None, None)
            return margin.build_margin_summary(
                client=client, cost_prices=cost_prices, days=days, date_from=date_from, date_to=date_to,
                rows=rows, rows_cover_from=rows_cover_from,
            )
        tax_pct = cabinet.get("settings", {}).get("tax_pct", 0)
        cached = ozon_sales_cache.get(cabinet_id)
        cpostings, caccrual, cnonitem, ccover_from = (cached[0], cached[1], cached[2], cached[3]) if cached else (None, None, None, None)
        return ozon_margin.build_margin_summary(
            client=client, cost_prices=cost_prices, days=days, date_from=date_from, date_to=date_to, tax_pct=tax_pct,
            cached_postings=cpostings, cached_accrual_entries=caccrual,
            cached_non_item_by_date=cnonitem, cache_cover_from=ccover_from,
        )
    except requests.exceptions.HTTPError as e:
        log.exception(f"Failed to build margin for cabinet {cabinet_id}")
        if e.response is not None and e.response.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="Маркетплейс временно ограничивает количество запросов от этого кабинета. Подождите пару минут и откройте вкладку заново.",
            )
        raise HTTPException(status_code=502, detail=f"upstream marketplace API error: {e}")
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


@app.post("/api/cabinets/{cabinet_id}/ozon/stocks")
def set_cabinet_ozon_stocks(
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
    try:
        result = ozon_stock.update_fbs_stock(client, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "done", "result": result}


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


@app.get("/api/cabinets/{cabinet_id}/wb/stock")
def get_cabinet_wb_stock(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "wb":
        raise HTTPException(status_code=400, detail="not a WB cabinet")
    client = _build_client(cabinet)
    cost_prices = cabinets.get_cost_prices(cabinet_id)
    cached = wb_sales_cache.get(cabinet_id)
    rows, rows_cover_from = (cached[0], cached[1]) if cached else (None, None)

    # Both calls are slow and independent (WB's finance-throttled sales
    # report — skipped if a cache hit — and WB's async FBO-remains report) —
    # run them side by side instead of one after the other.
    with ThreadPoolExecutor(max_workers=2) as pool:
        margin_future = pool.submit(
            margin.build_margin_summary, client=client, cost_prices=cost_prices, days=30,
            rows=rows, rows_cover_from=rows_cover_from,
        )
        stock_future = pool.submit(wb_stock.get_fbo_stock, client)
        margin_data = margin_future.result()
        fbo_stock = stock_future.result()

    names = {p["nm_id"]: p["title"] for p in margin_data["products"]}
    vendor_codes = {p["nm_id"]: p["vendor_code"] for p in margin_data["products"]}
    qty_by_nm = {p["nm_id"]: p.get("qty", 0) for p in margin_data["products"]}

    items = []
    for nm_id, stock in fbo_stock.items():
        qty30 = qty_by_nm.get(nm_id, 0)
        daily_velocity = qty30 / 30
        days_left = stock / daily_velocity if daily_velocity > 0 else None
        recommended_restock = max(0, round(30 * daily_velocity - stock)) if daily_velocity > 0 else 0
        items.append({
            "nm_id": nm_id,
            "title": names.get(nm_id) or str(nm_id),
            "vendor_code": vendor_codes.get(nm_id, ""),
            "fbo_stock": stock,
            "qty30": qty30,
            "days_left": round(days_left, 1) if days_left is not None else None,
            "recommended_restock": recommended_restock,
        })
    return {"items": items}


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


@app.get("/api/cabinets/{cabinet_id}/ads/{advert_id}/clusters")
def get_cabinet_ad_clusters(
    cabinet_id: int,
    advert_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
    days: int = 30,
):
    days = max(7, min(days, 90))
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "wb":
        raise HTTPException(status_code=400, detail="Доступно только для WB")
    client = _build_client(cabinet)
    return {"clusters": wb_ads.get_campaign_clusters(client, advert_id, days=days)}


@app.post("/api/cabinets/{cabinet_id}/ads/{advert_id}/exclude-phrase")
def exclude_cabinet_ad_phrase(
    cabinet_id: int,
    advert_id: int,
    body: dict,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "wb":
        raise HTTPException(status_code=400, detail="Доступно только для WB")
    norm_query = (body or {}).get("norm_query")
    if not norm_query:
        raise HTTPException(status_code=400, detail="norm_query is required")
    client = _build_client(cabinet)
    return wb_ads.exclude_cluster_from_campaign(client, advert_id, norm_query)


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


class NoCacheHtmlStaticFiles(StaticFiles):
    """StaticFiles served no Cache-Control header at all, which left it to
    each browser/webview's own heuristics whether to reuse a stale copy of
    index.html without even checking back with the server — this Mini App
    ships as a single monolithic HTML file with no separate versioned JS/CSS
    bundle, so any UI fix could silently fail to reach a user still on an
    old cached copy (observed live 2026-09-13: a shipped feature was
    invisible in the actual Telegram Mini App despite being confirmed
    present in the server's response). `no-cache` (not `no-store`) still
    lets the browser keep a local copy and revalidate it cheaply via
    ETag/Last-Modified (a fast 304 when unchanged) — it just forbids ever
    using that copy WITHOUT checking first."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # Checking the response's own Content-Type rather than `path` —
        # for the root URL Starlette normalizes `path` to "." (not "" or
        # "index.html"), which silently never matched a path-based check
        # here and meant this class did nothing for the one route that
        # actually mattered (verified live: the header was still missing
        # after first shipping the path-based version of this check).
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheHtmlStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
