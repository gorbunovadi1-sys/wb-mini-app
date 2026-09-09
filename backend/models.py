import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True, nullable=False, index=True)
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

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
