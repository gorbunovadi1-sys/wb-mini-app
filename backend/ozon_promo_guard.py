import logging

from . import cabinets, ozon_pricing, ozon_promotions
from .ozon_client import OzonClient

log = logging.getLogger("ozon_promo_guard")


def _profit_and_margin_at_price(price, cogs_unit, commission_pct, logistics_estimate, tax_pct=0):
    expense = price * (commission_pct / 100) + logistics_estimate
    tax = price * (tax_pct / 100)
    profit = price - cogs_unit - expense - tax
    margin_pct = (profit / price * 100) if price else 0
    return profit, margin_pct


def check_and_clean_cabinet(cabinet: dict) -> dict:
    """Finds products currently in an Ozon promotion whose promo price falls
    below the cabinet's minimum-margin setting (needs a known cost price to
    judge — unknown cost price is treated as "can't tell", not flagged).

    With settings.promo_auto_remove on (default): removes them from the
    promotion via the Seller API and returns them under "removed".

    With it off: doesn't touch anything, but still tells the owner when
    Ozon has auto-added their products to a *new* promotion (via
    ozon_promotions' own join/leave snapshot) so they can go check
    manually — returned under "joined"."""
    cabinet_id = cabinet["id"]
    auto_remove = cabinet.get("settings", {}).get("promo_auto_remove", True)

    if not auto_remove:
        result = ozon_promotions.refresh_promotions(
            client=_build_client(cabinet), cabinet_id=str(cabinet_id),
        )
        joined = [
            e for e in result["recent_events"]
            if e["at"] == result["generated_at"] and e["event"] == "joined"
        ]
        return {"removed": [], "joined": joined}

    client = _build_client(cabinet)
    min_margin_pct = cabinet.get("settings", {}).get("min_margin_pct", 0)

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
            profit, margin_pct = _profit_and_margin_at_price(
                action_price, info["cogs_unit"], info["commission_pct"], info["logistics_estimate"], info.get("tax_pct", 0),
            )
            if margin_pct < min_margin_pct:
                to_remove_by_action.setdefault(action_id, []).append({
                    "product_id": p["id"],
                    "offer_id": offer_id,
                    "title": info["name"],
                    "action_title": action.get("title", str(action_id)),
                    "action_price": action_price,
                    "profit": round(profit, 2),
                    "margin_percent": round(margin_pct, 2),
                })

    removed = []
    for action_id, items in to_remove_by_action.items():
        try:
            removed_ids = set(client.deactivate_products_from_action(action_id, [i["product_id"] for i in items]))
        except Exception:
            log.exception(f"Failed to deactivate products from action {action_id}")
            continue
        removed.extend(i for i in items if i["product_id"] in removed_ids)

    return {"removed": removed, "joined": []}


def _build_client(cabinet: dict) -> OzonClient:
    creds = cabinet["credentials"]
    return OzonClient(creds["client_id"], creds["api_key"])
