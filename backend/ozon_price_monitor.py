import json
import logging
import os

from . import ozon_pricing
from .ozon_client import OzonClient

log = logging.getLogger("ozon_price_monitor")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def _state_file(cabinet_id):
    return os.path.join(DATA_DIR, f"ozon_price_alerts_{cabinet_id}.json")


def _load(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _current_profit_and_margin(p):
    price = p.get("price") or p.get("min_price") or 0
    cogs = p.get("cogs_unit") or 0
    expense = price * ((p.get("commission_pct") or 0) / 100) + (p.get("logistics_estimate") or 0)
    profit = price - cogs - expense
    margin_pct = (profit / price * 100) if price else 0
    return profit, margin_pct


def check_cabinet(cabinet: dict) -> list:
    """Computes current profit-per-unit for every product in this Ozon
    cabinet (same formula as the Цены tab) and diffs the set of "below the
    cabinet's minimum margin" offer_ids against the last check. Returns only
    the NEWLY-below-threshold ones — products already known to be under it
    aren't re-reported every run. Threshold defaults to 0% (i.e. any loss),
    but is configurable per cabinet via settings.min_margin_pct."""
    creds = cabinet["credentials"]
    client = OzonClient(creds["client_id"], creds["api_key"])
    items = ozon_pricing.get_pricing_list(client, cabinet["id"])
    min_margin_pct = cabinet.get("settings", {}).get("min_margin_pct", 0)

    below_threshold = []
    for p in items:
        profit, margin_pct = _current_profit_and_margin(p)
        if margin_pct < min_margin_pct:
            below_threshold.append({"offer_id": p["offer_id"], "name": p["name"], "profit": round(profit, 2), "margin_percent": round(margin_pct, 2)})

    state_file = _state_file(cabinet["id"])
    previous_ids = set(_load(state_file))
    current_ids = {n["offer_id"] for n in below_threshold}
    _save(state_file, list(current_ids))

    return [n for n in below_threshold if n["offer_id"] not in previous_ids]
