import os
import time
import logging
import requests

log = logging.getLogger("wb_client")

WB_API_KEY = os.environ["WB_API_KEY"]
H = {"Authorization": WB_API_KEY, "Content-Type": "application/json"}

FINANCE_BASE = "https://finance-api.wildberries.ru"
ADVERT_BASE = "https://advert-api.wildberries.ru"
STATS_BASE = "https://statistics-api.wildberries.ru"

# The finance-reports endpoints (list + detailed) share a strict account-wide
# limit of 1 request/minute. Both calls below funnel through this throttle.
_FINANCE_MIN_INTERVAL = 61
_last_finance_call = 0.0


def _finance_post(url, payload, timeout=60):
    global _last_finance_call
    for attempt in range(5):
        wait = _FINANCE_MIN_INTERVAL - (time.time() - _last_finance_call)
        if wait > 0:
            log.info(f"Throttling finance API call, waiting {wait:.0f}s")
            time.sleep(wait)
        r = requests.post(url, headers=H, json=payload, timeout=timeout)
        _last_finance_call = time.time()
        if r.status_code != 429:
            return r
        retry_after = int(r.headers.get("Retry-After", _FINANCE_MIN_INTERVAL))
        log.warning(f"429 from finance API, retrying in {retry_after}s (attempt {attempt+1})")
        time.sleep(retry_after)
    return r


def get_sales_reports(date_from, date_to, period="weekly"):
    r = _finance_post(
        f"{FINANCE_BASE}/api/finance/v1/sales-reports/list",
        {"dateFrom": date_from, "dateTo": date_to, "period": period},
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.status_code == 200 else []


def get_report_detail(report_id):
    """Paginates through a report's detail rows using rrdId cursor.
    Each page is itself a throttled finance-api call (1/min)."""
    rows = []
    rrd_id = 0
    while True:
        r = _finance_post(
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


def get_active_campaign_ids(changed_since=None):
    """Returns campaign ids that are currently manageable (ready/active/paused),
    plus completed campaigns (status 7) that changed on/after `changed_since`
    (an ISO date string) — keeps the list from including years of old history."""
    r = requests.get(f"{ADVERT_BASE}/adv/v1/promotion/count", headers=H, timeout=30)
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


def get_campaign_fullstats(advert_ids, date_from, date_to):
    results = []
    for i in range(0, len(advert_ids), 50):
        chunk = advert_ids[i:i + 50]
        r = requests.get(
            f"{ADVERT_BASE}/adv/v3/fullstats",
            headers=H, params={"ids": ",".join(map(str, chunk)), "beginDate": date_from, "endDate": date_to},
            timeout=60,
        )
        if r.status_code == 200:
            results.extend(r.json())
        time.sleep(0.3)
    return results


def get_campaign_details(advert_ids):
    results = []
    for i in range(0, len(advert_ids), 50):
        chunk = advert_ids[i:i + 50]
        r = requests.get(
            f"{ADVERT_BASE}/api/advert/v2/adverts",
            headers=H, params={"ids": ",".join(map(str, chunk))}, timeout=60,
        )
        if r.status_code == 200:
            results.extend(r.json().get("adverts", []))
        time.sleep(0.3)
    return results
