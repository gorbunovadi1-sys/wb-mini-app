def update_fbs_stock(client, updates: list) -> list:
    """updates: [{"offer_id": str, "stock": int}]. Resolves the seller's own
    FBS warehouse (most sellers have exactly one) and pushes the new
    quantities to it. Raises ValueError if no FBS warehouse is found — an
    rFBS-only or FBO-only seller has nothing to push stock to here."""
    warehouses = client.get_warehouses()
    fbs_warehouses = [w for w in warehouses if not w.get("is_rfbs") and not w.get("is_karting")]
    if not fbs_warehouses:
        raise ValueError("У кабинета не найден склад FBS — нечего обновлять")
    warehouse_id = fbs_warehouses[0]["warehouse_id"]

    items = [
        {"offer_id": u["offer_id"], "stock": int(u["stock"]), "warehouse_id": warehouse_id}
        for u in updates
    ]
    return client.update_stocks(items)
