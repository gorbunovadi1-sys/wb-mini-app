from . import cabinets


def get_price_list(client, cabinet_id: int) -> list:
    """Current WB prices merged with stored cost prices — read-only (see
    WBClient.get_all_goods_prices). Margin shown here is a rough estimate
    (customer price minus cost price, no WB commission/logistics deducted)
    since those aren't available for a whole catalog without an actual
    sale — labelled accordingly on the frontend, not presented as exact
    profit the way the Ozon Цены tab can be."""
    goods = client.get_all_goods_prices()
    cost_prices = cabinets.get_cost_prices(cabinet_id)

    items = []
    for g in goods:
        nm_id = g.get("nmID")
        sizes = g.get("sizes") or []
        size = sizes[0] if sizes else {}
        cogs_unit = cost_prices.get(str(nm_id), 0)
        discounted_price = size.get("discountedPrice") or 0
        items.append({
            "nm_id": nm_id,
            "vendor_code": g.get("vendorCode"),
            "price": size.get("price"),
            "discount": g.get("discount"),
            "discounted_price": discounted_price,
            "club_discounted_price": size.get("clubDiscountedPrice"),
            "cogs_unit": cogs_unit,
            "rough_margin": round(discounted_price - cogs_unit, 2) if discounted_price else None,
        })
    items.sort(key=lambda x: x["vendor_code"] or "")
    return items
