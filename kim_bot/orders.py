"""Polls WB/Ozon FBS orders into FbsOrder and figures out which ones are
late for assembly. Three separate concerns kept apart on purpose:
  - sync_* just reflects marketplace state into the DB (idempotent, safe to
    call every poll).
  - overdue_orders()/mark_alerted()/still_overdue_recently() read that state
    for the twice-daily alert schedule (09:00 morning digest, 15:00 follow-up
    on the same orders only if still unresolved) — no network calls, easy to
    reason about/test independently of the API clients.
"""
import datetime
import logging

from . import articles
from .config import SLA_HOURS
from .db import SessionLocal
from .models import FbsOrder
from . import ozon_client
from . import wb_client
from .util import parse_dt as _parse_dt

log = logging.getLogger("kim_bot.orders")


def _upsert(db, marketplace: str, order_id: str, article: str, qty: int, created_at: datetime.datetime,
            status: str, pending: bool, cancelled: bool):
    row = db.query(FbsOrder).filter_by(marketplace=marketplace, order_id=order_id).first()
    now = datetime.datetime.utcnow()
    if row is None:
        row = FbsOrder(
            marketplace=marketplace, order_id=order_id,
            article=articles.canonical_article(marketplace, article) if article else None,
            qty=qty, created_at=created_at, status=status,
        )
        db.add(row)
    row.status = status
    if not pending and row.ready_for_pack_at is None and not cancelled:
        row.ready_for_pack_at = now
    if cancelled and row.cancelled_at is None:
        row.cancelled_at = now


def sync_wb_orders(client: "wb_client.WBClient", since_date: str):
    """Backfills/refreshes every WB order since `since_date` (YYYY-MM-DD).
    /orders doesn't carry a status field at all (verified live) — every
    order, backfilled or not, needs the separate batched /orders/status
    call to know its real supplierStatus."""
    raw_orders = client.get_orders_since(since_date)
    parsed = [wb_client.parse_order(o) for o in raw_orders]
    order_ids = [int(p["order_id"]) for p in parsed]
    statuses = client.get_order_statuses(order_ids) if order_ids else {}

    with SessionLocal() as db:
        for p in parsed:
            info = statuses.get(int(p["order_id"]))
            if info is None:
                log.warning(f"No status returned for WB order {p['order_id']} — skipping this cycle")
                continue
            supplier_status = info.get("supplierStatus")
            wb_status = info.get("wbStatus")
            pending = wb_client.is_pending_assembly(supplier_status, wb_status)
            cancelled = wb_client.is_cancelled(supplier_status, wb_status)
            _upsert(
                db, "wb", p["order_id"], p["article"], p["qty"],
                _parse_dt(p["created_at"]), supplier_status,
                pending=pending, cancelled=cancelled,
            )
        db.commit()


def sync_ozon_orders(client: "ozon_client.OzonClient", since_date: str):
    """Ozon's posting list always reflects current status for anything in
    the window, so no separate status lookup is needed like WB's."""
    date_from = f"{since_date}T00:00:00.000Z"
    date_to = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    postings = client.get_fbs_postings(date_from, date_to)

    with SessionLocal() as db:
        for posting in postings:
            for row in ozon_client.parse_posting(posting):
                if not row["created_at"]:
                    continue
                pending = ozon_client.is_pending_assembly(row["status"])
                cancelled = ozon_client.is_cancelled(row["status"])
                _upsert(
                    db, "ozon", row["order_id"], row["article"], row["qty"],
                    _parse_dt(row["created_at"]), row["status"],
                    pending=pending, cancelled=cancelled,
                )
        db.commit()


def _format_age(age: datetime.timedelta) -> str:
    total_minutes = int(age.total_seconds() // 60)
    return f"{total_minutes // 60}ч {total_minutes % 60}мин"


def _alert_dict(o: FbsOrder, now: datetime.datetime) -> dict:
    return {
        "marketplace": o.marketplace,
        "order_id": o.order_id,
        "article": o.article,
        "age": _format_age(now - o.created_at),
    }


def pending_orders() -> list[dict]:
    """Every order not yet sent to assembly, right now — for on-demand
    checks (the /orders bot command), not tied to the alert schedule.
    Reads whatever sync_orders last wrote (no live API calls here, so it's
    instant, at most ~15 min stale)."""
    now = datetime.datetime.utcnow()
    sla_cutoff = now - datetime.timedelta(hours=SLA_HOURS)
    with SessionLocal() as db:
        rows = db.query(FbsOrder).filter(
            FbsOrder.ready_for_pack_at.is_(None),
            FbsOrder.cancelled_at.is_(None),
        ).order_by(FbsOrder.created_at).all()
        return [
            {**_alert_dict(o, now), "overdue": o.created_at <= sla_cutoff}
            for o in rows
        ]


def overdue_orders() -> list[dict]:
    """Every order past the SLA and not yet resolved, right now — no side
    effects. Used for the 09:00 morning digest."""
    now = datetime.datetime.utcnow()
    sla_cutoff = now - datetime.timedelta(hours=SLA_HOURS)
    with SessionLocal() as db:
        candidates = db.query(FbsOrder).filter(
            FbsOrder.ready_for_pack_at.is_(None),
            FbsOrder.cancelled_at.is_(None),
            FbsOrder.created_at <= sla_cutoff,
        ).all()
        return [_alert_dict(o, now) for o in candidates]


def mark_alerted(keys: list[tuple]):
    """Stamps last_alert_at on the given (marketplace, order_id) pairs —
    call right after sending the morning digest, so still_overdue_recently
    knows which orders were in it."""
    now = datetime.datetime.utcnow()
    with SessionLocal() as db:
        for marketplace, order_id in keys:
            o = db.query(FbsOrder).filter_by(marketplace=marketplace, order_id=order_id).first()
            if o:
                o.last_alert_at = now
                o.alert_count += 1
        db.commit()


def still_overdue_recently(within_hours: float = 7) -> list[dict]:
    """Orders flagged in the morning digest (last_alert_at within the last
    `within_hours`, i.e. since ~09:00) that are still unresolved — used for
    the 15:00 follow-up. Deliberately does NOT pick up newly-overdue orders
    that weren't in the morning digest; those wait for the next morning."""
    now = datetime.datetime.utcnow()
    since = now - datetime.timedelta(hours=within_hours)
    with SessionLocal() as db:
        candidates = db.query(FbsOrder).filter(
            FbsOrder.ready_for_pack_at.is_(None),
            FbsOrder.cancelled_at.is_(None),
            FbsOrder.last_alert_at.isnot(None),
            FbsOrder.last_alert_at >= since,
        ).all()
        return [_alert_dict(o, now) for o in candidates]
