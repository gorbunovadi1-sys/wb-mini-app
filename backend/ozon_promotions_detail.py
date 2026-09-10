from . import cabinets, ozon_pricing


def list_actions(client) -> list:
    """All Ozon promotions this seller can see, with participation counts —
    for the Акции tab's top-level list."""
    actions = client.get_actions()
    return [
        {
            "id": a["id"],
            "title": a.get("title", str(a["id"])),
            "date_start": a.get("date_start"),
            "date_end": a.get("date_end"),
            "is_participating": bool(a.get("is_participating")),
            "participating_products_count": a.get("participating_products_count", 0),
        }
        for a in actions
    ]


def _profit(price, cogs_unit, commission_pct, logistics_estimate, tax_pct=0):
    if not price:
        return None
    expense = price * (commission_pct / 100) + logistics_estimate
    tax = price * (tax_pct / 100)
    return round(price - cogs_unit - expense - tax, 2)


def get_action_detail(client, cabinet_id: int, action_id: int) -> dict:
    """Everything the Акции drill-down needs: products currently in the
    promotion (with their promo price and the profit it yields) and eligible
    candidates not yet in it (with a suggested price range and the profit
    each bound would yield), so the price can be chosen with profit visible
    before adding — not just its Ozon-side elastic bounds."""
    pricing = ozon_pricing.get_pricing_list(client, cabinet_id)
    pricing_by_pid = {str(p["product_id"]): p for p in pricing if p.get("product_id") is not None}
    cost_prices = cabinets.get_cost_prices(cabinet_id)

    def _enrich(item, price_field):
        pid = str(item["id"])
        info = pricing_by_pid.get(pid)
        offer_id = info.get("offer_id", "") if info else ""
        price = item.get(price_field) or 0
        cogs_unit = info.get("cogs_unit", 0) if info else 0
        commission_pct = info.get("commission_pct", 0) if info else 0
        logistics_estimate = info.get("logistics_estimate", 0) if info else 0
        tax_pct = info.get("tax_pct", 0) if info else 0
        return {
            "product_id": item["id"],
            "offer_id": offer_id,
            "name": (info.get("name") if info else None) or str(item["id"]),
            "price": item.get("price"),
            "action_price": item.get("action_price"),
            "max_action_price": item.get("max_action_price"),
            "stock": item.get("stock", 0),
            "cogs_unit": cogs_unit,
            "commission_pct": commission_pct,
            "logistics_estimate": logistics_estimate,
            "tax_pct": tax_pct,
            "has_cost_price": offer_id in cost_prices,
            # None (not 0) when we couldn't match this product to our price/
            # commission data at all — showing a profit computed with fake
            # zero commission/logistics would be actively misleading.
            "profit": _profit(price, cogs_unit, commission_pct, logistics_estimate, tax_pct) if info else None,
        }

    in_action = [_enrich(p, "action_price") for p in client.get_action_products(action_id)]
    candidates = [_enrich(p, "max_action_price") for p in client.get_action_candidates(action_id)]
    in_action.sort(key=lambda x: x["name"])
    candidates.sort(key=lambda x: x["name"])
    return {"in_action": in_action, "candidates": candidates}


def add_products(client, action_id: int, items: list) -> dict:
    """items: [{"product_id": int, "action_price": number, "stock": int}]."""
    products = [
        {"product_id": i["product_id"], "action_price": i["action_price"], "stock": i.get("stock", 0)}
        for i in items
    ]
    added, rejected = client.activate_products_in_action(action_id, products)
    return {"added": added, "rejected": rejected}


def remove_products(client, action_id: int, product_ids: list) -> dict:
    removed = client.deactivate_products_from_action(action_id, product_ids)
    return {"removed": removed}
