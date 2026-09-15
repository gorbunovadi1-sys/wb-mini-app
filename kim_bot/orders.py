"""Polls WB/Ozon FBS orders into FbsOrder and figures out which ones are
late for assembly. Two separate concerns kept apart on purpose:
  - sync_* just reflects marketplace state into the DB (idempotent, safe to
    call every poll).
  - due_alerts() reads that state and decides who's overdue — no network
    calls, easy to reason about/test independently of the API clients.
"""
import datetime
import logging

from . import articles
from .config import ALERT_COOLDOWN_MINUTES, SLA_HOURS
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
            pending = wb_client.is_pending_assembly(supplier_status)
            cancelled = wb_client.is_cancelled(supplier_status)
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


def due_alerts() -> list[dict]:
    """Orders past the SLA that haven't been alerted on recently — marks
    them alerted (updates last_alert_at/alert_count) and returns what to
    send. Call this once per poll cycle, after sync_*."""
    now = datetime.datetime.utcnow()
    sla_cutoff = now - datetime.timedelta(hours=SLA_HOURS)
    cooldown_cutoff = now - datetime.timedelta(minutes=ALERT_COOLDOWN_MINUTES)

    alerts = []
    with SessionLocal() as db:
        candidates = db.query(FbsOrder).filter(
            FbsOrder.ready_for_pack_at.is_(None),
            FbsOrder.cancelled_at.is_(None),
            FbsOrder.created_at <= sla_cutoff,
        ).all()
        for o in candidates:
            if o.last_alert_at and o.last_alert_at > cooldown_cutoff:
                continue
            age = now - o.created_at
            alerts.append({
                "marketplace": o.marketplace,
                "order_id": o.order_id,
                "article": o.article,
                "age_hours": round(age.total_seconds() / 3600, 1),
            })
            o.last_alert_at = now
            o.alert_count += 1
        db.commit()
    return alerts
