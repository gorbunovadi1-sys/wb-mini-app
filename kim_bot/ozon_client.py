"""Ozon Seller API client for this bot's two needs: FBS posting/assembly
status (8h SLA alert) and returns (stock balance). get_fbs_postings is
copied verbatim from backend/ozon_client.py (proven in prod); get_returns is
new and NOT yet verified against a live key — Ozon has been retiring older
/v2/returns/company/* endpoints (seen autumn-2026 deprecation notices), so
this targets the current /v1/returns/list endpoint, isolated in
parse_return below so a wrong field-name guess is a one-function fix."""
import re
import time
import logging
import requests

log = logging.getLogger("kim_bot.ozon_client")

BASE = "https://api-seller.ozon.ru"

# A posting sits here until the seller packs it — this is Ozon's equivalent
# of "not yet sent to assembly" for the SLA check.
PENDING_ASSEMBLY_STATUS = "awaiting_packaging"
CANCELLED_STATUSES = {"cancelled", "cancelled_by_admin"}


def _sanitize(raw: str, allowed: str) -> str:
    cleaned = re.sub(f"[^{allowed}]", "", raw)
    if cleaned != raw.strip():
        log.warning(f"Ozon credential contained unexpected characters and was sanitized (raw len={len(raw)}, cleaned len={len(cleaned)})")
    return cleaned


class OzonClient:
    def __init__(self, client_id: str, api_key: str, max_retries: int = 5):
        self.client_id = _sanitize(client_id, "0-9")
        self.api_key = _sanitize(api_key, "A-Za-z0-9-")
        self.headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }
        self.max_retries = max_retries

    def _retry_delay(self, r, attempt: int) -> int:
        escalating = min(5 * (attempt + 1), 30)
        try:
            header_value = int(r.headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            header_value = 0
        return max(escalating, header_value)

    def _post(self, path, payload, timeout=60):
        r = None
        for attempt in range(self.max_retries):
            r = requests.post(f"{BASE}{path}", headers=self.headers, json=payload, timeout=timeout)
            if r.status_code != 429:
                r.raise_for_status()
                return r.json()
            retry_after = self._retry_delay(r, attempt)
            log.warning(f"429 from {path}, retrying in {retry_after}s (attempt {attempt + 1}/{self.max_retries})")
            time.sleep(retry_after)
        r.raise_for_status()
        return r.json()

    def check_credentials(self):
        self._post("/v3/product/list", {"filter": {}, "last_id": "", "limit": 1})

    def get_fbs_postings(self, date_from, date_to):
        """Paginates /v3/posting/fbs/list for [date_from, date_to) (ISO datetimes)."""
        postings = []
        offset = 0
        while True:
            data = self._post("/v3/posting/fbs/list", {
                "filter": {"since": date_from, "to": date_to},
                "limit": 1000, "offset": offset,
                "with": {"financial_data": False},
            })["result"]
            batch = data["postings"]
            postings.extend(batch)
            offset += len(batch)
            if not data.get("has_next") or not batch:
                break
        return postings

    def get_returns(self, date_from_iso: str) -> list:
        """POST /v1/returns/list — FBS+FBO returns, paginated by last_id.
        Filtered client-side to FBS since only that stock lives at her
        fulfillment warehouse."""
        returns = []
        last_id = 0
        while True:
            data = self._post("/v1/returns/list", {
                "filter": {"logistic_return_moment_time": {"time_from": date_from_iso}},
                "limit": 500, "last_id": last_id,
            })
            batch = data.get("returns", [])
            returns.extend(batch)
            if len(batch) < 500:
                break
            last_id = batch[-1].get("id", last_id)
        return returns


def parse_posting(posting: dict) -> dict:
    """Normalizes one Ozon FBS posting into a list of per-product dicts:
    {order_id, article, qty, created_at, status}. One posting can carry
    multiple products/quantities, unlike a WB order which is single-unit."""
    created_at = posting.get("in_process_at") or posting.get("created_at")
    status = posting.get("status")
    rows = []
    for p in posting.get("products", []) or [{}]:
        rows.append({
            "order_id": f"{posting.get('posting_number')}:{p.get('offer_id', '')}",
            "article": p.get("offer_id"),
            "qty": int(p.get("quantity") or 1),
            "created_at": created_at,
            "status": status,
        })
    return rows


def parse_return(ret: dict) -> dict:
    """Verified live 2026-09-15: /v1/returns/list mixes Fbs and Fbo returns
    together (schema field) — only Fbs belongs to her fulfillment warehouse
    stock, Fbo lives at Ozon's own warehouse and must be filtered by the
    caller. The date isn't top-level either — it's logistic.return_date."""
    logistic = ret.get("logistic") or {}
    return {
        "return_id": str(ret.get("id") or ret.get("return_id") or ""),
        "schema": ret.get("schema"),
        "article": (ret.get("product") or {}).get("offer_id"),
        "qty": int((ret.get("product") or {}).get("quantity") or 1),
        "created_at": logistic.get("return_date") or logistic.get("final_moment"),
    }


def is_pending_assembly(status: str) -> bool:
    return status == PENDING_ASSEMBLY_STATUS


def is_cancelled(status: str) -> bool:
    return status in CANCELLED_STATUSES
