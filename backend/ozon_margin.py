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


def accrual_scope_sets(postings: list) -> tuple:
    """The two posting-number sets _fetch_accrual_for_day needs: `buyout`
    (status=delivered — gates commission/bonus, matches revenue) and
    `shipped` (delivered OR cancelled after shipment — gates delivery, since
    Ozon charges real logistics cost once a posting ships regardless of
    whether the customer keeps it). See _fetch_accrual_for_day's docstring
    for the live verification behind this split."""
    buyout = set()
    shipped = set()
    for p in postings:
        pn = p.get("posting_number")
        if not pn:
            continue
        if p.get("status") in BUYOUT_STATUSES:
            buyout.add(pn)
            shipped.add(pn)
        elif (p.get("cancellation") or {}).get("cancelled_after_ship"):
            shipped.add(pn)
    return buyout, shipped


def _real_unit_price(posting: dict, idx: int, prod: dict) -> float:
    """Ozon's posting-list `products[].price` is the NOMINAL reference price
    used to compute commission (matches accrual/by-day's `commission.
    seller_price` — verified live, both equal 48%-of-this-field) — it is NOT
    what the customer actually paid, and NOT what Ozon's own reports count
    as "Выручка". The real recognized revenue (verified exactly, to the
    ruble, against Дарья's own official Ozon "Юнит-экономика" export — its
    "Прибыль за период" column reproduces exactly from Выручка+Баллы+
    Программы minus costs) is `financial_data.products[].customer_price`,
    often 40-50% lower on a heavily-discounted item — this was the real
    cause of Дашборд totals running ~2x too high, not any of the categories
    investigated earlier. `financial_data.products` is a parallel array to
    `products` (same order, same length in every posting checked); falls
    back to the nominal `price` if financial_data is missing/short (very old
    postings, or a shape Ozon hasn't sent here yet) so this never raises."""
    fd_products = (posting.get("financial_data") or {}).get("products") or []
    if idx < len(fd_products):
        customer_price = fd_products[idx].get("customer_price")
        if customer_price is not None:
            return float(customer_price)
    return float(prod.get("price") or 0)


def _accumulate_postings(postings, per_offer_orders, per_offer_buyouts, daily, totals_orders, totals_buyouts, totals_cancelled, sku_to_offer):
    """Splits FBS+FBO postings into 'orders' (all non-cancelled), 'buyouts'
    (status=delivered only) and 'cancelled' (tracked separately so a
    cancelled order's revenue is visible on its own, instead of just
    vanishing), both per-offer (for orders/buyouts) and account-wide, plus a
    daily orders series for the chart. Revenue uses the real customer-paid
    price (see _real_unit_price) — real commission/delivery/fees come from
    accrual/by-day (see _fetch_accrual_breakdown), which financial_data.
    payout turned out to NOT include (verified live: payout was missing the
    delivery deduction entirely, silently overstating profit by the
    shipping cost)."""
    for posting in postings:
        status = posting.get("status")
        if status in EXCLUDED_STATUSES:
            for idx, prod in enumerate(posting.get("products", [])):
                # sku_to_offer must be populated here too, even though this
                # posting's revenue isn't counted — a cancelled-after-ship
                # posting still has real accrued delivery cost (see
                # accrual_scope_sets), and if its SKU never appears in any
                # non-cancelled posting in this window, skipping this leaves
                # that delivery cost computed but orphaned (no offer_id to
                # roll it up under), silently dropping it from the total.
                sku = prod.get("sku")
                offer_id = prod.get("offer_id")
                if sku and offer_id:
                    sku_to_offer[sku] = offer_id
                qty = prod.get("quantity") or 0
                price = _real_unit_price(posting, idx, prod)
                totals_cancelled["revenue"] += price * qty
                totals_cancelled["qty"] += qty
            continue
        ts = posting.get("in_process_at") or posting.get("created_at") or ""
        date_str = ts[:10]
        is_buyout = status in BUYOUT_STATUSES

        for idx, prod in enumerate(posting.get("products", [])):
            offer_id = prod.get("offer_id")
            if not offer_id:
                continue
            sku = prod.get("sku")
            if sku:
                sku_to_offer[sku] = offer_id
            qty = prod.get("quantity") or 0
            price = _real_unit_price(posting, idx, prod)
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


def _fetch_accrual_for_day(client, date_str: str, buyout_posting_numbers: set = None, shipped_posting_numbers: set = None):
    """One day's accrual breakdown: real per-SKU commission + delivery (from
    accrual/by-day's POSTING category — the seller_price/commission/
    delivery.total_accrued triple reconciles exactly to what Ozon actually
    paid out, verified live) plus per-SKU other item-level fees (ITEM
    category) and account-wide fees not tied to any one product (NON_ITEM).
    Returns ({sku: {commission, delivery, item_fees, bonus}}, non_item_total).

    `buyout_posting_numbers` gates commission/bonus — restricts them to
    postings that are actually buyouts (status=delivered), matching revenue
    (which only ever comes from buyouts). Ozon books a POSTING accrual entry
    for essentially every order attempt, cancelled ones included — confirmed
    live: a SKU whose only order was cancelled (0 buyout qty) still had
    several accrual entries. Left unfiltered, those credited commission/
    bonus for units with zero counted revenue. Matches Ozon's own official
    `/v2/finance/realization`, which explicitly excludes cancellations.
    `None` means "don't filter" — used only where a caller can't supply the
    set (keeps this function safe to call standalone).

    Delivery is deliberately NEVER filtered by posting status — tried
    scoping it to "delivered OR cancelled-after-ship" first (reasoning: Ozon
    charges real logistics cost once a posting ships, regardless of outcome)
    via `shipped_posting_numbers` (kept as a parameter for compatibility,
    but no longer read here), but that undercounted badly: only 68 postings
    were flagged `cancelled_after_ship` on cabinet "Строй Мир"/August 2026,
    while real "Услуги доставки" was -197,482₽ vs a fully-unfiltered sum of
    -208,291₽ — trusting that Ozon only books a delivery entry when it
    actually incurred the cost matched far better than guessing from posting
    status. Using the buyout scope for delivery too (an earlier attempt) was
    the single largest remaining piece of the Дашборд-vs-real-balance gap
    after the revenue and commission/bonus fixes.

    The entry's top-level `unit_number` field is confirmed (live, exact
    match against cached `posting_number` values) to just be the
    posting_number under another name.

    Keys are always str(sku) — this dict gets cached through a Postgres JSON
    column, which silently turns int keys into strings on the way back out,
    so keeping them as ints here would make every cached lookup miss (which
    is exactly what happened: commission/delivery/item_fees all silently
    read as 0 for any cache-served period, since sku_to_offer's int keys
    never matched this dict's post-round-trip string keys).

    `bonus` (commission.bonus + commission.coinvestment) is a real credit
    Ozon pays the seller — confirmed against Ozon's own official "Отчёт по
    начислениям" export, where the matching line is "Продажи → Баллы за
    скидки": it's booked under Продажи (sales/revenue), NOT under
    Вознаграждение Ozon (commission). An earlier version of this function
    netted it into `commission` instead, which was wrong on two counts: it
    hid the seller's real, expected commission rate behind a misleadingly
    small number, and miscategorized a revenue-side credit as a
    commission-side one. Keep it separate; callers should add it to revenue
    (or otherwise credit it independently), not subtract it from commission."""
    per_sku = collections.defaultdict(lambda: {"revenue": 0.0, "commission": 0.0, "delivery": 0.0, "item_fees": 0.0, "bonus": 0.0})
    non_item_total = 0.0
    try:
        accruals = client.get_accrual_by_day(date_str)
    except Exception:
        log.exception(f"accrual/by-day failed for {date_str}, treating as empty")
        accruals = []
    for a in accruals:
        cat = a.get("accrued_category")
        if cat == "POSTING":
            unit_number = a.get("unit_number")
            is_buyout_entry = buyout_posting_numbers is None or unit_number in buyout_posting_numbers
            for prod in ((a.get("posting") or {}).get("products") or []):
                sku = prod.get("sku")
                if not sku:
                    continue
                sku = str(sku)
                commission = prod.get("commission") or {}
                delivery = prod.get("delivery") or {}
                if is_buyout_entry:
                    # `sale_price` is the real recognized revenue — verified
                    # exactly (to the kopeck) against Дарья's own "Отчёт по
                    # начислениям" "Выручка" line. Unlike the posting-list's
                    # price/customer_price (a static snapshot from when the
                    # order was placed), this comes from the SAME accrual
                    # ledger as commission/bonus and — crucially — a later
                    # return shows up as a NEW POSTING entry for the same
                    # unit_number with a NEGATIVE sale_price/bonus/
                    # coinvestment, so summing all entries for a unit_number
                    # nets the return out automatically. No separate
                    # returns-tracking system needed.
                    per_sku[sku]["revenue"] += _accrual_amount(commission, "sale_price", "amount")
                    per_sku[sku]["commission"] += _accrual_amount(commission, "commission", "amount")
                    per_sku[sku]["bonus"] += _accrual_amount(commission, "bonus", "amount") + _accrual_amount(commission, "coinvestment", "amount")
                # Delivery is NEVER filtered by posting status — Ozon only
                # books a delivery accrual entry when it actually incurred
                # the cost (confirmed live: an unfiltered sum came out to
                # -208,291₽ vs the real "Услуги доставки" of -197,482₽, a
                # close match; trying to predict which cancelled postings
                # have real shipping cost via `cancelled_after_ship` badly
                # undercounted — only 68 postings flagged, leaving most of
                # the real cost unaccounted for). Trust the entry's own
                # presence as the signal, not our guess about posting status.
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


def _fetch_accrual_breakdown(client, date_from: datetime.date, date_to: datetime.date, buyout_posting_numbers: set = None, shipped_posting_numbers: set = None):
    """Real per-SKU commission/delivery/fees/bonus summed over a date range —
    one call per day, used for the live (uncached) path."""
    per_sku = collections.defaultdict(lambda: {"revenue": 0.0, "commission": 0.0, "delivery": 0.0, "item_fees": 0.0, "bonus": 0.0})
    non_item_total = 0.0
    d = date_from
    while d <= date_to:
        day_sku, day_non_item = _fetch_accrual_for_day(client, d.isoformat(), buyout_posting_numbers, shipped_posting_numbers)
        for sku, vals in day_sku.items():
            per_sku[sku]["revenue"] += vals.get("revenue", 0.0)
            per_sku[sku]["commission"] += vals["commission"]
            per_sku[sku]["delivery"] += vals["delivery"]
            per_sku[sku]["item_fees"] += vals["item_fees"]
            per_sku[sku]["bonus"] += vals["bonus"]
        non_item_total += day_non_item
        d += datetime.timedelta(days=1)
        time.sleep(0.05)
    return per_sku, non_item_total


def fetch_accrual_by_date(client, date_from: datetime.date, date_to: datetime.date, buyout_posting_numbers: set = None, shipped_posting_numbers: set = None):
    """Same per-day accrual fetch, but keeps each day separate instead of
    summing — lets a cached window be sliced to any sub-range later. Used by
    ozon_sales_cache.refresh(), not the live per-request path."""
    accrual_by_date = {}
    non_item_by_date = {}
    d = date_from
    while d <= date_to:
        day_sku, day_non_item = _fetch_accrual_for_day(client, d.isoformat(), buyout_posting_numbers, shipped_posting_numbers)
        accrual_by_date[d.isoformat()] = day_sku
        non_item_by_date[d.isoformat()] = day_non_item
        d += datetime.timedelta(days=1)
        time.sleep(0.05)
    return accrual_by_date, non_item_by_date


def _slice_accrual_by_date(accrual_by_date: dict, non_item_by_date: dict, date_from: datetime.date, date_to: datetime.date):
    per_sku = collections.defaultdict(lambda: {"revenue": 0.0, "commission": 0.0, "delivery": 0.0, "item_fees": 0.0, "bonus": 0.0})
    non_item_total = 0.0
    d = date_from
    while d <= date_to:
        key = d.isoformat()
        for sku, vals in (accrual_by_date.get(key) or {}).items():
            per_sku[sku]["revenue"] += vals.get("revenue", 0.0)
            per_sku[sku]["commission"] += vals["commission"]
            per_sku[sku]["delivery"] += vals["delivery"]
            per_sku[sku]["item_fees"] += vals["item_fees"]
            per_sku[sku]["bonus"] += vals.get("bonus", 0.0)
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
        # The FULL cached pool (not sliced to this period) — a return
        # accrued in this period can belong to an order created in an
        # earlier one, and its posting must still be found here for the
        # accrual scope/rollup below, or that return's reversal silently
        # never counts. See _fetch_accrual_for_day's docstring.
        wide_pool = cached_postings
    else:
        log.info(f"Fetching Ozon postings for {d_from}..{d_to} (and prior period for comparison)...")
        # Fetched from 45 days before d_from (not iso_from) for the same
        # reason as wide_pool above — a live fallback fetch needs the same
        # lookback the cache normally provides, or a return accrued in this
        # period for an order placed slightly earlier gets silently dropped.
        wide_iso_from = f"{(d_from - datetime.timedelta(days=45)).isoformat()}T00:00:00Z"
        wide_pool = client.get_fbs_postings(wide_iso_from, iso_to) + client.get_fbo_postings(wide_iso_from, iso_to)
        postings = _slice_postings(wide_pool, d_from, d_to)
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
        # Cached accrual_by_date is already buyout-filtered at cache-build
        # time (see ozon_sales_cache.refresh) — no further filtering needed.
        per_sku_accrual, non_item_total = _slice_accrual_by_date(cached_accrual_by_date, cached_non_item_by_date, d_from, d_to)
    else:
        log.info(f"Fetching accrual breakdown (commission, delivery, fees) for {d_from}..{d_to}...")
        buyout_posting_numbers, shipped_posting_numbers = accrual_scope_sets(wide_pool)
        per_sku_accrual, non_item_total = _fetch_accrual_breakdown(client, d_from, d_to, buyout_posting_numbers, shipped_posting_numbers)
    other_fees_cost = abs(non_item_total)

    # sku_to_offer from the WIDE pool, not just this period's postings — a
    # return accrued this period can belong to an order (and SKU) from an
    # earlier period; without this, that SKU's accrual data (now correctly
    # scope-included above) has nowhere to roll up to and silently drops.
    # Verified live: this was the actual missing piece for return-aware
    # revenue, not the accrual approach itself — see project_ozon_accrual_api_gap memory.
    full_sku_to_offer = {}
    _accumulate_postings(
        wide_pool, collections.defaultdict(_empty_bucket), collections.defaultdict(_empty_bucket),
        collections.defaultdict(lambda: {"revenue": 0.0, "qty": 0}),
        _empty_bucket(), _empty_bucket(), _empty_bucket(), full_sku_to_offer,
    )

    # Roll per-SKU accrual up to per-offer (an offer normally maps to one SKU;
    # in the rare case Ozon assigns more than one, all are summed together).
    per_offer_accrual = collections.defaultdict(lambda: {"revenue": 0.0, "commission": 0.0, "delivery": 0.0, "item_fees": 0.0, "bonus": 0.0})
    for sku, offer_id in full_sku_to_offer.items():
        a = per_sku_accrual.get(str(sku))
        if not a:
            continue
        oa = per_offer_accrual[offer_id]
        oa["revenue"] += a.get("revenue", 0.0)
        oa["commission"] += a["commission"]
        oa["delivery"] += a["delivery"]
        oa["item_fees"] += a["item_fees"]
        oa["bonus"] += a.get("bonus", 0.0)

    daily_series = [
        {"date": d, "revenue": round(v["revenue"], 2), "qty": v["qty"]}
        for d, v in sorted(daily.items())
    ]
    buyout_rate = round(totals_buyouts["qty"] / totals_orders["qty"] * 100, 1) if totals_orders["qty"] else None

    cost_prices = cost_prices if cost_prices is not None else load_cost_prices()

    products = []
    for offer_id, p in per_offer_buyouts.items():
        accrual = per_offer_accrual.get(offer_id, {"revenue": 0.0, "commission": 0.0, "delivery": 0.0, "item_fees": 0.0, "bonus": 0.0})
        # Ozon reports these as negative (they're deductions); store as
        # positive cost magnitudes for display, subtract explicitly below.
        # `bonus` (commission.bonus + coinvestment — Ozon's own "Баллы за
        # скидки" line, confirmed against the seller's official "Отчёт по
        # начислениям") is a real credit booked under Продажи (sales), not
        # under Вознаграждение Ozon (commission) — kept separate here rather
        # than netted into commission, so the displayed commission still
        # reflects the seller's real, expected rate (not artificially small).
        # `revenue` comes from accrual's `sale_price` (see
        # _fetch_accrual_for_day), NOT the posting-list price — this matches
        # Дарья's real "Выручка" exactly (verified to the kopeck) AND makes
        # returns net out automatically: a return posts a NEGATIVE sale_price
        # entry for the same posting_number, and summing per_offer_accrual
        # from the WIDE postings pool (not just this period's postings —
        # see wide_pool/full_sku_to_offer above) means a return for an
        # order placed in an earlier period still gets found and netted.
        # Verified live end-to-end: payout_real reproduced Дарья's real
        # August balance (975,175.31₽) to the kopeck once this scope was
        # wide enough. qty still comes from the postings list.
        revenue = accrual["revenue"]
        commission = abs(accrual["commission"])
        delivery = abs(accrual["delivery"])
        item_fees = abs(accrual["item_fees"])
        bonus = accrual["bonus"]
        cogs_unit = cost_prices.get(offer_id, 0)
        cogs_total = cogs_unit * p["qty"]
        tax = revenue * (tax_pct / 100)
        profit = revenue + bonus - commission - delivery - item_fees - cogs_total - tax
        margin_pct = (profit / revenue * 100) if revenue else 0.0
        products.append({
            "offer_id": offer_id,
            "title": p.get("name") or offer_id,
            "revenue": round(revenue, 2),
            "qty": p["qty"],
            "commission": round(commission, 2),
            "delivery": round(delivery, 2),
            "item_fees": round(item_fees, 2),
            "bonus": round(bonus, 2),
            "cogs_unit": cogs_unit,
            "cogs_total": round(cogs_total, 2),
            "tax": round(tax, 2),
            "profit": round(profit, 2),
            "margin_percent": round(margin_pct, 2),
            "has_cost_price": offer_id in cost_prices,
        })
    products.sort(key=lambda x: -x["revenue"])

    # NOT summed from `products` (which only lists offers with buyout
    # revenue THIS period) — revenue, commission, bonus and delivery are all
    # accrual-scoped from the WIDE pool now (see wide_pool/full_sku_to_offer
    # above), so an offer that was, say, fully returned this period (zero
    # net buyouts, but real negative accrual entries) still needs to count.
    # Summing per_offer_accrual directly avoids silently dropping that —
    # confirmed live: this was worth ~95K₽/month for delivery alone, and was
    # the actual missing piece for return-aware revenue (not the accrual
    # approach itself, which reproduces Дарья's real balance to the kopeck
    # once the scope is wide enough).
    total_revenue = sum(a["revenue"] for a in per_offer_accrual.values())
    total_commission = sum(abs(a["commission"]) for a in per_offer_accrual.values())
    total_delivery = sum(abs(a["delivery"]) for a in per_offer_accrual.values())
    total_item_fees = sum(abs(a["item_fees"]) for a in per_offer_accrual.values())
    total_bonus = sum(a["bonus"] for a in per_offer_accrual.values())
    total_cogs = sum(pr["cogs_total"] for pr in products)
    total_tax = total_revenue * (tax_pct / 100)
    # "К перечислению" — what Ozon actually pays out for the buyouts, before
    # the seller's OWN costs (cogs, tax) are taken out of that. Everything
    # subtracted here is money Ozon itself keeps, not the seller's expense;
    # bonus is added back since it's a real credit Ozon pays the seller
    # (Продажи → Баллы за скидки in Ozon's own report), not part of what it
    # keeps.
    total_payout_real = total_revenue + total_bonus - total_commission - total_delivery - total_item_fees - other_fees_cost
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
            "bonus": round(total_bonus, 2),
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
