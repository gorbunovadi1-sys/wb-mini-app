import collections
import datetime
import logging

import requests

from . import ozon_client
from .ozon_cost_prices import load_cost_prices

log = logging.getLogger("ozon_margin")

EXCLUDED_STATUSES = {"cancelled"}
# "delivered" is the only status that reliably means the customer actually
# kept the item within this window — the seller's real "выкуп". Everything
# else non-cancelled (in transit, awaiting packaging/delivery) is a placed
# "заказ" that hasn't resolved into a kept purchase yet.
BUYOUT_STATUSES = {"delivered"}


def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _summarize_postings(postings):
    """Builds, from FBS+FBO postings over the selected period: a daily revenue
    series (for the chart), 'orders' (everything placed minus outright
    cancellations), and 'buyouts' (only postings that reached status=delivered
    — the subset of orders actually kept by the customer)."""
    daily = collections.defaultdict(lambda: {"revenue": 0.0, "qty": 0})
    orders = {"revenue": 0.0, "qty": 0}
    buyouts = {"revenue": 0.0, "qty": 0}

    for posting in postings:
        status = posting.get("status")
        if status in EXCLUDED_STATUSES:
            continue
        ts = posting.get("in_process_at") or posting.get("created_at") or ""
        date_str = ts[:10]
        is_buyout = status in BUYOUT_STATUSES

        for prod in posting.get("products", []):
            qty = prod.get("quantity") or 0
            price = float(prod.get("price") or 0)
            revenue = price * qty

            orders["revenue"] += revenue
            orders["qty"] += qty
            if is_buyout:
                buyouts["revenue"] += revenue
                buyouts["qty"] += qty

            if date_str:
                d = daily[date_str]
                d["revenue"] += revenue
                d["qty"] += qty

    return daily, orders, buyouts


def _fetch_report_safe(client, year, month):
    try:
        data = client.get_realization_report(year, month)
        return data.get("rows", []), True
    except requests.HTTPError as e:
        log.warning(f"Realization report {year}-{month:02d} unavailable: {e}")
        return [], False


def _empty_bucket():
    return {"revenue": 0.0, "qty": 0, "fees": 0.0}


def _accumulate_rows(rows, per_offer, totals):
    """Uses the official monthly Отчёт о реализации — actual booked figures, not
    an estimate. Per row: revenue = price × net qty (sale minus same-row return);
    fees = the row's total deduction (delivery_commission.total, net of any
    return_commission.total) — Ozon bundles commission+logistics+coinvestment
    into this one `total` field and doesn't itself split them out further
    (its `commission` sub-field is always 0), so neither do we."""
    for row in rows:
        item = row.get("item") or {}
        offer_id = item.get("offer_id")
        if not offer_id:
            continue
        dc = row.get("delivery_commission") or {}
        rc = row.get("return_commission") or {}
        qty = (dc.get("quantity") or 0) - (rc.get("quantity") or 0)
        price = row.get("seller_price_per_instance") or 0

        revenue = price * qty
        fees = (dc.get("total") or 0) - (rc.get("total") or 0)

        p = per_offer[offer_id]
        p["revenue"] += revenue
        p["qty"] += qty
        p["fees"] += fees
        p["name"] = item.get("name") or p.get("name", "")
        p["sku"] = item.get("sku") or p.get("sku")

        totals["revenue"] += revenue
        totals["qty"] += qty
        totals["fees"] += fees


def build_margin_summary(client=None, cost_prices=None, days: int = 30) -> dict:
    client = client or ozon_client.default_client
    today = datetime.date.today()
    year, month = today.year, today.month

    log.info(f"Fetching Ozon realization report {year}-{month:02d} (факт commission+logistics)...")
    rows, report_ready = _fetch_report_safe(client, year, month)
    if not report_ready:
        # The current month's report isn't published yet (Ozon closes it with a
        # lag) — fall back to the last fully closed month so the dashboard shows
        # real figures instead of zeros.
        year, month = _prev_month(year, month)
        log.info(f"Falling back to {year}-{month:02d}")
        rows, report_ready = _fetch_report_safe(client, year, month)

    prev_year, prev_month = _prev_month(year, month)
    prev_rows, prev_report_ready = _fetch_report_safe(client, prev_year, prev_month)

    per_offer = collections.defaultdict(_empty_bucket)
    totals = _empty_bucket()
    _accumulate_rows(rows, per_offer, totals)

    prev_totals = _empty_bucket()
    _accumulate_rows(prev_rows, collections.defaultdict(_empty_bucket), prev_totals)

    # Daily revenue trend (approximate, built from FBS+FBO postings) — separate
    # from the monthly report above, used only for the chart.
    date_to = today
    fetch_from = date_to - datetime.timedelta(days=days)
    iso_from = f"{fetch_from.isoformat()}T00:00:00Z"
    iso_to = f"{date_to.isoformat()}T23:59:59Z"
    log.info("Fetching Ozon FBS/FBO postings for orders/buyouts and the daily trend...")
    postings = client.get_fbs_postings(iso_from, iso_to) + client.get_fbo_postings(iso_from, iso_to)
    daily, orders, buyouts = _summarize_postings(postings)
    daily_series = [
        {"date": d, "revenue": round(v["revenue"], 2), "qty": v["qty"]}
        for d, v in sorted(daily.items())
    ]
    buyout_rate = round(buyouts["qty"] / orders["qty"] * 100, 1) if orders["qty"] else None

    cost_prices = cost_prices if cost_prices is not None else load_cost_prices()

    products = []
    for offer_id, p in per_offer.items():
        if p["qty"] == 0 and p["revenue"] == 0:
            continue  # a sale fully offset by a return within the same period
        cogs_unit = cost_prices.get(offer_id, 0)
        cogs_total = cogs_unit * p["qty"]
        payout = p["revenue"] - p["fees"]
        profit = payout - cogs_total
        margin_pct = (profit / p["revenue"] * 100) if p["revenue"] else 0.0
        products.append({
            "offer_id": offer_id,
            "sku": p.get("sku"),
            "title": p.get("name") or offer_id,
            "revenue": round(p["revenue"], 2),
            "qty": p["qty"],
            "fees": round(p["fees"], 2),
            "payout": round(payout, 2),
            "cogs_unit": cogs_unit,
            "cogs_total": round(cogs_total, 2),
            "profit": round(profit, 2),
            "margin_percent": round(margin_pct, 2),
            "has_cost_price": offer_id in cost_prices,
        })
    products.sort(key=lambda x: -x["revenue"])

    total_revenue = totals["revenue"]
    total_fees = totals["fees"]
    total_cogs = sum(pr["cogs_total"] for pr in products)
    total_payout = total_revenue - total_fees
    total_profit = total_payout - total_cogs
    total_margin_pct = (total_profit / total_revenue * 100) if total_revenue else 0.0

    prev_revenue = prev_totals["revenue"]
    prev_payout = prev_revenue - prev_totals["fees"]
    cogs_ratio = (total_cogs / total_revenue) if total_revenue else 0.0
    prev_cogs_approx = cogs_ratio * prev_revenue
    prev_profit_approx = prev_payout - prev_cogs_approx

    def _delta(cur, prev):
        diff = cur - prev
        pct = (diff / prev * 100) if prev else None
        return {"prev": round(prev, 2), "diff": round(diff, 2), "pct": round(pct, 2) if pct is not None else None}

    return {
        "generated_at": datetime.datetime.now().isoformat(),
        "period_label": f"{year}-{month:02d}",
        "period_days": days,
        "report_ready": report_ready,
        "prev_report_ready": prev_report_ready,
        "account": {
            "revenue": round(total_revenue, 2),
            "fees": round(total_fees, 2),
            "payout": round(total_payout, 2),
            "cogs_total": round(total_cogs, 2),
            "profit": round(total_profit, 2),
            "margin_percent": round(total_margin_pct, 2),
            "cost_prices_known_for": sum(1 for pr in products if pr["has_cost_price"]),
            "cost_prices_total_products": len(products),
            "qty_total": totals["qty"],
            "orders_qty": orders["qty"],
            "orders_revenue": round(orders["revenue"], 2),
            "buyouts_qty": buyouts["qty"],
            "buyouts_revenue": round(buyouts["revenue"], 2),
            "buyout_rate": buyout_rate,
        },
        "compare": {
            "revenue": _delta(total_revenue, prev_revenue),
            "profit": _delta(total_profit, prev_profit_approx),
            "margin_percent": _delta(
                total_margin_pct,
                (prev_profit_approx / prev_revenue * 100) if prev_revenue else 0.0,
            ),
        },
        "daily": daily_series,
        "products": products,
    }
