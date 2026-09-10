import time


def get_fbo_stock(client, max_wait: int = 90, poll_interval: int = 5) -> dict:
    """nm_id -> FBO stock quantity ("Всего находится на складах"), via WB's
    async warehouse_remains report. FBS stock isn't included here — that's a
    separate WB API keyed by the seller's own warehouse ids, not yet wired up."""
    task_id = client.create_warehouse_remains_task()
    deadline = time.time() + max_wait
    data = None
    while time.time() < deadline:
        data = client.get_warehouse_remains_result(task_id)
        if data is not None:
            break
        time.sleep(poll_interval)
    if data is None:
        raise TimeoutError("WB warehouse remains report did not finish in time — try again shortly")

    stock = {}
    for row in data:
        nm_id = row.get("nmId")
        total = next(
            (w["quantity"] for w in row.get("warehouses", []) if w.get("warehouseName") == "Всего находится на складах"),
            0,
        )
        stock[nm_id] = total
    return stock
