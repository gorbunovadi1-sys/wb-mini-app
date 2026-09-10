import logging

from . import cabinets, ozon_pricing
from .ozon_client import OzonClient

log = logging.getLogger("ozon_promo_guard")


def _profit_at_price(price, cogs_unit, commission_pct, logistics_estimate):
    expense = price * (commission_pct / 100) + logistics_estimate
    return price - cogs_unit - expense


def check_and_clean_cabinet(cabinet: dict) -> list:
    """Finds products currently in an Ozon promotion whose promo price would
    be a loss (needs a known cost price to judge — unknown cost price is
    treated as "can't tell", not flagged), and automatically removes them
    from that promotion via the Seller API. Returns what was removed, for
    notifying the owner after the fact."""
    creds = cabinet["credentials"]
    client = OzonClient(creds["client_id"], creds["api_key"])
    cabinet_id = cabinet["id"]

    pricing = ozon_pricing.get_pricing_list(client, cabinet_id)
    cost_prices = cabinets.get_cost_prices(cabinet_id)
    pricing_by_pid = {str(p["product_id"]): p for p in pricing if p.get("product_id") is not None}

    actions = client.get_actions()
    relevant = [a for a in actions if a.get("is_participating") or a.get("participating_products_count", 0) > 0]

    to_remove_by_action = {}  # action_id -> [(product_id, info), ...]
    for action in relevant:
        action_id = action["id"]
        try:
            products = client.get_action_products(action_id)
        except Exception:
            log.exception(f"Failed to fetch products for action {action_id}")
            continue
        for p in products:
            pid = str(p["id"])
            info = pricing_by_pid.get(pid)
            if not info:
                continue
            offer_id = info["offer_id"]
            if offer_id not in cost_prices:
                continue  # unknown cost price — can't judge, don't touch
            action_price = p.get("action_price") or 0
            if not action_price:
                continue
            profit = _profit_at_price(action_price, info["cogs_unit"], info["commission_pct"], info["logistics_estimate"])
            if profit < 0:
                to_remove_by_action.setdefault(action_id, []).append({
                    "product_id": p["id"],
                    "offer_id": offer_id,
                    "title": info["name"],
                    "action_title": action.get("title", str(action_id)),
                    "action_price": action_price,
                    "profit": round(profit, 2),
                })

    removed = []
    for action_id, items in to_remove_by_action.items():
        try:
            removed_ids = set(client.deactivate_products_from_action(action_id, [i["product_id"] for i in items]))
        except Exception:
            log.exception(f"Failed to deactivate products from action {action_id}")
            continue
        removed.extend(i for i in items if i["product_id"] in removed_ids)

    return removed
