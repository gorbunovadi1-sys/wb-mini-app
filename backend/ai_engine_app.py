import asyncio
import collections
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
from . import ozon_price_monitor
from . import ozon_prices
from . import ozon_pricing
from . import ozon_promo_guard
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
from .telegram_auth import parse_init_data_user, validate_init_data
from .wb_client import WBClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai_engine_app")

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend_engine")
DEV_MODE = os.environ.get("DEV_MODE", "1") == "1"  # skips Telegram signature check when set

app = FastAPI(title="ИИ Движок API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def _notify(bot, cabinet: dict, text: str):
    try:
        await bot.send_message(cabinet["telegram_user_id"], text)
    except Exception:
        log.exception(f"Failed to notify user for cabinet {cabinet['id']}")


async def _check_promo_one(cabinet: dict, bot, semaphore: "asyncio.Semaphore"):
    async with semaphore:
        try:
            # Blocking HTTP calls to Ozon — run off the event loop so one
            # slow/hanging cabinet can't stall the bot or API for everyone else.
            promo_result = await asyncio.to_thread(ozon_promo_guard.check_and_clean_cabinet, cabinet)
        except Exception:
            promo_result = {"removed": [], "joined": []}
            log.exception(f"Promo guard failed for cabinet {cabinet['id']}")

    name = cabinet["display_name"] or cabinet["id"]

    if promo_result["removed"]:
        lines = [
            f"• {r['title']} ({r['offer_id']}) — акция «{r['action_title']}», цена по акции {r['action_price']} ₽, маржа была бы {r['margin_percent']}%"
            for r in promo_result["removed"]
        ]
        text = (
            f"🚫 В кабинете «{name}» автоматически убрано "
            f"из невыгодных акций {len(promo_result['removed'])} товар(ов):\n\n" + "\n".join(lines)
        )
        await _notify(bot, cabinet, text)

    if promo_result["joined"]:
        lines = [f"• {e['title']} ({e['offer_id']}) — акция «{e['action_title']}»" for e in promo_result["joined"]]
        text = (
            f"🏷 В кабинете «{name}» Ozon добавил в акции {len(promo_result['joined'])} товар(ов) "
            f"(автовывод выключен, проверь сама):\n\n" + "\n".join(lines)
        )
        await _notify(bot, cabinet, text)


async def _run_promo_checks(bot):
    """Runs every 10 minutes — kept frequent on purpose (unlike the margin and
    dimension checks below) because a timely heads-up matters here: either
    Ozon just auto-added products to a new promotion (notify-only mode) or
    this just auto-removed something losing money, and both are worth
    knowing about soon, not up to an hour later. Cabinets are checked
    concurrently (capped at 5 in flight)."""
    cabs = cabinets.list_all_active_cabinets(marketplace="ozon")
    semaphore = asyncio.Semaphore(5)
    await asyncio.gather(*[_check_promo_one(c, bot, semaphore) for c in cabs])


async def _check_margin_one(cabinet: dict, bot, semaphore: "asyncio.Semaphore"):
    async with semaphore:
        try:
            new_below_margin = await asyncio.to_thread(ozon_price_monitor.check_cabinet, cabinet)
        except Exception:
            new_below_margin = []
            log.exception(f"Price check failed for cabinet {cabinet['id']}")

    if not new_below_margin:
        return
    name = cabinet["display_name"] or cabinet["id"]
    min_margin_pct = cabinet.get("settings", {}).get("min_margin_pct", 0)
    lines = [f"• {n['name']} ({n['offer_id']}): {n['profit']} ₽, маржа {n['margin_percent']}%" for n in new_below_margin]
    threshold_note = "стали убыточными" if min_margin_pct <= 0 else f"опустились ниже заданной маржи ({min_margin_pct}%)"
    text = f"⚠️ В кабинете «{name}» {len(new_below_margin)} товар(ов) {threshold_note}:\n\n" + "\n".join(lines)
    await _notify(bot, cabinet, text)


async def _run_margin_checks(bot):
    """Runs once an hour — prices/margins don't usually swing fast enough to
    need 10-minute polling, and this was one of the main contributors to
    hitting Ozon's rate limit (it re-fetches the whole catalog's prices,
    just like the promo check does independently)."""
    cabs = cabinets.list_all_active_cabinets(marketplace="ozon")
    semaphore = asyncio.Semaphore(5)
    await asyncio.gather(*[_check_margin_one(c, bot, semaphore) for c in cabs])


async def _check_dimensions_one(cabinet: dict, bot, semaphore: "asyncio.Semaphore"):
    if not cabinet.get("settings", {}).get("notify_dimension_changes", True):
        return
    async with semaphore:
        dimension_events = []
        try:
            creds = cabinet["credentials"]
            dim_client = OzonClient(creds["client_id"], creds["api_key"])
            dim_result = await asyncio.to_thread(ozon_dimensions.refresh_dimensions, client=dim_client, cabinet_id=str(cabinet["id"]))
            dimension_events = [e for e in dim_result.get("recent_events", []) if e["at"] == dim_result["generated_at"]]
        except Exception:
            log.exception(f"Dimension check failed for cabinet {cabinet['id']}")

        if dimension_events:
            # A dimension change shifts Ozon's own logistics estimate for the
            # product — pull current price/margin so the notification shows
            # the actual impact, not just "the size changed".
            try:
                pricing = await asyncio.to_thread(ozon_pricing.get_pricing_list, dim_client, cabinet["id"])
                pricing_by_offer = {p["offer_id"]: p for p in pricing}
                for e in dimension_events:
                    p = pricing_by_offer.get(e["offer_id"])
                    if p:
                        price = p["price"] or p["min_price"] or 0
                        expense = price * (p["commission_pct"] / 100) + p["logistics_estimate"]
                        profit = price - (p["cogs_unit"] or 0) - expense
                        e["current_profit"] = round(profit, 2)
                        e["current_margin_percent"] = round(profit / price * 100, 2) if price else None
            except Exception:
                log.exception(f"Failed to enrich dimension events with profit for cabinet {cabinet['id']}")

    if not dimension_events:
        return
    name = cabinet["display_name"] or cabinet["id"]

    def _dim_line(e):
        base = f"• {e['title']} ({e['offer_id']}): {e['field_label']} {e['old_value']} → {e['new_value']} {e['unit']}"
        if "current_profit" in e:
            margin = f"{e['current_margin_percent']}%" if e["current_margin_percent"] is not None else "—"
            base += f"\n  сейчас прибыль {e['current_profit']} ₽ (маржа {margin}) с учётом новой логистики"
        return base

    lines = [_dim_line(e) for e in dimension_events]
    text = (
        f"📐 В кабинете «{name}» изменились габариты/вес у {len(dimension_events)} товар(ов) — "
        f"это влияет на логистику:\n\n" + "\n".join(lines)
    )
    await _notify(bot, cabinet, text)


async def _run_dimension_checks(bot):
    """Runs once a day — Ozon's catalog data doesn't shift often enough to
    justify checking it every 10 minutes, and this was the third redundant
    full-catalog-ish fetch happening in the same cycle as the other two."""
    cabs = cabinets.list_all_active_cabinets(marketplace="ozon")
    semaphore = asyncio.Semaphore(5)
    await asyncio.gather(*[_check_dimensions_one(c, bot, semaphore) for c in cabs])


async def _refresh_wb_caches(skip_if_fresh: bool = False):
    """Runs periodically: re-fetches WB's slow finance-api sales-report rows
    for every active WB cabinet into wb_sales_cache, so the margin/stock
    endpoints can serve from cache (fast) instead of hitting WB live — a
    live 30-day fetch can take 15-20+ minutes due to WB's 1 req/min
    finance-api throttle and a real cabinet needing 15-20+ report chunks.
    `skip_if_fresh` (used only for the startup kick-off) skips a cabinet
    whose cache was refreshed minutes ago — several redeploys in a row would
    otherwise each redo the same expensive fetch for every cabinet."""
    for cabinet in cabinets.list_all_active_cabinets(marketplace="wb"):
        if skip_if_fresh and wb_sales_cache.refreshed_recently(cabinet["id"]):
            log.info(f"WB sales cache for cabinet {cabinet['id']} is recent — skipping startup refresh")
            continue
        try:
            client = WBClient(cabinet["credentials"]["api_key"])
            await asyncio.to_thread(wb_sales_cache.refresh, client, cabinet["id"])
        except Exception:
            log.exception(f"WB sales cache refresh failed for cabinet {cabinet['id']}")


async def _refresh_ozon_caches(skip_if_fresh: bool = False):
    """Runs periodically: re-fetches Ozon FBS+FBO postings and per-day
    accrual breakdown for every active Ozon cabinet into ozon_sales_cache, so
    the /margin endpoint (shared by Дашборд/Детализация/Аналитика) can serve
    from cache instead of redoing a full live fetch on every tab open — that
    repetition alone (4 posting-list calls + one accrual call per day) was
    enough to trigger sustained 429s from Ozon during normal browsing.
    `skip_if_fresh` (used only for the startup kick-off) skips a cabinet
    whose cache was refreshed minutes ago — several redeploys in a row would
    otherwise each fire a fresh full-account burst across every cabinet,
    exactly the kind of repeated load that trips Ozon's rate limiting."""
    for cabinet in cabinets.list_all_active_cabinets(marketplace="ozon"):
        if skip_if_fresh and ozon_sales_cache.refreshed_recently(cabinet["id"]):
            log.info(f"Ozon sales cache for cabinet {cabinet['id']} is recent — skipping startup refresh")
            continue
        try:
            # max_retries=3 (not the client default of 8): this job runs
            # again in 3h regardless, so there's no need for the default's
            # worst-case ~165s-per-call escalating backoff — under sustained
            # 429s (which happens for real, per cabinet, for extended
            # stretches) that default was tying up a thread-pool worker long
            # enough, across several cabinets in a row, to crash the single
            # Railway instance (observed live twice on 2026-09-12).
            client = OzonClient(cabinet["credentials"]["client_id"], cabinet["credentials"]["api_key"], max_retries=3)
            await asyncio.to_thread(ozon_sales_cache.refresh, client, cabinet["id"])
        except Exception:
            log.exception(f"Ozon sales cache refresh failed for cabinet {cabinet['id']}")
        await asyncio.sleep(3)  # small gap between cabinets — avoids a startup burst across many cabinets at once


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
                    BotCommand(command="limit", description="Лимит кабинетов: /limit id N"),
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
        scheduler.add_job(_run_promo_checks, "interval", minutes=10, args=[bot])
        scheduler.add_job(_run_margin_checks, "interval", hours=1, args=[bot])
        scheduler.add_job(_run_dimension_checks, "interval", hours=24, args=[bot])
        scheduler.add_job(_refresh_wb_caches, "interval", hours=3)
        scheduler.add_job(_refresh_ozon_caches, "interval", hours=3)
        scheduler.start()
        log.info("Scheduled: акции every 10 min, маржа every hour, габариты once a day, WB+Ozon sales cache every 3 hours")
        asyncio.create_task(_refresh_wb_caches(skip_if_fresh=True))
        asyncio.create_task(_refresh_ozon_caches(skip_if_fresh=True))
        log.info("Kicked off initial WB+Ozon sales cache refreshes (not waiting for the first 3h tick)")
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


def _build_client(cabinet: dict, ozon_max_retries: int = None):
    creds = cabinet["credentials"]
    if cabinet["marketplace"] == "wb":
        return WBClient(creds["api_key"])
    if cabinet["marketplace"] == "ozon":
        if ozon_max_retries is not None:
            return OzonClient(creds["client_id"], creds["api_key"], max_retries=ozon_max_retries)
        return OzonClient(creds["client_id"], creds["api_key"])
    raise ValueError(f"unknown marketplace {cabinet['marketplace']}")



_fix_verify_state = {}


def _compute_fixed_totals_sync(cabinet_id: int, date_from: str, date_to: str):
    """Uses cached postings (no live posting fetch — Ozon is already
    rate-limiting hard right now) for revenue/buyouts, but does a fresh LIVE
    accrual fetch through the just-shipped buyout-filtering fix, so the
    before/after can be compared without needing a full cache rebuild."""
    import datetime as _dt
    cabinet = cabinets.get_cabinet(cabinet_id)
    cost_prices = cabinets.get_cost_prices(cabinet_id)
    tax_pct = cabinet.get("settings", {}).get("tax_pct", 0)
    cached = ozon_sales_cache.get(cabinet_id)
    if not cached:
        raise RuntimeError("no cache for this cabinet")
    cpostings = cached[0]
    d_from = _dt.date.fromisoformat(date_from)
    d_to = _dt.date.fromisoformat(date_to)
    postings = ozon_margin._slice_postings(cpostings, d_from, d_to)

    per_offer_orders = collections.defaultdict(ozon_margin._empty_bucket)
    per_offer_buyouts = collections.defaultdict(ozon_margin._empty_bucket)
    daily = collections.defaultdict(lambda: {"revenue": 0.0, "qty": 0})
    totals_orders = ozon_margin._empty_bucket()
    totals_buyouts = ozon_margin._empty_bucket()
    totals_cancelled = ozon_margin._empty_bucket()
    sku_to_offer = {}
    ozon_margin._accumulate_postings(postings, per_offer_orders, per_offer_buyouts, daily, totals_orders, totals_buyouts, totals_cancelled, sku_to_offer)

    buyout_posting_numbers = {p.get("posting_number") for p in postings if p.get("status") in ozon_margin.BUYOUT_STATUSES}
    client = _build_client(cabinet, ozon_max_retries=2)
    per_sku_accrual, non_item_total = ozon_margin._fetch_accrual_breakdown(client, d_from, d_to, buyout_posting_numbers)
    other_fees_cost = abs(non_item_total)

    per_offer_accrual = collections.defaultdict(lambda: {"commission": 0.0, "delivery": 0.0, "item_fees": 0.0, "bonus": 0.0})
    for sku, offer_id in sku_to_offer.items():
        a = per_sku_accrual.get(str(sku))
        if not a:
            continue
        oa = per_offer_accrual[offer_id]
        oa["commission"] += a["commission"]
        oa["delivery"] += a["delivery"]
        oa["item_fees"] += a["item_fees"]
        oa["bonus"] += a.get("bonus", 0.0)

    total_revenue = totals_buyouts["revenue"]
    total_commission = total_delivery = total_item_fees = total_bonus = total_cogs = total_tax = 0.0
    for offer_id, p in per_offer_buyouts.items():
        accrual = per_offer_accrual.get(offer_id, {"commission": 0.0, "delivery": 0.0, "item_fees": 0.0, "bonus": 0.0})
        total_commission += abs(accrual["commission"])
        total_delivery += abs(accrual["delivery"])
        total_item_fees += abs(accrual["item_fees"])
        total_bonus += accrual["bonus"]
        cogs_unit = cost_prices.get(offer_id, 0)
        total_cogs += cogs_unit * p["qty"]
        total_tax += p["revenue"] * (tax_pct / 100)

    total_payout_real = total_revenue + total_bonus - total_commission - total_delivery - total_item_fees - other_fees_cost
    total_profit = total_payout_real - total_cogs - total_tax
    return {
        "revenue": round(total_revenue, 2), "commission": round(total_commission, 2),
        "bonus": round(total_bonus, 2), "delivery": round(total_delivery, 2),
        "item_fees": round(total_item_fees, 2), "other_fees": round(other_fees_cost, 2),
        "cogs_total": round(total_cogs, 2), "tax": round(total_tax, 2),
        "payout_real": round(total_payout_real, 2), "profit": round(total_profit, 2),
    }


async def _run_fixed_totals(cabinet_id: int, date_from: str, date_to: str):
    state = _fix_verify_state[cabinet_id] = {"running": True}
    try:
        account = await asyncio.to_thread(_compute_fixed_totals_sync, cabinet_id, date_from, date_to)
        state["account"] = account
        state["error"] = None
    except Exception as e:
        state["error"] = str(e)
    state["running"] = False


@app.get("/api/_debug/ozon-fixed-totals-start/{cabinet_id}")
def debug_ozon_fixed_totals_start(cabinet_id: int, telegram_id: int, date_from: str, date_to: str, background_tasks: BackgroundTasks):
    """TEMPORARY — kick off _run_fixed_totals in the background. Poll the
    -status route. Remove both once the fix is verified and shipped."""
    if telegram_id != int(os.environ.get("ADMIN_TELEGRAM_ID", "0")):
        raise HTTPException(status_code=403, detail="admin only")
    cabinet = cabinets.get_cabinet(cabinet_id)
    if not cabinet or cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    if _fix_verify_state.get(cabinet_id, {}).get("running"):
        return {"status": "already running"}
    background_tasks.add_task(_run_fixed_totals, cabinet_id, date_from, date_to)
    return {"status": "started"}


@app.get("/api/_debug/ozon-fixed-totals-status/{cabinet_id}")
def debug_ozon_fixed_totals_status(cabinet_id: int, telegram_id: int):
    if telegram_id != int(os.environ.get("ADMIN_TELEGRAM_ID", "0")):
        raise HTTPException(status_code=403, detail="admin only")
    return _fix_verify_state.get(cabinet_id, {"status": "not started"})


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
            cached_postings=cpostings, cached_accrual_by_date=caccrual,
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
