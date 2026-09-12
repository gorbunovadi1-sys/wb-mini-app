import datetime
import logging

from .db import SessionLocal
from .models import OzonSalesCache

log = logging.getLogger("ozon_sales_cache")

# Mirrors wb_sales_cache.WINDOW_DAYS — build_margin_summary needs data back
# to prev_d_from (an extra `days` beyond the requested cutoff, for the
# period-over-period comparison), so the widest supported range (180 days,
# see the /margin route's cap) needs cover back to today-359 in the worst
# case. 180 keeps every common view (7/30/90 days) cache-servable without
# doubling the daily accrual-fetch cost for the rare very-wide custom range,
# which still falls back to a live fetch.
WINDOW_DAYS = 180
STALE_AFTER = datetime.timedelta(hours=4)
# A much shorter window than STALE_AFTER, used only to decide whether a
# just-started process should skip its startup refresh — several redeploys
# in quick succession (a debugging session, say) would otherwise each kick
# off a fresh full-account burst across every cabinet, which is exactly the
# kind of repeated load that trips Ozon's rate limiting in the first place.
RECENTLY_REFRESHED_WITHIN = datetime.timedelta(minutes=30)


def refresh(client, cabinet_id: int):
    """Fetches the last WINDOW_DAYS of raw FBS+FBO postings and per-day
    accrual breakdown from Ozon and overwrites this cabinet's cache. Called
    by the background scheduler, not per-request — this is the same fetch
    ozon_margin.build_margin_summary used to do live on every tab open."""
    from . import ozon_margin  # local import: avoid a cycle at module load

    today = datetime.date.today()
    fetch_from = today - datetime.timedelta(days=WINDOW_DAYS)
    iso_from, iso_to = f"{fetch_from.isoformat()}T00:00:00Z", f"{today.isoformat()}T23:59:59Z"

    postings = client.get_fbs_postings(iso_from, iso_to) + client.get_fbo_postings(iso_from, iso_to)
    buyout_posting_numbers = {
        p.get("posting_number") for p in postings if p.get("status") in ozon_margin.BUYOUT_STATUSES
    }
    accrual_by_date, non_item_by_date = ozon_margin.fetch_accrual_by_date(client, fetch_from, today, buyout_posting_numbers)

    with SessionLocal() as session:
        existing = session.get(OzonSalesCache, cabinet_id)
        if existing:
            existing.postings = postings
            existing.accrual_by_date = accrual_by_date
            existing.non_item_by_date = non_item_by_date
            existing.period_from = fetch_from.isoformat()
            existing.period_to = today.isoformat()
        else:
            session.add(OzonSalesCache(
                cabinet_id=cabinet_id, postings=postings,
                accrual_by_date=accrual_by_date, non_item_by_date=non_item_by_date,
                period_from=fetch_from.isoformat(), period_to=today.isoformat(),
            ))
        session.commit()
    log.info(f"Refreshed Ozon sales cache for cabinet {cabinet_id}: {len(postings)} postings, {len(accrual_by_date)} accrual days")


def refreshed_recently(cabinet_id: int) -> bool:
    """True if this cabinet's cache was refreshed within RECENTLY_REFRESHED_WITHIN —
    used to skip a redundant startup refresh right after a redeploy."""
    with SessionLocal() as session:
        cached = session.get(OzonSalesCache, cabinet_id)
        if not cached:
            return False
        return datetime.datetime.utcnow() - cached.updated_at <= RECENTLY_REFRESHED_WITHIN


def get(cabinet_id: int):
    """Returns (postings, accrual_by_date, non_item_by_date, period_from, is_stale) or None."""
    with SessionLocal() as session:
        cached = session.get(OzonSalesCache, cabinet_id)
        if not cached:
            return None
        is_stale = datetime.datetime.utcnow() - cached.updated_at > STALE_AFTER
        return cached.postings, cached.accrual_by_date, cached.non_item_by_date, cached.period_from, is_stale
