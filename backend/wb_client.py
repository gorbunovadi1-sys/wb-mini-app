import os
import re
import time
import logging
import requests

log = logging.getLogger("wb_client")

FINANCE_BASE = "https://finance-api.wildberries.ru"
ADVERT_BASE = "https://advert-api.wildberries.ru"
STATS_BASE = "https://statistics-api.wildberries.ru"
COMMON_BASE = "https://common-api.wildberries.ru"

# The finance-reports endpoints share a strict 1 request/minute limit, per account.
_FINANCE_MIN_INTERVAL = 61


def _sanitize_key(raw: str) -> str:
    """WB API keys are JWTs (base64url segments joined by dots), so only
    A-Z a-z 0-9 . _ - are ever valid. Strips anything else — guards against
    stray whitespace/invisible characters that can sneak in via a hosting
    provider's env-var UI (seen: a UnicodeEncodeError from a corrupted
    Railway-stored value that broke HTTP header encoding)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", raw)
    if cleaned != raw.strip():
        log.warning(f"WB_API_KEY contained unexpected characters and was sanitized (raw len={len(raw)}, cleaned len={len(cleaned)})")
    return cleaned


class WBClient:
    """One seller's WB API key, bound to every request this instance makes —
    lets different cabinets (different Telegram users) hit the same endpoints
    with their own key at the same time. The finance-API 1/min throttle is
    tracked per instance, since it's a per-account limit, not a global one."""

    def __init__(self, api_key: str):
        self.api_key = _sanitize_key(api_key)
        self.headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        self._last_finance_call = 0.0

    def check_credentials(self):
        """Cheap call used to validate a key during cabinet onboarding.
        Raises requests.HTTPError (401/403) if the key is wrong."""
        r = requests.get(f"{ADVERT_BASE}/adv/v1/promotion/count", headers=self.headers, timeout=15)
        r.raise_for_status()

    def get_seller_info(self):
        """GET /api/v1/seller-info — tradeMark is the shop's storefront brand
        name (e.g. "Техника для жизни"), name is the legal entity; used to
        auto-label a connected cabinet instead of a generic "Wildberries"."""
        r = requests.get(f"{COMMON_BASE}/api/v1/seller-info", headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def _finance_post(self, url, payload, timeout=60):
        for attempt in range(5):
            wait = _FINANCE_MIN_INTERVAL - (time.time() - self._last_finance_call)
            if wait > 0:
                log.info(f"Throttling finance API call, waiting {wait:.0f}s")
                time.sleep(wait)
            r = requests.post(url, headers=self.headers, json=payload, timeout=timeout)
            self._last_finance_call = time.time()
            if r.status_code != 429:
                return r
            retry_after = int(r.headers.get("Retry-After", _FINANCE_MIN_INTERVAL))
            log.warning(f"429 from finance API, retrying in {retry_after}s (attempt {attempt + 1})")
            time.sleep(retry_after)
        return r

    def get_sales_reports(self, date_from, date_to, period="weekly"):
        r = self._finance_post(
            f"{FINANCE_BASE}/api/finance/v1/sales-reports/list",
            {"dateFrom": date_from, "dateTo": date_to, "period": period},
            timeout=30,
        )
        r.raise_for_status()
        return r.json() if r.status_code == 200 else []

    def get_report_detail(self, report_id):
        """Paginates through a report's detail rows using rrdId cursor.
        Each page is itself a throttled finance-api call (1/min)."""
        rows = []
        rrd_id = 0
        while True:
            r = self._finance_post(
                f"{FINANCE_BASE}/api/finance/v1/sales-reports/detailed/{report_id}",
                {"limit": 100000, "rrdId": rrd_id},
            )
            if r.status_code == 204:
                break
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            rrd_id = batch[-1]["rrdId"]
            if len(batch) < 100000:
                break
        return rows

    def get_active_campaign_ids(self, changed_since=None):
        """Returns campaign ids that are currently manageable (ready/active/paused),
        plus completed campaigns (status 7) that changed on/after `changed_since`
        (an ISO date string) — keeps the list from including years of old history."""
        r = requests.get(f"{ADVERT_BASE}/adv/v1/promotion/count", headers=self.headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        ids = []
        for group in data.get("adverts", []):
            status = group.get("status")
            if status in (4, 9, 11):
                for a in group.get("advert_list", []):
                    ids.append(a["advertId"])
            elif status == 7 and changed_since:
                for a in group.get("advert_list", []):
                    change_time = a.get("changeTime", "")
                    if change_time[:10] >= changed_since:
                        ids.append(a["advertId"])
        return ids

    def get_campaign_fullstats(self, advert_ids, date_from, date_to):
        results = []
        for i in range(0, len(advert_ids), 50):
            chunk = advert_ids[i:i + 50]
            r = requests.get(
                f"{ADVERT_BASE}/adv/v3/fullstats",
                headers=self.headers, params={"ids": ",".join(map(str, chunk)), "beginDate": date_from, "endDate": date_to},
                timeout=60,
            )
            if r.status_code == 200:
                results.extend(r.json())
            time.sleep(0.3)
        return results

    def get_campaign_details(self, advert_ids):
        results = []
        for i in range(0, len(advert_ids), 50):
            chunk = advert_ids[i:i + 50]
            r = requests.get(
                f"{ADVERT_BASE}/api/advert/v2/adverts",
                headers=self.headers, params={"ids": ",".join(map(str, chunk))}, timeout=60,
            )
            if r.status_code == 200:
                results.extend(r.json().get("adverts", []))
            time.sleep(0.3)
        return results


# Backward-compat default client for the original single-shop dashboard, built
# from the process-wide env var. New (multi-tenant) code should construct its
# own WBClient(api_key) per cabinet instead of using this.
default_client = WBClient(os.environ["WB_API_KEY"]) if os.environ.get("WB_API_KEY") else None
