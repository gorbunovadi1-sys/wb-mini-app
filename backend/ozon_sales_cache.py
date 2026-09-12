import datetime
import logging

from .db import SessionLocal
from .models import OzonSalesCache

log = logging.getLogger("ozon_sales_cache")

# How far back a cabinet's cache eventually backfills to. build_margin_summary
# needs data back to prev_d_from (an extra `days` beyond the requested
# cutoff, for period-over-period comparison), so the widest common view
# (90 days) needs cover back to today-180 to stay fully cache-servable
# without falling back to a live fetch. The rare very-wide custom range
# (up to 180 days, see the /margin route's cap) would need cover back to
# today-359 for ITS OWN comparison — that still falls back to a live fetch,
# same as before this module supported incremental backfill.
WINDOW_DAYS = 180
# A cabinet with more postings than this in its last cached run targets a
# smaller window instead — tuned to the one real oversized cabinet seen so
# far (26,207 postings, ~8x the next largest), not a principled cutoff.
# Revisit if more cabinets grow into this range. The tradeoff: THIS
# cabinet's own "90 дней" view loses full cache coverage for its
# period-over-period comparison and falls back to a live fetch — acceptable
# for a cabinet this size, versus every view failing outright.
LARGE_CABINET_POSTING_THRESHOLD = 10000
REDUCED_WINDOW_DAYS = 90

# The "hot" tail of the window is refetched in full on every single cycle,
# regardless of cabinet size or how much history is still being backfilled
# — recent data is what Цены/Акции/Детализация actually depend on
# minute-to-minute, and it's also exactly the part Ozon's settlement lag
# keeps revising after the fact (see ozon_margin.attribute_accrual_entries).
# 45 days comfortably covers the stated 14-day minimum plus a real
# settlement-lag buffer, while staying cheap enough (~45 accrual-by-day
# calls + a couple posting-list pages, even for a huge cabinet) to refresh
# every cycle without tripping Ozon's rate limiting on its own.
HOT_WINDOW_DAYS = 45
# Anything older than the hot window is backfilled gradually, one chunk
# this wide per refresh cycle, instead of ever being fetched all at once —
# this is what actually fixes a huge cabinet's refresh, rather than merely
# tolerating its failures: no single cycle ever asks Ozon for more than a
# hot-window's plus one chunk's worth of requests, however far back the
# target window (WINDOW_DAYS/REDUCED_WINDOW_DAYS) still needs to reach. A
# cabinet starting from zero reaches full target coverage within a handful
# of 3h cycles instead of needing one single ~200-request burst to ever
# fully succeed.
BACKFILL_CHUNK_DAYS = 30

STALE_AFTER = datetime.timedelta(hours=4)
# A much shorter window than STALE_AFTER, used only to decide whether a
# just-started process should skip its startup refresh — several redeploys
# in quick succession (a debugging session, say) would otherwise each kick
# off a fresh hot-window burst across every cabinet, exactly the kind of
# repeated load that trips Ozon's rate limiting in the first place.
RECENTLY_REFRESHED_WITHIN = datetime.timedelta(minutes=30)

# If more than this fraction of a fetched range's days fail outright
# (sustained 429s exhausting even the client's own retries), that range's
# result is too degraded to trust — better to keep serving the previous
# data for it a while longer than to silently zero out whichever days Ozon
# happened to reject this run.
MAX_FAILED_ACCRUAL_DAY_FRACTION = 0.1


def _posting_date(posting: dict) -> str:
    ts = posting.get("in_process_at") or posting.get("created_at") or ""
    return ts[:10]


def _fetch_postings_range(client, date_from: datetime.date, date_to: datetime.date) -> list:
    iso_from = f"{date_from.isoformat()}T00:00:00Z"
    iso_to = f"{date_to.isoformat()}T23:59:59Z"
    return client.get_fbs_postings(iso_from, iso_to) + client.get_fbo_postings(iso_from, iso_to)


def _fetch_accrual_range(client, cabinet_id: int, date_from: datetime.date, date_to: datetime.date, label: str):
    """Wraps ozon_margin.fetch_accrual_entries with the failed-day-fraction
    sanity check. Returns (entries, non_item_by_date, ok) — `ok` is False
    both on a hard exception and on a too-degraded (too many failed days)
    result; either way the caller keeps whatever it had for this range
    before. `label` is just for the log line (e.g. "hot window" vs a
    specific backfill chunk's date range)."""
    from . import ozon_margin  # local import: avoid a cycle at module load
    try:
        entries, non_item_by_date, failed_dates = ozon_margin.fetch_accrual_entries(client, date_from, date_to)
    except Exception:
        log.exception(f"Cabinet {cabinet_id}: {label} accrual fetch failed")
        return None, None, False
    total_days = (date_to - date_from).days + 1
    if len(failed_dates) > total_days * MAX_FAILED_ACCRUAL_DAY_FRACTION:
        log.warning(f"Cabinet {cabinet_id}: {label} accrual fetch had {len(failed_dates)}/{total_days} failed days — too degraded, discarding")
        return None, None, False
    if failed_dates:
        log.warning(f"Cabinet {cabinet_id}: {label} accrual fetch had {len(failed_dates)}/{total_days} failed days, accepted anyway: {failed_dates}")
    return entries, non_item_by_date, True


def refresh(client, cabinet_id: int):
    """Incrementally fetches and merges Ozon FBS+FBO postings and flat
    accrual entries into this cabinet's cache. Called by the background
    scheduler, not per-request.

    Two independent pieces are fetched each cycle, each merged in only if
    it actually succeeds — otherwise that slice of the cache is left
    exactly as it was:

    - The HOT_WINDOW_DAYS tail, always, every cycle — recent data, which is
      both what Цены/Акции/Детализация mostly depend on day to day, and
      what Ozon's settlement lag keeps revising after the fact, so it needs
      continual refetching no matter how much older history is cached.
    - One BACKFILL_CHUNK_DAYS-wide chunk immediately older than whatever's
      currently cached, only while the cache hasn't yet reached its target
      retention (WINDOW_DAYS, or REDUCED_WINDOW_DAYS for an oversized
      cabinet) — extends history backward a bit further each cycle.

    This bounds every cycle's request volume to "hot window + one backfill
    chunk" regardless of a cabinet's total size or how much history is
    still missing. A cabinet with 26,207 postings (~8x the next largest)
    previously couldn't complete a refresh for 24+ hours because a single
    bad request among the ~200 a full-window fetch needed discarded the
    whole run; now it makes small, resilient progress every cycle and
    reaches full target coverage within a handful of cycles instead of
    needing one all-or-nothing burst to ever succeed."""
    today = datetime.date.today()

    with SessionLocal() as session:
        existing = session.get(OzonSalesCache, cabinet_id)
        old_postings = existing.postings if existing else []
        old_accrual_entries = (existing.accrual_entries or []) if existing else []
        old_non_item_by_date = existing.non_item_by_date if existing else {}
        old_period_from = datetime.date.fromisoformat(existing.period_from) if existing and existing.period_from else None
        old_period_to = datetime.date.fromisoformat(existing.period_to) if existing and existing.period_to else None

    target_window_days = REDUCED_WINDOW_DAYS if len(old_postings) > LARGE_CABINET_POSTING_THRESHOLD else WINDOW_DAYS
    target_from = today - datetime.timedelta(days=target_window_days)
    hot_from = today - datetime.timedelta(days=HOT_WINDOW_DAYS)

    # --- Hot window: always attempted ---
    hot_postings, hot_postings_fresh = None, False
    try:
        hot_postings = _fetch_postings_range(client, hot_from, today)
        hot_postings_fresh = True
    except Exception:
        log.exception(f"Cabinet {cabinet_id}: hot-window postings fetch failed")

    hot_accrual_entries, hot_non_item_by_date, hot_accrual_fresh = _fetch_accrual_range(
        client, cabinet_id, hot_from, today, "hot window"
    )

    # --- Backfill: one chunk further back, only while target coverage isn't reached yet ---
    need_backfill = old_period_from is None or old_period_from > target_from
    chunk_from = chunk_to = None
    backfill_postings, backfill_accrual_entries, backfill_non_item_by_date = None, None, None
    backfill_postings_fresh = backfill_accrual_fresh = False
    if need_backfill:
        chunk_to = (old_period_from - datetime.timedelta(days=1)) if old_period_from else (hot_from - datetime.timedelta(days=1))
        chunk_from = max(target_from, chunk_to - datetime.timedelta(days=BACKFILL_CHUNK_DAYS - 1))
        if chunk_from <= chunk_to:
            try:
                backfill_postings = _fetch_postings_range(client, chunk_from, chunk_to)
                backfill_postings_fresh = True
            except Exception:
                log.exception(f"Cabinet {cabinet_id}: backfill-chunk postings fetch failed ({chunk_from}..{chunk_to})")
            backfill_accrual_entries, backfill_non_item_by_date, backfill_accrual_fresh = _fetch_accrual_range(
                client, cabinet_id, chunk_from, chunk_to, f"backfill chunk {chunk_from}..{chunk_to}"
            )

    if not any([hot_postings_fresh, hot_accrual_fresh, backfill_postings_fresh, backfill_accrual_fresh]):
        log.warning(f"Ozon sales cache refresh for cabinet {cabinet_id} fetched nothing new this run — cache left untouched")
        return

    # --- Merge: drop old data inside any range we successfully refreshed this cycle, keep everything else untouched, append the fresh pieces ---
    def _covered(date_str, ranges):
        return any(f.isoformat() <= date_str <= t.isoformat() for f, t in ranges)

    fresh_posting_ranges = [r for r, ok in [((hot_from, today), hot_postings_fresh), ((chunk_from, chunk_to), backfill_postings_fresh)] if ok]
    kept_postings = [p for p in old_postings if not _covered(_posting_date(p), fresh_posting_ranges)]
    final_postings = kept_postings + (hot_postings if hot_postings_fresh else []) + (backfill_postings if backfill_postings_fresh else [])

    fresh_accrual_ranges = [r for r, ok in [((hot_from, today), hot_accrual_fresh), ((chunk_from, chunk_to), backfill_accrual_fresh)] if ok]
    kept_accrual_entries = [e for e in old_accrual_entries if not _covered(e.get("date", ""), fresh_accrual_ranges)]
    final_accrual_entries = kept_accrual_entries + (hot_accrual_entries if hot_accrual_fresh else []) + (backfill_accrual_entries if backfill_accrual_fresh else [])

    final_non_item_by_date = {d: v for d, v in old_non_item_by_date.items() if not _covered(d, fresh_accrual_ranges)}
    if hot_accrual_fresh:
        final_non_item_by_date.update(hot_non_item_by_date)
    if backfill_accrual_fresh:
        final_non_item_by_date.update(backfill_non_item_by_date)

    # period_from only moves backward when a backfill chunk actually lands;
    # period_to only moves forward when the hot window actually lands — a
    # brand-new cabinet's very first cycle never claims coverage it doesn't
    # really have.
    if backfill_postings_fresh or backfill_accrual_fresh:
        new_period_from = chunk_from
    elif old_period_from is not None:
        new_period_from = old_period_from
    else:
        new_period_from = hot_from
    new_period_to = today if (hot_postings_fresh or hot_accrual_fresh) else (old_period_to or today)

    with SessionLocal() as session:
        existing = session.get(OzonSalesCache, cabinet_id)
        if existing:
            existing.postings = final_postings
            existing.accrual_entries = final_accrual_entries
            existing.non_item_by_date = final_non_item_by_date
            existing.period_from = new_period_from.isoformat()
            existing.period_to = new_period_to.isoformat()
        else:
            session.add(OzonSalesCache(
                cabinet_id=cabinet_id, postings=final_postings,
                accrual_entries=final_accrual_entries, non_item_by_date=final_non_item_by_date,
                period_from=new_period_from.isoformat(), period_to=new_period_to.isoformat(),
            ))
        session.commit()

    backfilled_note = "fully backfilled" if new_period_from <= target_from else f"backfilling toward {target_from} (at {new_period_from})"
    log.info(
        f"Refreshed Ozon sales cache for cabinet {cabinet_id}: {len(final_postings)} postings, "
        f"{len(final_accrual_entries)} accrual entries — covers {new_period_from}..{new_period_to}, {backfilled_note}"
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
