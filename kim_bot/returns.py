"""Polls WB/Ozon returns into ReturnRecord — counted into the stock balance
as soon as the marketplace registers them (see project decision: don't wait
for physical receipt at the warehouse)."""
import datetime
import logging

from . import articles
from .db import SessionLocal
from .models import ReturnRecord
from . import ozon_client
from .util import parse_dt as _parse_dt

log = logging.getLogger("kim_bot.returns")


def sync_wb_returns(client, date_from_iso: str):
    """/api/v1/supplier/sales rows are one physical unit each; a return is
    any row whose saleID starts with "R" (WB's long-standing convention)."""
    rows = client.get_sales_and_returns(date_from_iso)
    with SessionLocal() as db:
        for row in rows:
            sale_id = row.get("saleID") or ""
            if not sale_id.startswith("R"):
                continue
            return_id = row.get("srid") or f"{sale_id}:{row.get('odid', '')}"
            article = row.get("supplierArticle")
            created_at_raw = row.get("lastChangeDate") or row.get("date")
            if not created_at_raw:
                continue
            if db.query(ReturnRecord).filter_by(marketplace="wb", return_id=return_id).first():
                continue
            db.add(ReturnRecord(
                marketplace="wb", return_id=return_id,
                article=articles.canonical_article("wb", article) if article else None,
                qty=1, created_at=_parse_dt(created_at_raw),
            ))
        db.commit()


def sync_ozon_returns(client, date_from_iso: str):
    """The /v1/returns/list filter doesn't actually restrict by date server-
    side (verified live — it returns full history regardless), and it mixes
    Fbo returns in with Fbs — both filtered out here: Fbo stock isn't at her
    fulfillment warehouse, and anything older than date_from is irrelevant
    to the current stock calculation anyway."""
    since = _parse_dt(date_from_iso + "T00:00:00Z")
    raw_returns = client.get_returns(date_from_iso)
    with SessionLocal() as db:
        for raw in raw_returns:
            parsed = ozon_client.parse_return(raw)
            if parsed["schema"] != "Fbs":
                continue
            if not parsed["return_id"] or not parsed["created_at"]:
                continue
            created_at = _parse_dt(parsed["created_at"])
            if created_at < since:
                continue
            if db.query(ReturnRecord).filter_by(marketplace="ozon", return_id=parsed["return_id"]).first():
                continue
            db.add(ReturnRecord(
                marketplace="ozon", return_id=parsed["return_id"],
                article=articles.canonical_article("ozon", parsed["article"]) if parsed["article"] else None,
                qty=parsed["qty"], created_at=created_at,
            ))
        db.commit()
