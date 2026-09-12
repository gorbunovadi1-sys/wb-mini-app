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
    """Fetches the last WINDOW_DAYS of raw FBS+FBO postings and flat accrual
    entries from Ozon and overwrites this cabinet's cache. Called by the
    background scheduler, not per-request — this is the same fetch
    ozon_margin.build_margin_summary used to do live on every tab open.

    Fetching accrual entries all the way through `today` (not just to
    WINDOW_DAYS worth of postings) is what makes this immune to Ozon's
    settlement lag for the CACHED path specifically — a sale near the end of
    whatever period gets requested later will have had its accrual land
    somewhere in this window by the time this ran, since the window's upper
    edge is always "now". See ozon_margin.attribute_accrual_entries for how
    a requested sub-period then correctly pulls the right entries back out
    of this un-sliced pool."""
    from . import ozon_margin  # local import: avoid a cycle at module load

    today = datetime.date.today()
    fetch_from = today - datetime.timedelta(days=WINDOW_DAYS)
    iso_from, iso_to = f"{fetch_from.isoformat()}T00:00:00Z", f"{today.isoformat()}T23:59:59Z"

    postings = client.get_fbs_postings(iso_from, iso_to) + client.get_fbo_postings(iso_from, iso_to)
    accrual_entries, non_item_by_date = ozon_margin.fetch_accrual_entries(client, fetch_from, today)

    with SessionLocal() as session:
        existing = session.get(OzonSalesCache, cabinet_id)
        if existing:
            existing.postings = postings
            existing.accrual_entries = accrual_entries
            existing.non_item_by_date = non_item_by_date
            existing.period_from = fetch_from.isoformat()
            existing.period_to = today.isoformat()
        else:
            session.add(OzonSalesCache(
                cabinet_id=cabinet_id, postings=postings,
                accrual_entries=accrual_entries, non_item_by_date=non_item_by_date,
                period_from=fetch_from.isoformat(), period_to=today.isoformat(),
            ))
        session.commit()
    log.info(f"Refreshed Ozon sales cache for cabinet {cabinet_id}: {len(postings)} postings, {len(accrual_entries)} accrual entries")


def refreshed_recently(cabinet_id: int) -> bool:
    """True if this cabinet's cache was refreshed within RECENTLY_REFRESHED_WITHIN —
    used to skip a redundant startup refresh right after a redeploy."""
    with SessionLocal() as session:
        cached = session.get(OzonSalesCache, cabinet_id)
        if not cached:
            return False
        return datetime.datetime.utcnow() - cached.updated_at <= RECENTLY_REFRESHED_WITHIN


def get(cabinet_id: int):
    """Returns (postings, accrual_entries, non_item_by_date, period_from, is_stale) or None."""
    with SessionLocal() as session:
        cached = session.get(OzonSalesCache, cabinet_id)
        if not cached:
            return None
        is_stale = datetime.datetime.utcnow() - cached.updated_at > STALE_AFTER
        return cached.postings, cached.accrual_entries, cached.non_item_by_date, cached.period_from, is_stale
