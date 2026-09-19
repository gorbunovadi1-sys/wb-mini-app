"""Deterministic text summaries for the three morning digests (Feature C) —
no LLM here: every number below is already computed exactly by backend/
modules the multi-tenant app relies on, so re-deriving it via a prompt would
only add a chance of an invented figure, not remove one. The only genuinely
LLM-shaped piece of Feature C is the review-reply draft — see reviews.py."""
import datetime
import logging

from backend import margin, ozon_margin, wb_ads, wb_stock
from backend.ozon_client import OzonClient
from backend.wb_client import WBClient

from . import config, cost_prices, rate_limit

log = logging.getLogger("seldereeva_bot.digests")

DIGEST_DAYS = 7  # rolling week, not literally "yesterday" — WB's sales-report
# data is built from weekly report chunks and lags by design (see
# backend/margin.py:fetch_rows docstring), so a single-day window is too
# noisy/incomplete to be a meaningful morning number.

LOW_STOCK_THRESHOLD = 5  # units — below this, flag as "риск дефицита"


def _wb_client():
    return WBClient(config.WB_API_KEY) if config.WB_API_KEY else None


def _ozon_client():
    return OzonClient(config.OZON_CLIENT_ID, config.OZON_API_KEY) if (config.OZON_CLIENT_ID and config.OZON_API_KEY) else None


def _fmt_rub(v) -> str:
    return f"{v:,.0f} ₽".replace(",", " ")


def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _fmt_period(period_from: str, period_to: str) -> str:
    f = datetime.date.fromisoformat(period_from)
    t = datetime.date.fromisoformat(period_to)
    if f == t:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        suffix = " (вчера)" if f == yesterday else ""
        return f"{f.strftime('%d.%m')}{suffix}"
    return f"{f.strftime('%d.%m')}–{t.strftime('%d.%m')}"


def finance_digest_text(days: int = None) -> str:
    """По умолчанию (days=None) — вчерашний день целиком, с динамикой к
    позавчера: она попросила именно так 2026-09-17 вместо скользящего
    окна ("рискует быть неполным из-за лага" было верно технически, но
    ей нужен именно вчерашний день, не среднее по неделе). /finance N —
    старое поведение, N дней в среднем окне (тоже полезно, когда нужна
    картина шире одного дня — /finance 14 и т.п.), считает пропорционально
    дольше (см. bot.py:_finance_eta_note)."""
    yesterday_mode = days is None
    lines = ["💰 Финансы за вчера" if yesterday_mode else f"💰 Финансы за {days} дн."]
    any_data = False

    wb = _wb_client()
    if wb:
        try:
            with rate_limit.wb_finance_lock:
                if yesterday_mode:
                    y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
                    summary = margin.build_margin_summary(
                        client=wb, cost_prices=cost_prices.load_cost_prices("wb"), date_from=y, date_to=y, period="daily",
                    )
                else:
                    summary = margin.build_margin_summary(client=wb, cost_prices=cost_prices.load_cost_prices("wb"), days=days)
            acc = summary["account"]
            lines[0] = f"💰 Финансы за {_fmt_period(summary['period_from'], summary['period_to'])}"
            roi = f"{acc['roi_percent']:.0f}%" if acc.get("roi_percent") is not None else "—"
            lines.append(
                "\nWB:\n"
                f"Выручка: {_fmt_rub(acc['revenue'])}\n"
                f"Комиссия WB: −{_fmt_rub(acc['commission'])}\n"
                f"Логистика: −{_fmt_rub(acc['logistics'])}\n"
                f"Хранение: −{_fmt_rub(acc['storage'])}\n"
                f"Реклама: −{_fmt_rub(acc['ad_spend'])}\n"
                f"Себестоимость: −{_fmt_rub(acc['cogs_total'])}\n"
                f"Прибыль: {_fmt_rub(acc['profit'])} ({acc['margin_percent']:.1f}% маржа, ROI {roi})\n"
                f"Средний чек: {_fmt_rub(acc['avg_check'])}, {acc['qty_total']} шт продано, "
                f"возвратов — {acc.get('returns_qty', 0)}\n"
                f"{'К позавчера' if yesterday_mode else 'К пред. периоду'}: "
                f"{_fmt_pct(summary['compare']['profit']['pct'])} по прибыли"
            )
            missing_cost = acc["cost_prices_total_products"] - acc["cost_prices_known_for"]
            if missing_cost:
                lines.append(f"⚠️ Нет себестоимости у {missing_cost} товаров — прибыль по ним не посчитана.")
            any_data = True
        except Exception:
            log.exception("WB finance digest failed")
            lines.append("\nWB: не удалось посчитать (ошибка API) — проверю в следующий раз.")

    ozon = _ozon_client()
    if ozon:
        try:
            if yesterday_mode:
                y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
                summary = ozon_margin.build_margin_summary(
                    client=ozon, cost_prices=cost_prices.load_cost_prices("ozon"), date_from=y, date_to=y, tax_pct=config.TAX_PCT,
                )
            else:
                summary = ozon_margin.build_margin_summary(
                    client=ozon, cost_prices=cost_prices.load_cost_prices("ozon"), days=days, tax_pct=config.TAX_PCT,
                )
            acc = summary.get("account", summary.get("totals", {}))
            lines.append(f"\nOzon: выручка {_fmt_rub(acc.get('revenue', 0))}, прибыль {_fmt_rub(acc.get('profit', 0))}")
            any_data = True
        except Exception:
            log.exception("Ozon finance digest failed")
            lines.append("\nOzon: не удалось посчитать (ошибка API) — проверю в следующий раз.")

    if not any_data:
        return "💰 Финансы: не настроены ключи WB/Ozon (SELD_WB_API_KEY / SELD_OZON_*)."
    return "\n".join(lines)


def ads_digest_text() -> str:
    """9:01 — ДРР и кампании, требующие внимания. Кластерный разбор
    ("Зачистить") — только по запросу в мини-аппе/чате, не здесь: у
    normquery/stats тесный лимит (10 запросов/мин), гонять его каждое утро
    по всем кампаниям нецелесообразно (см. backend/wb_ads.py)."""
    wb = _wb_client()
    if not wb:
        return "📣 Реклама: WB не подключён (SELD_WB_API_KEY)."
    try:
        with rate_limit.wb_finance_lock:
            summary = wb_ads.get_campaigns_summary(wb, days=DIGEST_DAYS)
    except Exception:
        log.exception("Ads digest failed")
        return "📣 Реклама: не удалось получить данные (ошибка API)."

    totals = summary["totals"]
    lines = [
        f"📣 Реклама за {DIGEST_DAYS} дн.: расход {_fmt_rub(totals['spend'])}, "
        f"ДРР {totals['drr']:.1f}%" if totals["drr"] is not None else f"📣 Реклама за {DIGEST_DAYS} дн.: расход {_fmt_rub(totals['spend'])}"
    ]
    attention = [c for c in summary["campaigns"] if c["spend"] > 0 and (c["drr"] is None or c["drr"] > 30)]
    attention.sort(key=lambda c: -c["spend"])
    if attention:
        lines.append("Требуют внимания (ДРР>30% либо без заказов):")
        for c in attention[:5]:
            drr = f"{c['drr']:.0f}%" if c["drr"] is not None else "нет заказов"
            lines.append(f"• {c['name']} — расход {_fmt_rub(c['spend'])}, ДРР {drr}")
    else:
        lines.append("Кампаний с высоким ДРР не найдено.")
    return "\n".join(lines)


def stock_digest_text() -> str:
    """9:02 — остатки с риском дефицита. WB: дни запаса из проданного
    количества (margin summary) и текущего FBO-остатка. Ozon: пока только
    текущий остаток ниже порога — расчёт дней запаса по Ozon требует
    отдельной сверки формы данных ozon_margin по офферам, не делался в
    этой итерации (см. план, Feature C)."""
    lines = ["📦 Остатки"]
    any_data = False

    wb = _wb_client()
    if wb:
        try:
            with rate_limit.wb_finance_lock:
                stock = wb_stock.get_fbo_stock(wb)
                summary = margin.build_margin_summary(client=wb, cost_prices=cost_prices.load_cost_prices("wb"), days=DIGEST_DAYS)
            risky = []
            for p in summary["products"]:
                daily_rate = p["qty"] / DIGEST_DAYS
                on_hand = stock.get(p["nm_id"], 0)
                if daily_rate > 0:
                    days_left = on_hand / daily_rate
                    if days_left < 14:
                        risky.append((days_left, p["title"], on_hand))
            risky.sort()
            if risky:
                lines.append("WB, риск дефицита (<14 дней запаса):")
                for days_left, title, on_hand in risky[:5]:
                    lines.append(f"• {title} — осталось {on_hand} шт., ~{days_left:.0f} дн.")
            else:
                lines.append("WB: явного риска дефицита нет.")
            any_data = True
        except Exception:
            log.exception("WB stock digest failed")
            lines.append("WB: не удалось получить остатки (ошибка API).")

    ozon = _ozon_client()
    if ozon:
        try:
            stocks = ozon.get_all_stocks()
            low = [(offer, s["fbo"] + s["fbs"]) for offer, s in stocks.items() if 0 < s["fbo"] + s["fbs"] < LOW_STOCK_THRESHOLD]
            low.sort(key=lambda x: x[1])
            if low:
                lines.append(f"Ozon, ниже {LOW_STOCK_THRESHOLD} шт.:")
                for offer, qty in low[:5]:
                    lines.append(f"• {offer} — {qty} шт.")
            any_data = True
        except Exception:
            log.exception("Ozon stock digest failed")
            lines.append("Ozon: не удалось получить остатки (ошибка API).")

    if not any_data:
        return "📦 Остатки: не настроены ключи WB/Ozon."
    return "\n".join(lines)


def rollup_digest_text(finance_text: str, ads_text: str, stock_text: str) -> str:
    """Итог по магазину — простой агрегат трёх сводок, без отдельного
    LLM-вызова (см. план: можно добавить позже, если этого не хватит)."""
    today = datetime.date.today().isoformat()
    return f"📋 Итог по магазину на {today}\n\n{finance_text}\n\n{ads_text}\n\n{stock_text}"
