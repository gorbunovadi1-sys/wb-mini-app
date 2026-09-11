import os
import re
import time
import logging
import requests

log = logging.getLogger("ozon_client")

BASE = "https://api-seller.ozon.ru"


def _sanitize(raw: str, allowed: str) -> str:
    """Ozon's Client-Id (digits) and Api-Key (UUID) are both narrow, predictable
    formats — strips anything else, guarding against the same class of corrupted
    Railway env-var value that previously broke WB_API_KEY (see wb_client.py)."""
    cleaned = re.sub(f"[^{allowed}]", "", raw)
    if cleaned != raw.strip():
        log.warning(f"Ozon credential contained unexpected characters and was sanitized (raw len={len(raw)}, cleaned len={len(cleaned)})")
    return cleaned


class OzonClient:
    """One seller's Ozon Seller API credentials, bound to every request this
    instance makes — lets different cabinets (different Telegram users) hit the
    same endpoints with their own Client-Id/Api-Key at the same time."""

    def __init__(self, client_id: str, api_key: str, max_retries: int = 8):
        self.client_id = _sanitize(client_id, "0-9")
        self.api_key = _sanitize(api_key, "A-Za-z0-9-")
        self.headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }
        # A background job (cache refresh, monitoring) has nothing waiting on
        # an HTTP connection, so it can afford the full patient schedule. A
        # request made live inside a route DOES have something waiting — the
        # platform's own reverse-proxy timeout — and the full 8-attempt
        # schedule (up to ~165s on one call alone) blows past that and comes
        # back as a bare 502 with no error message at all, worse than a fast,
        # clear failure. Callers on the interactive path should pass a lower
        # max_retries (see ai_engine_app._build_client).
        self.max_retries = max_retries

    def _retry_delay(self, r, attempt: int) -> int:
        """Ozon's own Retry-After under sustained rate-limiting has been
        observed to repeat a lowball value (e.g. "1") on every single retry —
        honoring it verbatim meant our own escalating backoff never actually
        ran (attempt N always waited the same ~1s Ozon claimed was enough),
        so 8 retries burned through in ~8s and still failed. Take whichever
        is longer: Ozon's stated value, or our own escalating schedule."""
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

    def _get(self, path, timeout=30):
        r = None
        for attempt in range(self.max_retries):
            r = requests.get(f"{BASE}{path}", headers=self.headers, timeout=timeout)
            if r.status_code != 429:
                r.raise_for_status()
                return r.json()
            retry_after = self._retry_delay(r, attempt)
            log.warning(f"429 from {path}, retrying in {retry_after}s (attempt {attempt + 1}/{self.max_retries})")
            time.sleep(retry_after)
        r.raise_for_status()
        return r.json()

    def check_credentials(self):
        """Cheap call used to validate a Client-Id/Api-Key pair during cabinet
        onboarding. Raises requests.HTTPError (401/403) if the credentials are wrong."""
        self._post("/v3/product/list", {"filter": {}, "last_id": "", "limit": 1})

    def get_all_products(self):
        """Paginates /v3/product/list. Returns [{product_id, offer_id, sku, archived}]."""
        items = []
        last_id = ""
        while True:
            data = self._post("/v3/product/list", {"filter": {}, "last_id": last_id, "limit": 1000})["result"]
            items.extend(data["items"])
            last_id = data.get("last_id", "")
            if not last_id or len(data["items"]) < 1000:
                break
        return items

    def get_all_prices(self):
        """Paginates /v5/product/info/prices. Returns raw items keyed by offer_id."""
        items = []
        cursor = ""
        while True:
            data = self._post("/v5/product/info/prices", {"filter": {}, "limit": 1000, "cursor": cursor})
            items.extend(data["items"])
            cursor = data.get("cursor", "")
            if not cursor or len(data["items"]) < 1000:
                break
        return items

    def update_prices(self, price_updates):
        """price_updates: [{offer_id, price, old_price?, min_price?, currency_code?}].
        Ozon caps this at 1000 items per call and requires 1-1000 items (no empty calls)."""
        results = []
        for i in range(0, len(price_updates), 1000):
            chunk = price_updates[i:i + 1000]
            payload = {
                "prices": [
                    {
                        "offer_id": p["offer_id"],
                        "price": str(p["price"]),
                        **({"old_price": str(p["old_price"])} if p.get("old_price") is not None else {}),
                        **({"min_price": str(p["min_price"])} if p.get("min_price") is not None else {}),
                        "currency_code": p.get("currency_code", "RUB"),
                    }
                    for p in chunk
                ]
            }
            data = self._post("/v1/product/import/prices", payload)
            results.extend(data.get("result", []))
        return results

    def get_all_attributes(self):
        """Paginates /v4/product/info/attributes. Returns items with name/height/width/depth/
        dimension_unit/weight/weight_unit per product (offer_id keyed by caller)."""
        items = []
        last_id = ""
        while True:
            data = self._post("/v4/product/info/attributes", {"filter": {}, "limit": 1000, "last_id": last_id})
            items.extend(data["result"])
            last_id = data.get("last_id", "")
            if not last_id or len(data["result"]) < 1000:
                break
        return items

    def get_all_stocks(self):
        """Paginates /v4/product/info/stocks (cursor-based) — FBO and FBS
        present/reserved stock per product. Returns {offer_id: {"fbo": n, "fbs": n}}."""
        stocks = {}
        cursor = ""
        while True:
            data = self._post("/v4/product/info/stocks", {"filter": {}, "limit": 1000, "cursor": cursor})
            items = data.get("items", [])
            for item in items:
                offer_id = item.get("offer_id")
                entry = stocks.setdefault(offer_id, {"fbo": 0, "fbs": 0})
                for s in item.get("stocks", []):
                    stype = s.get("type")
                    if stype in entry:
                        entry[stype] += s.get("present", 0)
            cursor = data.get("cursor", "")
            if not cursor or len(items) < 1000:
                break
        return stocks

    def get_actions(self):
        """GET /v1/actions — all promotions Ozon is currently running, with
        is_participating / participating_products_count already computed for this seller."""
        return self._get("/v1/actions").get("result", [])

    def get_action_products(self, action_id):
        """Paginates /v1/actions/products — items (by product_id) already IN the action."""
        products = []
        offset = 0
        while True:
            data = self._post("/v1/actions/products", {"action_id": action_id, "limit": 1000, "offset": offset})
            batch = data.get("result", {}).get("products", [])
            products.extend(batch)
            offset += len(batch)
            if len(batch) < 1000:
                break
        return products

    def get_action_candidates(self, action_id):
        """Paginates /v1/actions/candidates — items eligible to join but not yet in the action."""
        products = []
        offset = 0
        while True:
            data = self._post("/v1/actions/candidates", {"action_id": action_id, "limit": 1000, "offset": offset})
            batch = data.get("result", {}).get("products", [])
            products.extend(batch)
            offset += len(batch)
            if len(batch) < 1000:
                break
        return products

    def deactivate_products_from_action(self, action_id, product_ids):
        """POST /v1/actions/products/deactivate — removes products from a
        promotion. Returns the ids Ozon actually removed (it can reject some)."""
        data = self._post("/v1/actions/products/deactivate", {
            "action_id": action_id, "product_ids": product_ids,
        })
        return data.get("result", {}).get("product_ids", [])

    def activate_products_in_action(self, action_id, products):
        """POST /v1/actions/products/activate — adds products to a promotion.
        `products`: [{"product_id": int, "action_price": float, "stock": int}].
        Returns (added_ids, rejected) — rejected is Ozon's per-product error list."""
        data = self._post("/v1/actions/products/activate", {
            "action_id": action_id, "products": products,
        })
        result = data.get("result", {})
        return result.get("product_ids", []), result.get("rejected", [])

    def get_fbs_postings(self, date_from, date_to):
        """Paginates /v3/posting/fbs/list for [date_from, date_to) (ISO datetimes),
        with financial_data (per-product commission_amount/payout — actual, not estimated)."""
        postings = []
        offset = 0
        while True:
            data = self._post("/v3/posting/fbs/list", {
                "filter": {"since": date_from, "to": date_to},
                "limit": 1000, "offset": offset,
                "with": {"financial_data": True},
            })["result"]
            batch = data["postings"]
            postings.extend(batch)
            offset += len(batch)
            if not data.get("has_next") or not batch:
                break
        return postings

    def get_fbo_postings(self, date_from, date_to):
        """Paginates /v2/posting/fbo/list for [date_from, date_to), same financial_data shape as FBS."""
        postings = []
        offset = 0
        while True:
            batch = self._post("/v2/posting/fbo/list", {
                "filter": {"since": date_from, "to": date_to},
                "limit": 1000, "offset": offset,
                "with": {"financial_data": True},
            })["result"]
            postings.extend(batch)
            offset += len(batch)
            if len(batch) < 1000:
                break
        return postings

    def get_accrual_by_day(self, date_str):
        """POST /v1/finance/accrual/by-day for one date (YYYY-MM-DD). Returns individual
        accrual line items — actual booked fees (logistics services, acquiring, corrections,
        claims, etc.) that aren't part of a posting's commission_amount."""
        data = self._post("/v1/finance/accrual/by-day", {"date": date_str})
        return data.get("accruals", [])

    def get_realization_report(self, year, month):
        """POST /v2/finance/realization — the official monthly seller settlement
        report (Отчёт о реализации). Returns the raw `result` dict with header+rows;
        this is the authoritative source for actual commission/logistics figures."""
        data = self._post("/v2/finance/realization", {"year": year, "month": month}, timeout=120)
        return data.get("result", {})

    def get_seller_info(self):
        """POST /v1/seller/info — company.name is the shop's storefront name
        (e.g. "Все для дома и дачи"); used to auto-label a connected cabinet
        instead of a generic "Ozon"."""
        return self._post("/v1/seller/info", {})

    def get_warehouses(self):
        """POST /v1/warehouse/list — this seller's own warehouses (FBS/rFBS).
        Needed to resolve a warehouse_id for update_stocks — Ozon requires one
        per line, there's no "just use the default" shortcut."""
        return self._post("/v1/warehouse/list", {}).get("result", [])

    def update_stocks(self, items):
        """POST /v2/products/stocks — sets FBS stock quantity. `items`:
        [{"offer_id": str, "stock": int, "warehouse_id": int}], up to 100 per
        call. Returns Ozon's per-item result (each with updated/errors)."""
        results = []
        for i in range(0, len(items), 100):
            chunk = items[i:i + 100]
            data = self._post("/v2/products/stocks", {"stocks": chunk})
            results.extend(data.get("result", []))
        return results


# Backward-compat default client for the original single-shop dashboard, built
# from the process-wide env vars. New (multi-tenant) code should construct its
# own OzonClient(client_id, api_key) per cabinet instead of using this.
default_client = None
if os.environ.get("OZON_CLIENT_ID") and os.environ.get("OZON_API_KEY"):
    default_client = OzonClient(os.environ["OZON_CLIENT_ID"], os.environ["OZON_API_KEY"])
