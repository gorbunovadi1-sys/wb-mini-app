import calendar
import collections
import datetime
import logging
import time

from . import ozon_client
from .ozon_cost_prices import load_cost_prices

log = logging.getLogger("ozon_margin")

EXCLUDED_STATUSES = {"cancelled"}
# "delivered" is the only status that reliably means the customer actually
# kept the item within this window — the seller's real "выкуп". Everything
# else non-cancelled (in transit, awaiting packaging/delivery) is a placed
# "заказ" that hasn't resolved into a kept purchase yet. Profit/margin are
# computed off buyouts, since that's the money that's actually real.
BUYOUT_STATUSES = {"delivered"}

# ITEM (acquiring, packaging materials/partners, temporary storage by partners)
# and NON_ITEM (warehouse placement, insurance, etc.) accrual categories are
# real costs Ozon bills that aren't part of a posting's commission_amount.
# POSTING category is deliberately excluded — its total_amount mixes revenue-
# recognition entries with logistics deductions in a way that isn't safely
# separable (confirmed against live data), so we don't trust it for costs.
OTHER_FEE_CATEGORIES = {"ITEM", "NON_ITEM"}


def _empty_bucket():
    return {"revenue": 0.0, "qty": 0, "commission": 0.0, "payout": 0.0}


def _accumulate_postings(postings, per_offer_orders, per_offer_buyouts, daily, totals_orders, totals_buyouts):
    """Splits FBS+FBO postings into 'orders' (all non-cancelled) and 'buyouts'
    (status=delivered only), both per-offer and account-wide, plus a daily
    orders series for the chart. financial_data.products[].product_id is
    actually the item's SKU (confirmed against live data), matched against
    each product's own sku field, not the catalog product_id."""
    for posting in postings:
        status = posting.get("status")
        if status in EXCLUDED_STATUSES:
            continue
        ts = posting.get("in_process_at") or posting.get("created_at") or ""
        date_str = ts[:10]
        is_buyout = status in BUYOUT_STATUSES

        fin_by_sku = {}
        for fp in (posting.get("financial_data") or {}).get("products", []):
            fin_by_sku[fp.get("product_id")] = fp

        for prod in posting.get("products", []):
            offer_id = prod.get("offer_id")
            if not offer_id:
                continue
            sku = prod.get("sku")
            qty = prod.get("quantity") or 0
            price = float(prod.get("price") or 0)
            revenue = price * qty

            fin = fin_by_sku.get(sku)
            if fin:
                commission = abs(fin.get("commission_amount") or 0)
                payout = fin.get("payout")
                if payout is None:
                    payout = revenue - commission
            else:
                commission = 0.0
                payout = revenue

            po = per_offer_orders[offer_id]
            po["revenue"] += revenue
            po["qty"] += qty
            po["commission"] += commission
            po["payout"] += payout
            po["name"] = prod.get("name") or po.get("name", "")
            po["sku"] = sku

            totals_orders["revenue"] += revenue
            totals_orders["qty"] += qty
            totals_orders["commission"] += commission
            totals_orders["payout"] += payout

            if date_str:
                d = daily[date_str]
                d["revenue"] += revenue
                d["qty"] += qty

            if is_buyout:
                pb = per_offer_buyouts[offer_id]
                pb["revenue"] += revenue
                pb["qty"] += qty
                pb["commission"] += commission
                pb["payout"] += payout
                pb["name"] = po["name"]
                pb["sku"] = sku

                totals_buyouts["revenue"] += revenue
                totals_buyouts["qty"] += qty
                totals_buyouts["commission"] += commission
                totals_buyouts["payout"] += payout


def _fetch_other_fees(client, date_from: datetime.date, date_to: datetime.date):
    """Sums ITEM + NON_ITEM accrual categories for [date_from, date_to] via
    /v1/finance/accrual/by-day — one call per day. Returns a negative number
    (a cost), or 0.0 on total failure."""
    total = 0.0
    d = date_from
    while d <= date_to:
        try:
            accruals = client.get_accrual_by_day(d.isoformat())
        except Exception:
            log.exception(f"accrual/by-day failed for {d.isoformat()}, treating as 0")
            accruals = []
        for acc in accruals:
            if acc.get("accrued_category") in OTHER_FEE_CATEGORIES:
                total += float((acc.get("total_amount") or {}).get("amount") or 0)
        d += datetime.timedelta(days=1)
        time.sleep(0.05)
    return total


def build_margin_summary(
    client=None,
    cost_prices=None,
    days: int = 30,
    date_from: str = None,
    date_to: str = None,
) -> dict:
    """Everything here — orders, buyouts, fees, cost prices, profit, margin —
    is computed for the SAME single period (either `days` back from today, or
    an explicit [date_from, date_to] range), from one source (FBS+FBO
    postings + accrual/by-day). No more mixing a rolling window with a
    separate monthly settlement report."""
    client = client or ozon_client.default_client

    if date_from and date_to:
        d_from = datetime.date.fromisoformat(date_from)
        d_to = datetime.date.fromisoformat(date_to)
    else:
        d_to = datetime.date.today()
        d_from = d_to - datetime.timedelta(days=days - 1)

    period_len = (d_to - d_from).days + 1
    prev_d_to = d_from - datetime.timedelta(days=1)
    prev_d_from = prev_d_to - datetime.timedelta(days=period_len - 1)

    iso_from, iso_to = f"{d_from.isoformat()}T00:00:00Z", f"{d_to.isoformat()}T23:59:59Z"
    prev_iso_from, prev_iso_to = f"{prev_d_from.isoformat()}T00:00:00Z", f"{prev_d_to.isoformat()}T23:59:59Z"

    log.info(f"Fetching Ozon postings for {d_from}..{d_to} (and prior period for comparison)...")
    postings = client.get_fbs_postings(iso_from, iso_to) + client.get_fbo_postings(iso_from, iso_to)
    prev_postings = client.get_fbs_postings(prev_iso_from, prev_iso_to) + client.get_fbo_postings(prev_iso_from, prev_iso_to)

    per_offer_orders = collections.defaultdict(_empty_bucket)
    per_offer_buyouts = collections.defaultdict(_empty_bucket)
    daily = collections.defaultdict(lambda: {"revenue": 0.0, "qty": 0})
    totals_orders = _empty_bucket()
    totals_buyouts = _empty_bucket()
    _accumulate_postings(postings, per_offer_orders, per_offer_buyouts, daily, totals_orders, totals_buyouts)

    prev_totals_buyouts = _empty_bucket()
    _accumulate_postings(
        prev_postings,
        collections.defaultdict(_empty_bucket), collections.defaultdict(_empty_bucket),
        collections.defaultdict(lambda: {"revenue": 0.0, "qty": 0}),
        _empty_bucket(), prev_totals_buyouts,
    )

    log.info(f"Fetching other accrual fees (acquiring, packaging, storage...) for {d_from}..{d_to}...")
    other_fees_cost = abs(_fetch_other_fees(client, d_from, d_to))

    daily_series = [
        {"date": d, "revenue": round(v["revenue"], 2), "qty": v["qty"]}
        for d, v in sorted(daily.items())
    ]
    buyout_rate = round(totals_buyouts["qty"] / totals_orders["qty"] * 100, 1) if totals_orders["qty"] else None

    cost_prices = cost_prices if cost_prices is not None else load_cost_prices()

    products = []
    for offer_id, p in per_offer_buyouts.items():
        cogs_unit = cost_prices.get(offer_id, 0)
        cogs_total = cogs_unit * p["qty"]
        profit = p["payout"] - cogs_total
        margin_pct = (profit / p["revenue"] * 100) if p["revenue"] else 0.0
        products.append({
            "offer_id": offer_id,
            "sku": p.get("sku"),
            "title": p.get("name") or offer_id,
            "revenue": round(p["revenue"], 2),
            "qty": p["qty"],
            "commission": round(p["commission"], 2),
            "payout": round(p["payout"], 2),
            "cogs_unit": cogs_unit,
            "cogs_total": round(cogs_total, 2),
            "profit": round(profit, 2),
            "margin_percent": round(margin_pct, 2),
            "has_cost_price": offer_id in cost_prices,
        })
    products.sort(key=lambda x: -x["revenue"])

    total_revenue = totals_buyouts["revenue"]
    total_commission = totals_buyouts["commission"]
    total_payout = totals_buyouts["payout"]
    total_cogs = sum(pr["cogs_total"] for pr in products)
    total_profit = total_payout - other_fees_cost - total_cogs
    total_margin_pct = (total_profit / total_revenue * 100) if total_revenue else 0.0

    prev_revenue = prev_totals_buyouts["revenue"]
    prev_payout = prev_totals_buyouts["payout"]
    cogs_ratio = (total_cogs / total_revenue) if total_revenue else 0.0
    other_fees_ratio = (other_fees_cost / total_revenue) if total_revenue else 0.0
    prev_cogs_approx = cogs_ratio * prev_revenue
    prev_other_fees_approx = other_fees_ratio * prev_revenue
    prev_profit_approx = prev_payout - prev_other_fees_approx - prev_cogs_approx

    def _delta(cur, prev):
        diff = cur - prev
        pct = (diff / prev * 100) if prev else None
        return {"prev": round(prev, 2), "diff": round(diff, 2), "pct": round(pct, 2) if pct is not None else None}

    return {
        "generated_at": datetime.datetime.now().isoformat(),
        "period_from": d_from.isoformat(),
        "period_to": d_to.isoformat(),
        "period_days": period_len,
        "account": {
            "revenue": round(total_revenue, 2),
            "commission": round(total_commission, 2),
            "other_fees": round(other_fees_cost, 2),
            "payout": round(total_payout, 2),
            "cogs_total": round(total_cogs, 2),
            "profit": round(total_profit, 2),
            "margin_percent": round(total_margin_pct, 2),
            "cost_prices_known_for": sum(1 for pr in products if pr["has_cost_price"]),
            "cost_prices_total_products": len(products),
            "qty_total": totals_buyouts["qty"],
            "orders_qty": totals_orders["qty"],
            "orders_revenue": round(totals_orders["revenue"], 2),
            "buyouts_qty": totals_buyouts["qty"],
            "buyouts_revenue": round(totals_buyouts["revenue"], 2),
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
