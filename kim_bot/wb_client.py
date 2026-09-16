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
FEEDBACKS_BASE = "https://feedbacks-api.wildberries.ru"

# Marketplace-API orders not yet confirmed by the seller sit in this
# supplierStatus — the moment it moves on, the order has left "awaiting
# assembly" (see _parse_order/is_pending_assembly below).
PENDING_ASSEMBLY_STATUS = "new"
SUPPLIER_CANCELLED_STATUSES = {"cancel", "declined_by_client"}
# Verified live 2026-09-15: an order the customer cancels/declines BEFORE
# the seller ever confirms it never leaves supplierStatus "new" — WB only
# reflects that cancellation in wbStatus. Without checking this too, such
# orders looked "pending assembly" forever and kept re-triggering the SLA
# alert for weeks (real incident — see project memory).
WB_CANCELLED_STATUSES = {"canceled", "canceled_by_client", "declined_by_client", "defect"}


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

    def _get_orders_window(self, start_ts: int, limit=1000) -> list:
        """One /api/v3/orders cursor-paginated pull from a single dateFrom
        timestamp. Verified live 2026-09-15: this endpoint silently caps
        results at ~29 days of span from dateFrom (not "dateFrom to now") —
        a dateFrom 30+ days back came back truncated with no error, missing
        everything past day 29, which meant the freshest orders (and their
        current status) silently vanished from every sync — see
        get_orders_since, which chunks around this cap."""
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

    def get_orders_since(self, date_from_iso: str, limit=1000) -> list:
        """Returns every order (any status) created since `date_from_iso`
        ('YYYY-MM-DD') — this is both the backfill and the ongoing sync
        source; combine with get_order_statuses for actual status, which
        this endpoint doesn't carry. Chunks into <=20-day windows to stay
        well clear of WB's ~29-day-span cap on a single dateFrom (see
        _get_orders_window) — otherwise a date_from more than ~29 days back
        silently drops everything past that span, including today."""
        start = datetime.datetime.strptime(date_from_iso, "%Y-%m-%d")
        today = datetime.datetime.utcnow()
        step = datetime.timedelta(days=20)

        by_id = {}
        window_start = start
        while window_start <= today:
            for o in self._get_orders_window(int(window_start.timestamp()), limit=limit):
                by_id[o["id"]] = o
            window_start += step
        return list(by_id.values())

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

    def get_unanswered_reviews(self, take: int = 50) -> list:
        """GET /api/v1/feedbacks?isAnswered=false — verified live 2026-09-15
        against a real key (worked without a separate "Вопросы и отзывы"
        token category — access already included). Returns raw feedback
        objects: id, text, productValuation (1-5), productDetails.productName/
        supplierArticle, userName, createdDate."""
        skip, out = 0, []
        while True:
            data = self._request(
                "GET", f"{FEEDBACKS_BASE}/api/v1/feedbacks",
                params={"isAnswered": "false", "take": take, "skip": skip},
            )
            batch = data.get("data", {}).get("feedbacks", []) or []
            out.extend(batch)
            skip += take
            if len(batch) < take:
                break
        return out

    def post_review_answer(self, review_id: str, text: str):
        """PATCH /api/v1/feedbacks — posts (or edits) the seller's public
        reply to a review. NOT yet verified live — this is a real, public,
        irreversible-ish action, so it was deliberately never test-fired;
        first real call happens only when she approves a draft in the bot."""
        r = requests.patch(
            f"{FEEDBACKS_BASE}/api/v1/feedbacks",
            headers=self.headers, json={"id": review_id, "text": text}, timeout=30,
        )
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


def is_cancelled(supplier_status: str, wb_status: str = None) -> bool:
    return supplier_status in SUPPLIER_CANCELLED_STATUSES or wb_status in WB_CANCELLED_STATUSES


def is_pending_assembly(supplier_status: str, wb_status: str = None) -> bool:
    return supplier_status == PENDING_ASSEMBLY_STATUS and not is_cancelled(supplier_status, wb_status)
