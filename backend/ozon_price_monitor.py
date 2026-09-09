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


def _current_profit(p):
    price = p.get("price") or p.get("min_price") or 0
    cogs = p.get("cogs_unit") or 0
    expense = price * ((p.get("commission_pct") or 0) / 100) + (p.get("logistics_estimate") or 0)
    return price - cogs - expense


def check_cabinet(cabinet: dict) -> list:
    """Computes current profit-per-unit for every product in this Ozon
    cabinet (same formula as the Цены tab) and diffs the set of loss-making
    offer_ids against the last check. Returns only the NEWLY negative ones —
    products already known to be losing money aren't re-reported every run."""
    creds = cabinet["credentials"]
    client = OzonClient(creds["client_id"], creds["api_key"])
    items = ozon_pricing.get_pricing_list(client, cabinet["id"])

    negative = []
    for p in items:
        profit = _current_profit(p)
        if profit < 0:
            negative.append({"offer_id": p["offer_id"], "name": p["name"], "profit": round(profit, 2)})

    state_file = _state_file(cabinet["id"])
    previous_ids = set(_load(state_file))
    current_ids = {n["offer_id"] for n in negative}
    _save(state_file, list(current_ids))

    return [n for n in negative if n["offer_id"] not in previous_ids]
