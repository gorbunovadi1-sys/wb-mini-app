import json
import os

COST_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "cost_prices.json")


def load_cost_prices():
    """Maps nmId (str) -> cost price in RUB. Empty/missing entries default to 0."""
    if not os.path.exists(COST_FILE):
        return {}
    with open(COST_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_cost_prices(prices: dict):
    os.makedirs(os.path.dirname(COST_FILE), exist_ok=True)
    with open(COST_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)
