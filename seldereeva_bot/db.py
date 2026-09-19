import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Independent from backend/db.py and kim_bot/db.py on purpose — same
# reasoning as kim_bot/db.py: a bug or migration here must never be able to
# touch another bot's tables. Defaults to its own SELD_DATABASE_URL; falls
# back to the shared DATABASE_URL (same Postgres, distinct seld_-prefixed
# table names below) if that's simpler to run with, and finally to a local
# sqlite file for dev.
DATABASE_URL = os.environ.get("SELD_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "seldereeva_bot.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    DATABASE_URL = f"sqlite:///{db_path}"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def init_db():
    from . import models  # noqa: F401 — registers models on Base before create_all
    Base.metadata.create_all(bind=engine)
