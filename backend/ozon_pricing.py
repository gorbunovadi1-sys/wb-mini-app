from . import cabinets


def get_pricing_list(client, cabinet_id: int) -> list:
    """Merges live prices + per-product commission/logistics rate info (static,
    not tied to a specific sale) with stored cost prices — everything the
    repricing tool needs to recompute profit-per-unit client-side as the user
    edits a price, without a round-trip per keystroke."""
    prices = client.get_all_prices()
    attrs = client.get_all_attributes()
    names = {a.get("offer_id"): a.get("name", "") for a in attrs}
    cost_prices = cabinets.get_cost_prices(cabinet_id)
    stocks = client.get_all_stocks()
    tax_pct = cabinets.get_cabinet_settings(cabinet_id).get("tax_pct", 0)

    items = []
    for p in prices:
        offer_id = p.get("offer_id")
        price_info = p.get("price") or {}
        commissions = p.get("commissions") or {}

        sales_pct = commissions.get("sales_percent_fbs") or commissions.get("sales_percent_fbo") or 0
        logistics_min = commissions.get("fbs_direct_flow_trans_min_amount") or 0
        logistics_max = commissions.get("fbs_direct_flow_trans_max_amount") or 0
        last_mile = commissions.get("fbs_deliv_to_customer_amount") or 0
        # Trunk logistics varies by actual delivery distance/cluster — using
        # the midpoint of Ozon's own min/max range as a working estimate.
        logistics_estimate = round((logistics_min + logistics_max) / 2 + last_mile, 2)

        stock = stocks.get(offer_id, {"fbo": 0, "fbs": 0})
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
            "tax_pct": tax_pct,
            "fbo_stock": stock["fbo"],
            "fbs_stock": stock["fbs"],
        })
    items.sort(key=lambda x: x["name"] or "")
    return items
