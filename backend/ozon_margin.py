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
    """Two posting-number sets, from a given list of postings: `buyout`
    (status=delivered) and `shipped` (delivered OR cancelled after
    shipment — Ozon charges real logistics cost once a posting ships
    regardless of whether the customer keeps it, so this is the set used to
    gate an "original" accrual entry's inclusion in attribute_accrual_entries,
    covering revenue/commission/bonus/delivery/item_fees together — a
    cancelled-after-ship posting's own sale/commission fields come out to 0
    from Ozon's accrual anyway, so including it in the wider set only
    affects delivery/item_fees, which is the intent). `buyout` alone isn't
    used by attribute_accrual_entries directly anymore, but is still handy
    for callers that need a strict delivered-only set."""
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
    daily orders series for the chart. This `revenue`/`qty` (posting-list
    based) is only used for qty and the daily chart now — the actual
    revenue/commission/delivery/bonus figures come from accrual data (see
    fetch_accrual_entries/attribute_accrual_entries), which financial_data.
    payout also turned out to NOT include delivery (verified live: payout
    was missing the delivery deduction entirely, silently overstating
    profit by the shipping cost)."""
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


def _fetch_accrual_for_day(client, date_str: str):
    """One day's raw accrual/by-day data, flattened into per-(posting,sku)
    entries — deliberately NOT aggregated or filtered by posting status
    here (an earlier version filtered/summed at this point, keyed only by
    SKU+date; that threw away the `unit_number` and made it impossible to
    correctly attribute a settlement-lagged or cross-period entry to the
    right reporting period — see attribute_accrual_entries, which is where
    all the real filtering logic now lives). Returns (entries, non_item_total)
    where entries is a list of {unit_number, sku, date, revenue, commission,
    bonus, delivery, item_fees} dicts — one per (posting, sku) accrual
    contribution seen this day, from both the POSTING category (revenue via
    `commission.sale_price` — verified exactly, to the kopeck, against
    Дарья's real "Отчёт по начислениям" "Выручка" line; commission; bonus =
    commission.bonus + commission.coinvestment, a real credit Ozon pays the
    seller, confirmed against her export's "Баллы за скидки" line, booked
    under Продажи not Вознаграждение Ozon; delivery via
    `delivery.total_accrued`) and the ITEM category (item_fees). NON_ITEM
    entries (account-wide, not tied to any posting) are summed separately
    into non_item_total, unaffected by any of this — they're not "per sale"
    so accrual-date scoping is fine for them.

    The entry's top-level `unit_number` field is confirmed (live, exact
    match against cached `posting_number` values) to just be the
    posting_number under another name — this is what lets a later stage
    correlate an accrual entry back to the specific posting it belongs to."""
    entries = []
    non_item_total = 0.0
    ok = True
    try:
        accruals = client.get_accrual_by_day(date_str)
    except Exception:
        log.exception(f"accrual/by-day failed for {date_str}, treating as empty")
        accruals = []
        ok = False
    for a in accruals:
        cat = a.get("accrued_category")
        unit_number = a.get("unit_number")
        if cat == "POSTING":
            for prod in ((a.get("posting") or {}).get("products") or []):
                sku = prod.get("sku")
                if not sku:
                    continue
                commission = prod.get("commission") or {}
                delivery = prod.get("delivery") or {}
                # type_id 59 = ReturnFlowLogistic = "Обратная логистика" —
                # its own real cost line in Дарья's Юнит-экономика export,
                # distinct from "Логистика" (the forward leg). Split it out:
                # forward-leg delivery is gated by posting-creation-period
                # (real shipping cost, incurred regardless of a later
                # cancellation — verified live: a cancelled-after-ship
                # posting still carries a genuine type_id=32 forward charge
                # that DOES belong in the period), while the reverse leg is
                # gated like a reversal, by its own entry date, in
                # attribute_accrual_entries.
                services = delivery.get("services") or []
                delivery_reverse = sum(
                    _accrual_amount(s, "accrued", "amount") for s in services if s.get("type_id") == 59
                )
                delivery_total = _accrual_amount(delivery, "total_accrued", "amount")
                entries.append({
                    "unit_number": unit_number, "sku": str(sku), "date": date_str,
                    "revenue": _accrual_amount(commission, "sale_price", "amount"),
                    "commission": _accrual_amount(commission, "commission", "amount"),
                    "bonus": _accrual_amount(commission, "bonus", "amount") + _accrual_amount(commission, "coinvestment", "amount"),
                    "delivery": delivery_total - delivery_reverse,
                    "delivery_reverse": delivery_reverse,
                    "item_fees": 0.0,
                })
        elif cat == "ITEM":
            for fee_group in ((a.get("item_fees") or {}).get("fees") or []):
                sku = fee_group.get("sku")
                if not sku:
                    continue
                fee_sum = sum(_accrual_amount(fee, "accrued", "amount") for fee in (fee_group.get("fees") or []))
                entries.append({
                    "unit_number": unit_number, "sku": str(sku), "date": date_str,
                    "revenue": 0.0, "commission": 0.0, "bonus": 0.0, "delivery": 0.0, "delivery_reverse": 0.0,
                    "item_fees": fee_sum,
                })
        elif cat == "NON_ITEM":
            non_item_total += _accrual_amount(a, "non_item_fee", "accrued", "amount")
    return entries, non_item_total, ok


def fetch_accrual_entries(client, date_from: datetime.date, date_to: datetime.date):
    """Flat, un-aggregated per-(posting,sku) accrual records for a date
    range — NOT summed or bucketed by day, because "which day an entry
    happened to post on" is the wrong key for attributing it to a reporting
    period (see attribute_accrual_entries). One call per day.

    A day whose request exhausts its retries (sustained 429s, say) is
    treated as empty rather than aborting the whole range — that day's
    isoformat date is collected into the returned `failed_dates` list so a
    caller (ozon_sales_cache.refresh, in particular) can tell "this window
    is genuinely all-zero" apart from "this window is mostly-empty because
    Ozon was unreachable for a chunk of it" and react accordingly (e.g. fall
    back to a previous cache instead of overwriting it with degraded data)."""
    entries = []
    non_item_by_date = {}
    failed_dates = []
    d = date_from
    while d <= date_to:
        day_entries, day_non_item, day_ok = _fetch_accrual_for_day(client, d.isoformat())
        entries.extend(day_entries)
        non_item_by_date[d.isoformat()] = day_non_item
        if not day_ok:
            failed_dates.append(d.isoformat())
        d += datetime.timedelta(days=1)
        time.sleep(0.05)
    return entries, non_item_by_date, failed_dates


def attribute_accrual_entries(entries: list, buyout_posting_numbers: set, created_posting_numbers: set, period_from: str, period_to: str) -> dict:
    """Sums accrual entries into per-SKU totals for [period_from, period_to],
    handling Ozon's settlement lag correctly. Three different attribution
    rules, because "which kind of event is this" matters more than a single
    revenue-sign check turned out to capture:

    - **Sale fields** (revenue, commission, bonus — always move together,
      from the same accrual entry) and **item_fees**: an "original" entry
      (revenue >= 0, or an item-fee entry with no revenue field at all)
      belongs to the period if its POSTING was created within it AND is a
      genuine buyout (`buyout_posting_numbers` — status=delivered), matching
      Ozon's own `/v2/finance/realization`, which excludes cancellations —
      regardless of which day the entry itself settled on (this is what
      survives Ozon's real settlement lag: verified live, an August
      "тачка2-02нов" order's revenue/commission was still settling as late
      as September 12th). A "reversal" (revenue < 0 — a return) instead
      belongs if ITS OWN date falls in the period, regardless of when the
      original posting was created — matching how Ozon books "Возврат
      выручки" on the return's date, independent of the original sale.

    - **Delivery, forward leg** (`delivery` — everything except return-flow
      logistics, see below): belongs if its posting was created within the
      period, **regardless of final status** (`created_posting_numbers` —
      no delivered/cancelled filter at all). Verified live, raw entry by raw
      entry: a posting cancelled AFTER shipment still carries a real,
      separate type_id=32 ("Логистика") charge — Ozon incurs that cost the
      moment it ships, independent of whether the sale later completes, and
      a cancelled-before-ship posting simply has no delivery entry to sum in
      the first place, so this scope doesn't need to distinguish further.

    - **Delivery, reverse leg** (`delivery_reverse` — type_id=59,
      ReturnFlowLogistic = "Обратная логистика", split out in
      _fetch_accrual_for_day): treated like a reversal — belongs if its own
      date falls in the period, regardless of posting scope. This has its
      own real line in Дарья's Юнит-экономика export, separate from
      "Логистика", and behaves like a return event, not an original sale.

    `entries` should cover [period_from, period_from + enough lag buffer] —
    the caller is responsible for fetching wide enough forward that
    settlement has actually landed by the time this runs."""
    per_sku = collections.defaultdict(lambda: {"revenue": 0.0, "commission": 0.0, "delivery": 0.0, "item_fees": 0.0, "bonus": 0.0})
    for e in entries:
        is_original = e["revenue"] >= 0
        s = per_sku[e["sku"]]

        sale_belongs = (e["unit_number"] in buyout_posting_numbers) if is_original else (period_from <= e["date"] <= period_to)
        if sale_belongs:
            s["revenue"] += e["revenue"]
            s["commission"] += e["commission"]
            s["bonus"] += e["bonus"]
            s["item_fees"] += e["item_fees"]

        if e["unit_number"] in created_posting_numbers:
            s["delivery"] += e["delivery"]
        if period_from <= e["date"] <= period_to:
            s["delivery"] += e.get("delivery_reverse", 0.0)
    return dict(per_sku)


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
    cached_accrual_entries: list = None,
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

    # A cache row from before the entry-based accrual redesign has
    # cached_accrual_entries=None (new DB column, nullable, NULL until the
    # next refresh) — detect that and fall through to the live path instead
    # of silently misusing old-format data. (An earlier, narrower version of
    # this check — sampling cached_accrual_by_date for a "revenue" key —
    # caught a similar but different format break live in production; this
    # replaces it now that the whole accrual cache shape changed again.)
    use_cache = (
        cached_postings is not None and cached_accrual_entries is not None
        and cache_cover_from is not None
        and datetime.date.fromisoformat(cache_cover_from) <= prev_d_from
    )

    if use_cache:
        log.info(f"Serving Ozon margin for {d_from}..{d_to} from cache ({len(cached_postings)} cached postings)")
        postings = _slice_postings(cached_postings, d_from, d_to)
        prev_postings = _slice_postings(cached_postings, prev_d_from, prev_d_to)
        # The FULL cached pool — needed only to resolve SKU→offer_id for a
        # return's SKU when its original order was created in an earlier
        # period (attribute_accrual_entries's reversal rule doesn't need
        # posting-scope matching, only date matching, but the rollup below
        # still needs to know which offer that SKU belongs to).
        wide_pool = cached_postings
        entries = cached_accrual_entries
        non_item_by_date = cached_non_item_by_date or {}
    else:
        log.info(f"Fetching Ozon postings for {d_from}..{d_to} (and prior period for comparison)...")
        postings = client.get_fbs_postings(iso_from, iso_to) + client.get_fbo_postings(iso_from, iso_to)
        prev_postings = client.get_fbs_postings(prev_iso_from, prev_iso_to) + client.get_fbo_postings(prev_iso_from, prev_iso_to)
        # 45 days before d_from — a return dated within [d_from,d_to] can
        # belong to an order created earlier; wide_pool only needs to
        # resolve that return's SKU to an offer_id (attribute_accrual_entries
        # already handles the actual date logic), but it still needs that
        # earlier posting to exist somewhere to look up.
        wide_iso_from = f"{(d_from - datetime.timedelta(days=45)).isoformat()}T00:00:00Z"
        wide_pool = client.get_fbs_postings(wide_iso_from, iso_to) + client.get_fbo_postings(wide_iso_from, iso_to)
        # Fetched through d_to + 45 days (capped at today), not just d_to —
        # Ozon settles a sale's revenue/commission accrual with a real lag
        # (verified live: an order placed in August was still settling into
        # its accrual as late as 12 days after month-end), so an accrual
        # fetch limited to exactly the requested period silently misses late
        # settlements for orders placed near its end. See
        # attribute_accrual_entries's docstring for how this is reconciled
        # without double-counting.
        entries_fetch_to = min(datetime.date.today(), d_to + datetime.timedelta(days=45))
        entries_fetch_to = max(entries_fetch_to, d_to)
        log.info(f"Fetching accrual entries for {d_from}..{entries_fetch_to} (settlement-lag buffer past {d_to})...")
        entries, non_item_by_date, failed_dates = fetch_accrual_entries(client, d_from, entries_fetch_to)
        if failed_dates:
            log.warning(f"Accrual fetch had {len(failed_dates)} failed day(s), treated as empty: {failed_dates}")

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

    # buyout_posting_numbers (status=delivered) gates sale fields/item_fees;
    # created_posting_numbers (any status, just "was this posting created in
    # the period") gates delivery's forward leg — see
    # attribute_accrual_entries's docstring for the full reasoning (this
    # split itself went through two revisions: first delivery had no status
    # filter at all, which over-counted cancelled-after-ship logistics that
    # her report doesn't attribute to the product; then it was narrowed to
    # strict buyout, which under-counted, because a cancelled-after-ship
    # posting genuinely DOES carry a real forward-leg "Логистика" charge —
    # confirmed raw, entry by entry; only the reverse-leg "Обратная
    # логистика" behaves like a return and needs its own date-based rule).
    buyout_posting_numbers, _ = accrual_scope_sets(postings)
    created_posting_numbers = {p.get("posting_number") for p in postings if p.get("posting_number")}
    per_sku_accrual = attribute_accrual_entries(entries, buyout_posting_numbers, created_posting_numbers, d_from.isoformat(), d_to.isoformat())
    non_item_total = sum(
        v for k, v in non_item_by_date.items() if d_from.isoformat() <= k <= d_to.isoformat()
    )
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
