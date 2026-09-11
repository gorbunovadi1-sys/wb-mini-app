from . import cabinets, ozon_margin, ozon_sales_cache


def _real_rates_by_offer(cabinet_id: int) -> dict:
    """Real per-offer commission % and logistics-per-unit, derived from the
    same cached accrual data Дашборд/Детализация use — an actual weighted
    average of what this product really sold through recently, whatever mix
    of FBO/FBS that was. Far more accurate than the static estimate below,
    which only ever reads FBS-specific rate fields regardless of how the
    product actually ships. Returns {} if there's no cache yet for this
    cabinet — callers fall back to the estimate; this never makes a live
    Ozon call on its own, so it's safe to call on every pricing/promo load."""
    cached = ozon_sales_cache.get(cabinet_id)
    if not cached:
        return {}
    cpostings, caccrual, cnonitem, ccover_from, _is_stale = cached
    try:
        result = ozon_margin.build_margin_summary(
            client=None, cost_prices={}, days=60,
            cached_postings=cpostings, cached_accrual_by_date=caccrual,
            cached_non_item_by_date=cnonitem, cache_cover_from=ccover_from,
        )
    except Exception:
        return {}
    rates = {}
    for p in result.get("products", []):
        if not p.get("qty") or not p.get("revenue"):
            continue
        rates[p["offer_id"]] = {
            "commission_pct": round(p["commission"] / p["revenue"] * 100, 2),
            "logistics_per_unit": round(p["delivery"] / p["qty"], 2),
        }
    return rates


def _estimate_rate(commissions: dict, fulfillment: str):
    """Static fallback estimate for a product with no sales history yet
    (see _real_rates_by_offer, which is used instead whenever it's
    available). `fulfillment` is "fbo" or "fbs" — Ozon reports separate
    commission % and logistics fee fields per scheme, and they can differ
    substantially (FBO adds its own fulfillment/packaging fee, FBS its own
    first-mile fee), so which one applies depends entirely on how THIS
    product actually ships — picking the wrong one silently over- or
    understates logistics cost. Field names verified against Ozon's own
    commissions object (both fbo_* and fbs_* variants exist in parallel)."""
    prefix = fulfillment
    sales_pct = commissions.get(f"sales_percent_{prefix}") or 0
    trunk_min = commissions.get(f"{prefix}_direct_flow_trans_min_amount") or 0
    trunk_max = commissions.get(f"{prefix}_direct_flow_trans_max_amount") or 0
    last_mile = commissions.get(f"{prefix}_deliv_to_customer_amount") or 0
    # Trunk logistics varies by actual delivery distance/cluster — using the
    # midpoint of Ozon's own min/max range as a working estimate.
    logistics = (trunk_min + trunk_max) / 2 + last_mile
    if fulfillment == "fbo":
        logistics += commissions.get("fbo_fulfillment_amount") or 0
    else:
        first_mile_min = commissions.get("fbs_first_mile_min_amount") or 0
        first_mile_max = commissions.get("fbs_first_mile_max_amount") or 0
        logistics += (first_mile_min + first_mile_max) / 2
    return sales_pct, round(logistics, 2)


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
    tax_pct = cabinets.get_cabinet_settings(cabinet_id).get("tax_pct", 0)
    real_rates = _real_rates_by_offer(cabinet_id)

    items = []
    for p in prices:
        offer_id = p.get("offer_id")
        price_info = p.get("price") or {}
        commissions = p.get("commissions") or {}
        stock = stocks.get(offer_id, {"fbo": 0, "fbs": 0})

        real = real_rates.get(offer_id)
        if real:
            sales_pct = real["commission_pct"]
            logistics_estimate = real["logistics_per_unit"]
            rate_source = "real"
        else:
            # No sales history yet to derive real rates from — fall back to
            # Ozon's quoted reference rates for whichever scheme this
            # product's stock is actually sitting in (defaults to FBS when
            # there's no stock either way, since that's this app's most
            # common setup, but a client whose catalog is FBO gets FBO rates
            # automatically, no manual switch needed).
            fulfillment = "fbo" if stock["fbo"] > stock["fbs"] else "fbs"
            sales_pct, logistics_estimate = _estimate_rate(commissions, fulfillment)
            rate_source = "estimate"

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
            "rate_source": rate_source,
            "tax_pct": tax_pct,
            "fbo_stock": stock["fbo"],
            "fbs_stock": stock["fbs"],
        })
    items.sort(key=lambda x: x["name"] or "")
    return items
