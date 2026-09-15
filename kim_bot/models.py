import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint

from .db import Base


class ArticleMap(Base):
    """Manual override for the (rare, per her own warning) case where the
    same physical item has a different code on WB vs Ozon. canonical_article
    is what the warehouse/stock file uses; wb_article/ozon_article are only
    needed when a marketplace's own code differs from it — if null, the
    marketplace's own code is assumed to equal the canonical one."""
    __tablename__ = "kim_article_map"

    id = Column(Integer, primary_key=True)
    canonical_article = Column(String, nullable=False, unique=True)
    wb_article = Column(String, nullable=True)
    ozon_article = Column(String, nullable=True)


class StockBaseline(Base):
    """Her own counted warehouse stock as of one date — replaced wholesale
    every time she uploads a new file (see kim_bot/stock.py)."""
    __tablename__ = "kim_stock_baseline"

    id = Column(Integer, primary_key=True)
    article = Column(String, nullable=False, unique=True)
    qty = Column(Integer, nullable=False)
    as_of_date = Column(String, nullable=False)  # YYYY-MM-DD, one date for the whole upload
    uploaded_at = Column(DateTime, default=datetime.datetime.utcnow)


class FbsOrder(Base):
    """One FBS order (WB or Ozon), tracked from creation through assembly.
    ready_for_pack_at is set once the marketplace reports it's left the
    "awaiting assembly" state — that's both what stops the SLA alert and
    what counts as a shipment for the stock balance."""
    __tablename__ = "kim_fbs_order"
    __table_args__ = (UniqueConstraint("marketplace", "order_id", name="uq_kim_fbs_order"),)

    id = Column(Integer, primary_key=True)
    marketplace = Column(String, nullable=False)  # "wb" | "ozon"
    order_id = Column(String, nullable=False)
    article = Column(String, nullable=True)
    qty = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False)
    status = Column(String, nullable=True)
    ready_for_pack_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    last_alert_at = Column(DateTime, nullable=True)
    alert_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ReturnRecord(Base):
    """One customer return/refund (WB or Ozon), counted back into stock as
    soon as it's registered by the marketplace — see project memory decision
    to not wait for physical warehouse receipt."""
    __tablename__ = "kim_return_record"
    __table_args__ = (UniqueConstraint("marketplace", "return_id", name="uq_kim_return_record"),)

    id = Column(Integer, primary_key=True)
    marketplace = Column(String, nullable=False)
    return_id = Column(String, nullable=False)
    article = Column(String, nullable=True)
    qty = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False)


class DailyStockSnapshot(Base):
    """Cached daily-recomputed balance per article — lets /stock answer
    instantly and keeps a history instead of recomputing on every request."""
    __tablename__ = "kim_daily_stock_snapshot"
    __table_args__ = (UniqueConstraint("article", "snapshot_date", name="uq_kim_daily_snapshot"),)

    id = Column(Integer, primary_key=True)
    article = Column(String, nullable=False)
    snapshot_date = Column(String, nullable=False)  # YYYY-MM-DD
    balance = Column(Float, nullable=False)
    computed_at = Column(DateTime, default=datetime.datetime.utcnow)
