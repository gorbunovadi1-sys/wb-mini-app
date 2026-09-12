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


# If more than this fraction of the accrual window's days fail outright
# (sustained 429s exhausting even the client's own retries), the result is
# too degraded to trust as "the new truth" — better to keep serving
# yesterday's real numbers a while longer than to silently zero out
# whichever days Ozon happened to reject this run.
MAX_FAILED_ACCRUAL_DAY_FRACTION = 0.1


def refresh(client, cabinet_id: int):
    """Fetches the last WINDOW_DAYS of raw FBS+FBO postings and flat accrual
    entries from Ozon and updates this cabinet's cache. Called by the
    background scheduler, not per-request — this is the same fetch
    ozon_margin.build_margin_summary used to do live on every tab open.

    Postings and accrual are fetched and evaluated independently, and each
    is persisted only if this run's fetch actually succeeded — otherwise
    that half of the cache is left exactly as it was. This matters most for
    a very large cabinet (thousands of postings needing dozens of paginated
    calls, plus one call per day across the whole window — close to 200
    sequential requests total): previously, a single request anywhere in
    that chain exhausting its retries raised an exception that aborted the
    entire refresh BEFORE the one DB write at the end, discarding
    everything already fetched this run and leaving the cache stuck getting
    no fresher no matter how many of the ~200 requests actually succeeded.
    Now a bad postings fetch or a badly-degraded accrual fetch just keeps
    the previous cache for that part, while the healthy part still updates
    — so `updated_at` (and therefore is_stale) reflects real partial
    progress instead of an all-or-nothing gate, and the next run 3h later
    only has to make up the part that actually failed.

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

    with SessionLocal() as session:
        existing = session.get(OzonSalesCache, cabinet_id)
        old_postings = existing.postings if existing else []
        old_accrual_entries = existing.accrual_entries if existing else []
        old_non_item_by_date = existing.non_item_by_date if existing else {}

    postings, postings_fresh = old_postings, False
    try:
        postings = client.get_fbs_postings(iso_from, iso_to) + client.get_fbo_postings(iso_from, iso_to)
        postings_fresh = True
    except Exception:
        log.exception(f"Postings fetch failed for cabinet {cabinet_id} — keeping {len(old_postings)} previously cached postings")

    accrual_entries, non_item_by_date = old_accrual_entries, old_non_item_by_date
    accrual_fresh = False
    try:
        new_entries, new_non_item_by_date, failed_dates = ozon_margin.fetch_accrual_entries(client, fetch_from, today)
        window_days = (today - fetch_from).days + 1
        if len(failed_dates) > window_days * MAX_FAILED_ACCRUAL_DAY_FRACTION:
            log.warning(
                f"Accrual fetch for cabinet {cabinet_id} had {len(failed_dates)}/{window_days} failed days "
                f"— too degraded to trust, keeping previous accrual cache"
            )
        else:
            if failed_dates:
                log.warning(f"Accrual fetch for cabinet {cabinet_id} had {len(failed_dates)}/{window_days} failed days, accepted anyway: {failed_dates}")
            accrual_entries, non_item_by_date = new_entries, new_non_item_by_date
            accrual_fresh = True
    except Exception:
        log.exception(f"Accrual fetch failed for cabinet {cabinet_id} — keeping previously cached accrual data")

    if not postings_fresh and not accrual_fresh:
        log.warning(f"Ozon sales cache refresh for cabinet {cabinet_id} fetched nothing new this run — cache left untouched")
        return

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
    log.info(
        f"Refreshed Ozon sales cache for cabinet {cabinet_id}: "
        f"{len(postings)} postings ({'fresh' if postings_fresh else 'kept previous'}), "
        f"{len(accrual_entries)} accrual entries ({'fresh' if accrual_fresh else 'kept previous'})"
    )


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
