import datetime
import logging

from .db import SessionLocal
from .models import WBSalesCache

log = logging.getLogger("wb_sales_cache")

WINDOW_DAYS = 90  # covers every quick-chip (7/14/30/90) in one fetch
STALE_AFTER = datetime.timedelta(hours=4)


def refresh(client, cabinet_id: int):
    """Fetches the last WINDOW_DAYS of raw report rows from WB (slow — see
    margin.fetch_rows) and overwrites this cabinet's cache. Called by the
    background scheduler, not per-request."""
    from . import margin  # local import: margin imports wb_client, avoid a cycle at module load

    today = datetime.date.today()
    fetch_from = today - datetime.timedelta(days=WINDOW_DAYS)
    rows = margin.fetch_rows(client, fetch_from, today)

    with SessionLocal() as session:
        existing = session.get(WBSalesCache, cabinet_id)
        if existing:
            existing.rows = rows
            existing.period_from = fetch_from.isoformat()
            existing.period_to = today.isoformat()
        else:
            session.add(WBSalesCache(
                cabinet_id=cabinet_id, rows=rows,
                period_from=fetch_from.isoformat(), period_to=today.isoformat(),
            ))
        session.commit()
    log.info(f"Refreshed WB sales cache for cabinet {cabinet_id}: {len(rows)} rows")


def get(cabinet_id: int):
    """Returns (rows, period_from, is_stale) or None if never cached."""
    with SessionLocal() as session:
        cached = session.get(WBSalesCache, cabinet_id)
        if not cached:
            return None
        is_stale = datetime.datetime.utcnow() - cached.updated_at > STALE_AFTER
        return cached.rows, cached.period_from, is_stale
