from . import cabinets, ozon_margin, ozon_sales_cache

# Below this many units sold in the 60-day window, commission/revenue (and
# the other per-unit ratios) are too noisy to trust as "the real rate" —
# verified live: an offer with only a couple of units (one of them
# apparently carrying an odd accrual, e.g. a partial return) showed a
# commission% of 76-77% against Ozon's own official category rate card of
# 51% for that category, and the number visibly drifted between cache
# refreshes (76.4% → 77.1%) purely from sample-size noise, not a real rate
# change. Below the threshold, fall back to the estimate (Ozon's quoted
# category reference rate) instead — see get_pricing_list.
MIN_QTY_FOR_REAL_RATE = 5

# How far (in percentage points) a real-data commission% may stray from
# Ozon's own quoted category rate before it's distrusted entirely — see
# get_pricing_list. Loose enough to allow genuine cross-category/price-
# bracket variation, tight enough to reject the kind of distortion caught
# live (76-91% real vs 51% or lower quoted).
REAL_VS_QUOTED_SANITY_MARGIN_PCT = 15


def _real_rates_by_offer(cabinet_id: int):
    """Real per-offer commission % and logistics-per-unit, derived from the
    same cached accrual data Дашборд/Детализация use — an actual weighted
    average of what this product really sold through recently, whatever mix
    of FBO/FBS that was. Far more accurate than the static estimate below,
    which only ever reads FBS-specific rate fields regardless of how the
    product actually ships. Returns ({}, 0) if there's no cache yet for this
    cabinet — callers fall back to the estimate; this never makes a live
    Ozon call on its own, so it's safe to call on every pricing/promo load.

    Also returns shop_avg_acquiring_pct — acquiring has no static "reference
    rate" field in Ozon's own commissions object (unlike commission %/
    logistics, which Ozon quotes per category even for an unsold item), so
    a product with no sales history has nothing to estimate it from. Ozon
    charges acquiring as a % of the item's price (its own commission-tariff
    examples quote it as "цена × тариф банка"), not a flat per-unit ruble
    amount — using a flat ruble average across the whole cabinet silently
    mis-estimates it for any item priced far from the cabinet's average
    (and, worse, stays wrong at every OTHER price the seller might type into
    Цены/Акции, since a flat ruble number doesn't move with price the way
    commission already does). The cabinet-wide average expressed as a %
    (total acquiring ÷ total nominal revenue, across everything with real
    data) is a far better guess than 0 for a product with no sales yet."""
    cached = ozon_sales_cache.get(cabinet_id)
    if not cached:
        return {}, 0
    cpostings, caccrual, cnonitem, ccover_from, _is_stale = cached
    try:
        result = ozon_margin.build_margin_summary(
            client=None, cost_prices={}, days=60,
            cached_postings=cpostings, cached_accrual_entries=caccrual,
            cached_non_item_by_date=cnonitem, cache_cover_from=ccover_from,
        )
    except Exception:
        return {}, 0
    rates = {}
    total_acquiring, total_nominal_revenue = 0.0, 0.0
    for p in result.get("products", []):
        if not p.get("qty") or not p.get("revenue"):
            continue
        # Commission is Ozon's cut of the NOMINAL price (before any
        # Ozon-funded promo discount) — the bonus/coinvestment credit is
        # exactly that discount refunded to the seller (verified: bonus ==
        # seller_price - sale_price, see project_ozon_bonus_coinvestment_fix
        # in session memory), so `revenue` (sale_price, net of the discount)
        # understates the price commission was actually charged against.
        # Dividing by revenue alone overstated commission% for any offer
        # that historically sold through a bonus-funded promo — caught live
        # (2026-09-13): a "Бур" offer showed 76-77% here against Ozon's own
        # 51% category rate card, and adding bonus back into the
        # denominator brings it back in line. Acquiring is charged against
        # the same nominal price, so it uses the same denominator.
        nominal_revenue = p["revenue"] + p["bonus"]
        total_acquiring += p["item_fees"]
        total_nominal_revenue += nominal_revenue
        # The cabinet-wide acquiring average above benefits from every data
        # point, however small — but a per-offer rate entry below this many
        # units is excluded entirely (falls through to the estimate in
        # get_pricing_list) rather than published as "real".
        if p["qty"] < MIN_QTY_FOR_REAL_RATE:
            continue
        rates[p["offer_id"]] = {
            "commission_pct": round(p["commission"] / nominal_revenue * 100, 2) if nominal_revenue else 0,
            "logistics_per_unit": round(p["delivery"] / p["qty"], 2),
            # "item_fees" here is Ozon's acquiring fee specifically for this
            # cabinet (verified live: every ITEM-category accrual entry on
            # it carries the same type_id — no other item-level fee type has
            # shown up) — exposed under its real name for the Цены
            # breakdown rather than the generic "сборы" label.
            "acquiring_pct": round(p["item_fees"] / nominal_revenue * 100, 4) if nominal_revenue else 0,
            "bonus_per_unit": round(p["bonus"] / p["qty"], 2),
        }
    shop_avg_acquiring_pct = round(total_acquiring / total_nominal_revenue * 100, 4) if total_nominal_revenue else 0
    return rates, shop_avg_acquiring_pct


def _estimate_rate(commissions: dict, fulfillment: str):
    """Static fallback estimate for a product with no sales history yet
    (see _real_rates_by_offer, which is used instead whenever it's
    available). `fulfillment` is "fbo" or "fbs" — Ozon reports separate
    commission % and logistics fee fields per scheme, and they can differ
    substantially (FBO adds its own fulfillment/packaging fee, FBS its own
    first-mile fee), so which one applies depends entirely on how THIS
    product actually ships — picking the wrong one silently over- or
    understates logistics cost. Field names verified against Ozon's own
    commissions object (both fbo_* and fbs_* variants exist in parallel).

    Returns the components separately (trunk transit, last mile, first
    mile/fulfillment) rather than pre-summed — Ozon's own seller-facing
    tariff calculator shows these as separate line items ("Логистика",
    "Последняя миля", "Обработка отправления"/fulfillment), and Цены
    mirrors that breakdown instead of hiding it inside one "Логистика"
    number. Callers that only want the total still just sum the three."""
    prefix = fulfillment
    sales_pct = commissions.get(f"sales_percent_{prefix}") or 0
    trunk_min = commissions.get(f"{prefix}_direct_flow_trans_min_amount") or 0
    trunk_max = commissions.get(f"{prefix}_direct_flow_trans_max_amount") or 0
    # Trunk logistics varies by actual delivery distance/cluster — using the
    # midpoint of Ozon's own min/max range as a working estimate.
    trunk = round((trunk_min + trunk_max) / 2, 2)
    last_mile = round(commissions.get(f"{prefix}_deliv_to_customer_amount") or 0, 2)
    if fulfillment == "fbo":
        # FBO has no seller-paid first-mile leg (the seller isn't the one
        # shipping to the sorting center) — its equivalent up-front cost is
        # Ozon's warehouse fulfillment/packaging fee instead.
        first_mile = round(commissions.get("fbo_fulfillment_amount") or 0, 2)
    else:
        first_mile_min = commissions.get("fbs_first_mile_min_amount") or 0
        first_mile_max = commissions.get("fbs_first_mile_max_amount") or 0
        first_mile = round((first_mile_min + first_mile_max) / 2, 2)
    return sales_pct, trunk, last_mile, first_mile


def get_pricing_list(client, cabinet_id: int) -> list:
    """Merges live prices + per-product commission/logistics rate (real,
    where recent sales history is cached — see _real_rates_by_offer; a
    static per-price-bracket estimate otherwise) with stored cost prices —
    everything the repricing tool needs to recompute profit-per-unit
    client-side as the user edits a price, without a round-trip per
    keystroke."""
    prices = client.get_all_prices()
    attrs = client.get_all_attributes()
    names = {a.get("offer_id"): a.get("name", "") for a in attrs}
    cost_prices = cabinets.get_cost_prices(cabinet_id)
    stocks = client.get_all_stocks()
    settings = cabinets.get_cabinet_settings(cabinet_id)
    tax_pct = settings.get("tax_pct", 0)
    # Same target-margin setting ozon_price_monitor/ozon_promo_guard already
    # alert against — surfaced here too so Цены can show "маржа X% (цель
    # Y%)" and suggest the price needed to actually hit it, instead of only
    # the hardcoded 15%/0% break-even suggestion.
    min_margin_pct = settings.get("min_margin_pct", 0)
    real_rates, shop_avg_acquiring_pct = _real_rates_by_offer(cabinet_id)

    items = []
    for p in prices:
        offer_id = p.get("offer_id")
        price_info = p.get("price") or {}
        commissions = p.get("commissions") or {}
        stock = stocks.get(offer_id, {"fbo": 0, "fbs": 0})

        # Computed unconditionally (not just as the no-real-data fallback):
        # Ozon's own quoted reference rate for this exact offer's category
        # is an authoritative sanity check for the real-data-derived rate
        # below, whatever the actual distortion mechanism turns out to be.
        fulfillment = "fbo" if stock["fbo"] > stock["fbs"] else "fbs"
        est_sales_pct, est_trunk, est_last_mile, est_first_mile = _estimate_rate(commissions, fulfillment)
        est_logistics_estimate = round(est_trunk + est_last_mile + est_first_mile, 2)

        real = real_rates.get(offer_id)
        # Even after correcting for the bonus/nominal-price distortion
        # above, a real-data commission% can still land far from Ozon's own
        # quoted category rate (caught live 2026-09-13: a "Ведро" offer
        # showed 91.6% with real sales history, no plausible bonus
        # adjustment gets it close to a sane houseware-category rate) —
        # rather than chase every possible distortion mechanism, distrust
        # the whole real-data rate set for this offer whenever it strays
        # this far from the authoritative quoted rate and fall back to that
        # instead of guessing which part of "real" might still be right.
        if real and est_sales_pct and abs(real["commission_pct"] - est_sales_pct) > REAL_VS_QUOTED_SANITY_MARGIN_PCT:
            real = None

        if real:
            sales_pct = real["commission_pct"]
            logistics_estimate = real["logistics_per_unit"]
            # Real accrual data only ever reports total delivery cost per
            # posting, not broken into trunk/last-mile/first-mile — there's
            # nothing to split it by, so Цены shows one "Логистика" line for
            # these (rate_source "real"), same as before.
            logistics_trunk = logistics_last_mile = logistics_first_mile = None
            acquiring_pct = real["acquiring_pct"]
            bonus = real["bonus_per_unit"]
            rate_source = "real"
        else:
            # No sales history yet to derive real rates from (or the real
            # rate failed the sanity check above) — fall back to Ozon's
            # quoted reference rate for whichever scheme this product's
            # stock is actually sitting in (defaults to FBS when there's no
            # stock either way, since that's this app's most common setup,
            # but a client whose catalog is FBO gets FBO rates
            # automatically, no manual switch needed). Acquiring has no
            # equivalent "reference rate" field to estimate from — use the
            # cabinet-wide average instead of 0, a much better guess.
            sales_pct, logistics_estimate = est_sales_pct, est_logistics_estimate
            logistics_trunk, logistics_last_mile, logistics_first_mile = est_trunk, est_last_mile, est_first_mile
            acquiring_pct = shop_avg_acquiring_pct
            bonus = 0
            rate_source = "estimate"

        # Ozon returns price as a string ("2550.0000") — cast before doing
        # arithmetic with it.
        price = float(price_info.get("price") or 0)
        # Acquiring is a % of price, same as commission — computed here
        # against the item's current live price purely as a starting number
        # to render; Цены/Акции recompute it live as the user types a
        # different price (see computePriceRow in the frontend), the same
        # way commission already does.
        acquiring = round(price * acquiring_pct / 100, 2)

        items.append({
            "offer_id": offer_id,
            "product_id": p.get("product_id"),
            "name": names.get(offer_id) or offer_id,
            "price": price_info.get("price"),
            "old_price": price_info.get("old_price"),
            "min_price": price_info.get("min_price"),
            "cogs_unit": cost_prices.get(offer_id, 0),
            "commission_pct": sales_pct,
            "logistics_estimate": logistics_estimate,
            "logistics_trunk": logistics_trunk,
            "logistics_last_mile": logistics_last_mile,
            "logistics_first_mile": logistics_first_mile,
            "acquiring": acquiring,
            "acquiring_pct": acquiring_pct,
            "bonus": bonus,
            "rate_source": rate_source,
            "tax_pct": tax_pct,
            "min_margin_pct": min_margin_pct,
            "fbo_stock": stock["fbo"],
            "fbs_stock": stock["fbs"],
        })
    items.sort(key=lambda x: x["name"] or "")
    return items
