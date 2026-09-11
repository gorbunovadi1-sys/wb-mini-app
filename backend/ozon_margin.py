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


def _empty_bucket():
    return {"revenue": 0.0, "qty": 0}


def _accumulate_postings(postings, per_offer_orders, per_offer_buyouts, daily, totals_orders, totals_buyouts, totals_cancelled, sku_to_offer):
    """Splits FBS+FBO postings into 'orders' (all non-cancelled), 'buyouts'
    (status=delivered only) and 'cancelled' (tracked separately so a
    cancelled order's seller-price revenue is visible on its own, instead of
    just vanishing), both per-offer (for orders/buyouts) and account-wide,
    plus a daily orders series for the chart. Only revenue/qty come from
    here now — real commission/delivery/fees come from accrual/by-day (see
    _fetch_accrual_breakdown), which financial_data.payout turned out to
    NOT include (verified live: payout was missing the delivery deduction
    entirely, silently overstating profit by the shipping cost)."""
    for posting in postings:
        status = posting.get("status")
        if status in EXCLUDED_STATUSES:
            for prod in posting.get("products", []):
                qty = prod.get("quantity") or 0
                price = float(prod.get("price") or 0)
                totals_cancelled["revenue"] += price * qty
                totals_cancelled["qty"] += qty
            continue
        ts = posting.get("in_process_at") or posting.get("created_at") or ""
        date_str = ts[:10]
        is_buyout = status in BUYOUT_STATUSES

        for prod in posting.get("products", []):
            offer_id = prod.get("offer_id")
            if not offer_id:
                continue
            sku = prod.get("sku")
            if sku:
                sku_to_offer[sku] = offer_id
            qty = prod.get("quantity") or 0
            price = float(prod.get("price") or 0)
            revenue = price * qty

            po = per_offer_orders[offer_id]
            po["revenue"] += revenue
            po["qty"] += qty
            po["name"] = prod.get("name") or po.get("name", "")

            totals_orders["revenue"] += revenue
            totals_orders["qty"] += qty

            if date_str:
                d = daily[date_str]
                d["revenue"] += revenue
                d["qty"] += qty

            if is_buyout:
                pb = per_offer_buyouts[offer_id]
                pb["revenue"] += revenue
                pb["qty"] += qty
                pb["name"] = po["name"]

                totals_buyouts["revenue"] += revenue
                totals_buyouts["qty"] += qty


def _accrual_amount(obj, *path):
    for key in path:
        if obj is None:
            return 0.0
        obj = obj.get(key)
    try:
        return float(obj or 0)
    except (TypeError, ValueError):
        return 0.0


def _fetch_accrual_for_day(client, date_str: str):
    """One day's accrual breakdown: real per-SKU commission + delivery (from
    accrual/by-day's POSTING category — the seller_price/commission/
    delivery.total_accrued triple reconciles exactly to what Ozon actually
    paid out, verified live) plus per-SKU other item-level fees (ITEM
    category) and account-wide fees not tied to any one product (NON_ITEM).
    Returns ({sku: {commission, delivery, item_fees}}, non_item_total). Keys
    are always str(sku) — this dict gets cached through a Postgres JSON
    column, which silently turns int keys into strings on the way back out,
    so keeping them as ints here would make every cached lookup miss (which
    is exactly what happened: commission/delivery/item_fees all silently
    read as 0 for any cache-served period, since sku_to_offer's int keys
    never matched this dict's post-round-trip string keys)."""
    per_sku = collections.defaultdict(lambda: {"commission": 0.0, "delivery": 0.0, "item_fees": 0.0})
    non_item_total = 0.0
    try:
        accruals = client.get_accrual_by_day(date_str)
    except Exception:
        log.exception(f"accrual/by-day failed for {date_str}, treating as empty")
        accruals = []
    for a in accruals:
        cat = a.get("accrued_category")
        if cat == "POSTING":
            for prod in ((a.get("posting") or {}).get("products") or []):
                sku = prod.get("sku")
                if not sku:
                    continue
                sku = str(sku)
                commission = prod.get("commission") or {}
                delivery = prod.get("delivery") or {}
                per_sku[sku]["commission"] += _accrual_amount(commission, "commission", "amount")
                per_sku[sku]["delivery"] += _accrual_amount(delivery, "total_accrued", "amount")
        elif cat == "ITEM":
            for fee_group in ((a.get("item_fees") or {}).get("fees") or []):
                sku = fee_group.get("sku")
                if not sku:
                    continue
                sku = str(sku)
                for fee in (fee_group.get("fees") or []):
                    per_sku[sku]["item_fees"] += _accrual_amount(fee, "accrued", "amount")
        elif cat == "NON_ITEM":
            non_item_total += _accrual_amount(a, "non_item_fee", "accrued", "amount")
    return dict(per_sku), non_item_total


def _fetch_accrual_breakdown(client, date_from: datetime.date, date_to: datetime.date):
    """Real per-SKU commission/delivery/fees summed over a date range — one
    call per day, used for the live (uncached) path."""
    per_sku = collections.defaultdict(lambda: {"commission": 0.0, "delivery": 0.0, "item_fees": 0.0})
    non_item_total = 0.0
    d = date_from
    while d <= date_to:
        day_sku, day_non_item = _fetch_accrual_for_day(client, d.isoformat())
        for sku, vals in day_sku.items():
            per_sku[sku]["commission"] += vals["commission"]
            per_sku[sku]["delivery"] += vals["delivery"]
            per_sku[sku]["item_fees"] += vals["item_fees"]
        non_item_total += day_non_item
        d += datetime.timedelta(days=1)
        time.sleep(0.05)
    return per_sku, non_item_total


def fetch_accrual_by_date(client, date_from: datetime.date, date_to: datetime.date):
    """Same per-day accrual fetch, but keeps each day separate instead of
    summing — lets a cached window be sliced to any sub-range later. Used by
    ozon_sales_cache.refresh(), not the live per-request path."""
    accrual_by_date = {}
    non_item_by_date = {}
    d = date_from
    while d <= date_to:
        day_sku, day_non_item = _fetch_accrual_for_day(client, d.isoformat())
        accrual_by_date[d.isoformat()] = day_sku
        non_item_by_date[d.isoformat()] = day_non_item
        d += datetime.timedelta(days=1)
        time.sleep(0.05)
    return accrual_by_date, non_item_by_date


def _slice_accrual_by_date(accrual_by_date: dict, non_item_by_date: dict, date_from: datetime.date, date_to: datetime.date):
    per_sku = collections.defaultdict(lambda: {"commission": 0.0, "delivery": 0.0, "item_fees": 0.0})
    non_item_total = 0.0
    d = date_from
    while d <= date_to:
        key = d.isoformat()
        for sku, vals in (accrual_by_date.get(key) or {}).items():
            per_sku[sku]["commission"] += vals["commission"]
            per_sku[sku]["delivery"] += vals["delivery"]
            per_sku[sku]["item_fees"] += vals["item_fees"]
        non_item_total += non_item_by_date.get(key, 0.0)
        d += datetime.timedelta(days=1)
    return per_sku, non_item_total


def _posting_date(posting: dict) -> str:
    ts = posting.get("in_process_at") or posting.get("created_at") or ""
    return ts[:10]


def _slice_postings(postings: list, date_from: datetime.date, date_to: datetime.date) -> list:
    lo, hi = date_from.isoformat(), date_to.isoformat()
    return [p for p in postings if lo <= _posting_date(p) <= hi]


def build_margin_summary(
    client=None,
    cost_prices=None,
    days: int = 30,
    date_from: str = None,
    date_to: str = None,
    tax_pct: float = 0,
    cached_postings: list = None,
    cached_accrual_by_date: dict = None,
    cached_non_item_by_date: dict = None,
    cache_cover_from: str = None,
) -> dict:
    """Everything here — orders, buyouts, commission, delivery, fees, cost
    prices, tax, profit, margin — is computed for the SAME single period
    (either `days` back from today, or an explicit [date_from, date_to]
    range). Commission/delivery/item-fees come from accrual/by-day (real,
    per-SKU); postings only supply revenue/qty and order-vs-buyout status.
    `tax_pct` is charged on sale price (revenue), not on what Ozon pays out —
    matches how a seller's own turnover-based tax (УСН "доходы" etc.) works.

    If `cached_*` (from ozon_sales_cache) cover the requested range plus the
    prior period needed for comparison, everything is sliced from them
    in-memory instead of calling Ozon live — repeating the live fetch on
    every tab open (Дашборд/Детализация/Аналитика each call this
    independently) was on its own enough to trigger sustained 429s."""
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

    use_cache = (
        cached_postings is not None and cached_accrual_by_date is not None
        and cache_cover_from is not None
        and datetime.date.fromisoformat(cache_cover_from) <= prev_d_from
    )

    if use_cache:
        log.info(f"Serving Ozon margin for {d_from}..{d_to} from cache ({len(cached_postings)} cached postings)")
        postings = _slice_postings(cached_postings, d_from, d_to)
        prev_postings = _slice_postings(cached_postings, prev_d_from, prev_d_to)
    else:
        log.info(f"Fetching Ozon postings for {d_from}..{d_to} (and prior period for comparison)...")
        postings = client.get_fbs_postings(iso_from, iso_to) + client.get_fbo_postings(iso_from, iso_to)
        prev_postings = client.get_fbs_postings(prev_iso_from, prev_iso_to) + client.get_fbo_postings(prev_iso_from, prev_iso_to)

    per_offer_orders = collections.defaultdict(_empty_bucket)
    per_offer_buyouts = collections.defaultdict(_empty_bucket)
    daily = collections.defaultdict(lambda: {"revenue": 0.0, "qty": 0})
    totals_orders = _empty_bucket()
    totals_buyouts = _empty_bucket()
    totals_cancelled = _empty_bucket()
    sku_to_offer = {}
    _accumulate_postings(postings, per_offer_orders, per_offer_buyouts, daily, totals_orders, totals_buyouts, totals_cancelled, sku_to_offer)

    prev_totals_buyouts = _empty_bucket()
    _accumulate_postings(
        prev_postings,
        collections.defaultdict(_empty_bucket), collections.defaultdict(_empty_bucket),
        collections.defaultdict(lambda: {"revenue": 0.0, "qty": 0}),
        _empty_bucket(), prev_totals_buyouts, _empty_bucket(), {},
    )

    if use_cache:
        per_sku_accrual, non_item_total = _slice_accrual_by_date(cached_accrual_by_date, cached_non_item_by_date, d_from, d_to)
    else:
        log.info(f"Fetching accrual breakdown (commission, delivery, fees) for {d_from}..{d_to}...")
        per_sku_accrual, non_item_total = _fetch_accrual_breakdown(client, d_from, d_to)
    other_fees_cost = abs(non_item_total)

    # Roll per-SKU accrual up to per-offer (an offer normally maps to one SKU;
    # in the rare case Ozon assigns more than one, all are summed together).
    per_offer_accrual = collections.defaultdict(lambda: {"commission": 0.0, "delivery": 0.0, "item_fees": 0.0})
    for sku, offer_id in sku_to_offer.items():
        a = per_sku_accrual.get(str(sku))
        if not a:
            continue
        oa = per_offer_accrual[offer_id]
        oa["commission"] += a["commission"]
        oa["delivery"] += a["delivery"]
        oa["item_fees"] += a["item_fees"]

    daily_series = [
        {"date": d, "revenue": round(v["revenue"], 2), "qty": v["qty"]}
        for d, v in sorted(daily.items())
    ]
    buyout_rate = round(totals_buyouts["qty"] / totals_orders["qty"] * 100, 1) if totals_orders["qty"] else None

    cost_prices = cost_prices if cost_prices is not None else load_cost_prices()

    products = []
    for offer_id, p in per_offer_buyouts.items():
        accrual = per_offer_accrual.get(offer_id, {"commission": 0.0, "delivery": 0.0, "item_fees": 0.0})
        # Ozon reports these as negative (they're deductions); store as
        # positive cost magnitudes for display, subtract explicitly below.
        commission = abs(accrual["commission"])
        delivery = abs(accrual["delivery"])
        item_fees = abs(accrual["item_fees"])
        cogs_unit = cost_prices.get(offer_id, 0)
        cogs_total = cogs_unit * p["qty"]
        tax = p["revenue"] * (tax_pct / 100)
        profit = p["revenue"] - commission - delivery - item_fees - cogs_total - tax
        margin_pct = (profit / p["revenue"] * 100) if p["revenue"] else 0.0
        products.append({
            "offer_id": offer_id,
            "title": p.get("name") or offer_id,
            "revenue": round(p["revenue"], 2),
            "qty": p["qty"],
            "commission": round(commission, 2),
            "delivery": round(delivery, 2),
            "item_fees": round(item_fees, 2),
            "cogs_unit": cogs_unit,
            "cogs_total": round(cogs_total, 2),
            "tax": round(tax, 2),
            "profit": round(profit, 2),
            "margin_percent": round(margin_pct, 2),
            "has_cost_price": offer_id in cost_prices,
        })
    products.sort(key=lambda x: -x["revenue"])

    total_revenue = totals_buyouts["revenue"]
    total_commission = sum(pr["commission"] for pr in products)
    total_delivery = sum(pr["delivery"] for pr in products)
    total_item_fees = sum(pr["item_fees"] for pr in products)
    total_cogs = sum(pr["cogs_total"] for pr in products)
    total_tax = sum(pr["tax"] for pr in products)
    # "К перечислению" — what Ozon actually pays out for the buyouts, before
    # the seller's OWN costs (cogs, tax) are taken out of that. Everything
    # subtracted here is money Ozon itself keeps, not the seller's expense.
    total_payout_real = total_revenue - total_commission - total_delivery - total_item_fees - other_fees_cost
    total_profit = total_payout_real - total_cogs - total_tax
    total_margin_pct = (total_profit / total_revenue * 100) if total_revenue else 0.0

    prev_revenue = prev_totals_buyouts["revenue"]
    # Previous period's real commission/delivery/fees aren't fetched (would
    # double the accrual calls for a number only used in the comparison
    # delta) — approximated using this period's overall cost-to-revenue
    # ratio applied to previous revenue, same approach as cost prices below.
    cost_ratio = (total_revenue - total_profit) / total_revenue if total_revenue else 0.0
    prev_profit_approx = prev_revenue * (1 - cost_ratio)

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
            "delivery": round(total_delivery, 2),
            "item_fees": round(total_item_fees, 2),
            "other_fees": round(other_fees_cost, 2),
            "cogs_total": round(total_cogs, 2),
            "tax": round(total_tax, 2),
            "tax_pct": tax_pct,
            "payout_real": round(total_payout_real, 2),
            "profit": round(total_profit, 2),
            "margin_percent": round(total_margin_pct, 2),
            "cost_prices_known_for": sum(1 for pr in products if pr["has_cost_price"]),
            "cost_prices_total_products": len(products),
            "qty_total": totals_buyouts["qty"],
            "orders_qty": totals_orders["qty"],
            "orders_revenue": round(totals_orders["revenue"], 2),
            "cancelled_qty": totals_cancelled["qty"],
            "cancelled_revenue": round(totals_cancelled["revenue"], 2),
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
