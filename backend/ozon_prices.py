from . import ozon_client


def get_price_list(client=None):
    """Merges /v5/product/info/prices with /v4/product/info/attributes (for names)
    into one flat list for the price-management view."""
    client = client or ozon_client.default_client
    prices = client.get_all_prices()
    attrs = client.get_all_attributes()
    names = {a.get("offer_id"): a.get("name", "") for a in attrs}

    items = []
    for p in prices:
        offer_id = p.get("offer_id")
        price_info = p.get("price") or {}
        indexes = p.get("price_indexes") or {}
        items.append({
            "offer_id": offer_id,
            "product_id": p.get("product_id"),
            "name": names.get(offer_id) or offer_id,
            "price": price_info.get("price"),
            "old_price": price_info.get("old_price"),
            "min_price": price_info.get("min_price"),
            "price_index": indexes.get("color_index"),
        })
    items.sort(key=lambda x: x["name"] or "")
    return items


def update_prices(updates, client=None):
    """updates: [{offer_id, price, old_price?, min_price?}]."""
    client = client or ozon_client.default_client
    return client.update_prices(updates)
