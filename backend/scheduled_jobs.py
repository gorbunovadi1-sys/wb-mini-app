"""Background jobs that talk to Ozon/WB on a schedule — promo/margin/dimension
checks and the WB/Ozon sales-cache refresh. Deliberately kept free of any
FastAPI/`app` dependency: this module is imported both by `ai_engine_app`
(no longer used there directly, kept for reference/admin routes) and by
`worker.py`, the standalone process that now owns actually running these on
a schedule — see that file's module docstring for why the split exists."""
import asyncio
import logging

from . import cabinets
from . import ozon_dimensions
from . import ozon_price_monitor
from . import ozon_pricing
from . import ozon_promo_guard
from . import ozon_sales_cache
from . import wb_sales_cache
from .ozon_client import OzonClient
from .wb_client import WBClient

log = logging.getLogger("scheduled_jobs")


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
                        expense = (
                            price * (p["commission_pct"] / 100)
                            + p["logistics_estimate"]
                            + price * ((p.get("acquiring_pct") or 0) / 100)
                        )
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


# Hard ceiling on how long a single cabinet's background refresh may run.
# Observed live (2026-09-13): under Ozon's sustained rate limiting, the hot
# window's per-day accrual calls (up to 45 of them, each retried up to 3x
# with escalating backoff) can each burn ~30s worst case — enough bad days
# in a row stretches one cabinet's refresh to several minutes. Railway's
# own memory metric showed the process reporting the exact same value for
# 4+ minutes straight (a stall, not just slow), its health check decided
# the app was unresponsive, and killed the instance ("Application failed
# to respond") even though CPU/memory were nowhere near their limits — this
# was a hang, not a resource shortage. asyncio.wait_for can't force-kill
# the underlying thread (Python threads aren't cancellable), but it stops
# THIS loop from ever waiting on one stuck cabinet indefinitely, so a
# lingering thread from a bad cabinet no longer blocks every other
# cabinet's turn or delays the loop's own progress.
CABINET_REFRESH_TIMEOUT_SECONDS = 90


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
            await asyncio.wait_for(
                asyncio.to_thread(ozon_sales_cache.refresh, client, cabinet["id"]),
                timeout=CABINET_REFRESH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning(f"Ozon sales cache refresh for cabinet {cabinet['id']} exceeded {CABINET_REFRESH_TIMEOUT_SECONDS}s — moving on, will retry next cycle")
        except Exception:
            log.exception(f"Ozon sales cache refresh failed for cabinet {cabinet['id']}")
        await asyncio.sleep(3)  # small gap between cabinets — avoids a startup burst across many cabinets at once
