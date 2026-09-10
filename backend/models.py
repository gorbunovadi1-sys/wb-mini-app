import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True, nullable=False, index=True)
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    is_blocked = Column(Boolean, default=False, nullable=False)
    # NULL = unlimited access (default for everyone until explicitly time-limited).
    access_until = Column(DateTime, nullable=True)

    cabinets = relationship("Cabinet", back_populates="user", cascade="all, delete-orphan")


class Cabinet(Base):
    """One connected WB or Ozon account. Credentials are Fernet-encrypted JSON —
    {"api_key": "..."} for WB, {"client_id": "...", "api_key": "..."} for Ozon."""
    __tablename__ = "cabinets"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    marketplace = Column(String, nullable=False)  # "wb" | "ozon"
    display_name = Column(String, nullable=True)
    encrypted_credentials = Column(Text, nullable=False)
    settings = Column(JSON, default=dict)  # per-cabinet rules: margin floor, auto-approve flags, ...
    is_active = Column(Boolean, default=True)
    last_synced_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="cabinets")


class WBSalesCache(Base):
    """Raw WB sales-report rows for one cabinet, covering a rolling window
    (period_from..period_to). WB's finance-api is throttled to 1 req/min and
    a 30-day window alone needs 15-20+ throttled calls, so this is fetched
    periodically by a background job (see wb_sales_cache.py) instead of on
    every request — margin.build_margin_summary() re-aggregates from these
    rows in memory (fast) instead of re-fetching from WB (slow)."""
    __tablename__ = "wb_sales_cache"

    cabinet_id = Column(Integer, ForeignKey("cabinets.id"), primary_key=True)
    rows = Column(JSON, nullable=False)
    period_from = Column(String, nullable=False)
    period_to = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class CostPrice(Base):
    """Per-cabinet cost price, keyed by the marketplace's own item id —
    offer_id for Ozon, str(nm_id) for WB."""
    __tablename__ = "cost_prices"
    __table_args__ = (UniqueConstraint("cabinet_id", "item_key", name="uq_cost_price_cabinet_item"),)

    id = Column(Integer, primary_key=True)
    cabinet_id = Column(Integer, ForeignKey("cabinets.id"), nullable=False, index=True)
    item_key = Column(String, nullable=False)
    cost_price = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
