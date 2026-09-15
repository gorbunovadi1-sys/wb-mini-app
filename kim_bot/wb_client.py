"""WB API client for this bot's two needs: FBS order/assembly status (for the
8h SLA alert) and returns (for the stock balance). Copies the retry/sanitize
pattern from backend/wb_client.py (proven in prod) rather than importing it,
so this bot has zero runtime dependency on the backend/ package.

Verified live against a real KIM_WB_API_KEY (2026-09-15, ИП Ким / Chess
Masters): order objects really do carry the seller's own article in a field
called "article" (e.g. "2656-24"), and neither /orders/new nor /orders
(dateFrom) return a status field at all — /orders/status is required for
that, for every order regardless of source. dateFrom on /orders must be a
Unix timestamp (an ISO date string 400s)."""
import datetime
import re
import time
import logging
import requests

log = logging.getLogger("kim_bot.wb_client")

MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"
STATS_BASE = "https://statistics-api.wildberries.ru"
COMMON_BASE = "https://common-api.wildberries.ru"
ANALYTICS_BASE = "https://seller-analytics-api.wildberries.ru"

# Marketplace-API orders not yet confirmed by the seller sit in this
# supplierStatus — the moment it moves on, the order has left "awaiting
# assembly" (see _parse_order/is_pending_assembly below).
PENDING_ASSEMBLY_STATUS = "new"
CANCELLED_STATUSES = {"cancel", "declined_by_client"}


def _sanitize_key(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", raw)
    if cleaned != raw.strip():
        log.warning(f"WB_API_KEY contained unexpected characters and was sanitized (raw len={len(raw)}, cleaned len={len(cleaned)})")
    return cleaned


class WBClient:
    def __init__(self, api_key: str):
        self.api_key = _sanitize_key(api_key)
        self.headers = {"Authorization": self.api_key, "Content-Type": "application/json"}

    def _request(self, method, url, max_retries=5, **kwargs):
        r = None
        for attempt in range(max_retries):
            r = requests.request(method, url, headers=self.headers, timeout=kwargs.pop("timeout", 30), **kwargs)
            if r.status_code != 429:
                r.raise_for_status()
                return r.json()
            retry_after = int(r.headers.get("Retry-After", 20))
            log.warning(f"429 from {url}, retrying in {retry_after}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_after)
        r.raise_for_status()
        return r.json()

    def get_seller_info(self):
        return self._request("GET", f"{COMMON_BASE}/api/v1/seller-info")

    def get_orders_since(self, date_from_iso: str, limit=1000) -> list:
        """GET /api/v3/orders, cursor-paginated via `next`. `date_from_iso`:
        'YYYY-MM-DD'. Returns every order (any status) created since then —
        this is both the backfill and the ongoing sync source; combine with
        get_order_statuses for actual status, which this endpoint doesn't carry."""
        start_ts = int(datetime.datetime.strptime(date_from_iso, "%Y-%m-%d").timestamp())
        orders = []
        next_cursor = 0
        while True:
            params = {"limit": limit, "next": next_cursor, "dateFrom": start_ts}
            data = self._request("GET", f"{MARKETPLACE_BASE}/api/v3/orders", params=params)
            batch = data.get("orders", [])
            orders.extend(batch)
            next_cursor = data.get("next", 0)
            if not batch or len(batch) < limit:
                break
        return orders

    def get_order_statuses(self, order_ids: list) -> dict:
        """POST /api/v3/orders/status, batched 1000/call. Returns
        {order_id: {"supplierStatus": ..., "wbStatus": ...}}."""
        result = {}
        for i in range(0, len(order_ids), 1000):
            chunk = order_ids[i:i + 1000]
            data = self._request("POST", f"{MARKETPLACE_BASE}/api/v3/orders/status", json={"orders": chunk})
            for o in data.get("orders", []):
                result[o["id"]] = {"supplierStatus": o.get("supplierStatus"), "wbStatus": o.get("wbStatus")}
        return result

    def create_warehouse_remains_task(self):
        """Starts WB's async "остатки на складах" (FBO stock) report —
        grouped by seller article/nmID/barcode. Ported from backend/wb_client.py
        (proven in prod); not part of this bot's regular polling, only used
        for one-off analysis (e.g. a China reorder recommendation)."""
        r = requests.get(
            f"{ANALYTICS_BASE}/api/v1/warehouse_remains",
            headers=self.headers,
            params={"groupBySa": "true", "groupByNm": "true", "groupByBarcode": "true"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["data"]["taskId"]

    def get_warehouse_remains_result(self, task_id):
        """Returns report rows once ready, or None while WB is still
        generating it (429 covers both "still working" and real throttling)."""
        r = requests.get(f"{ANALYTICS_BASE}/api/v1/warehouse_remains/tasks/{task_id}/download", headers=self.headers, timeout=30)
        if r.status_code == 429:
            return None
        r.raise_for_status()
        return r.json()

    def get_sales_and_returns(self, date_from_iso: str, flag: int = 0) -> list:
        """GET /api/v1/supplier/sales — individual sale/return records.
        saleID starting with "R" is a return (WB's long-standing convention);
        supplierArticle is the seller's own article — see returns.py."""
        return self._request(
            "GET", f"{STATS_BASE}/api/v1/supplier/sales",
            params={"dateFrom": date_from_iso, "flag": flag}, timeout=60,
        )


def parse_order(order: dict) -> dict:
    """Normalizes one WB Marketplace-API order object into
    {order_id, article, qty, created_at}. Neither /orders/new nor /orders
    carry a status field (verified live) — callers get real status from
    get_order_statuses separately."""
    article = order.get("article") or order.get("supplierArticle") or str(order.get("nmId") or "")
    return {
        "order_id": str(order["id"]),
        "article": article,
        "qty": 1,  # WB FBS orders are per-unit (one order = one item); WB does not send a quantity field here.
        "created_at": order.get("createdAt"),
    }


def is_pending_assembly(supplier_status: str) -> bool:
    return supplier_status == PENDING_ASSEMBLY_STATUS


def is_cancelled(supplier_status: str) -> bool:
    return supplier_status in CANCELLED_STATUSES
