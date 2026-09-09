import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    # Local dev fallback — Railway provides DATABASE_URL once Postgres is linked.
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "app.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    DATABASE_URL = f"sqlite:///{db_path}"

# Railway (and most providers) hand out "postgres://", but SQLAlchemy 1.4+/2.0 only
# accepts the "postgresql://" scheme.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def init_db():
    from . import models  # noqa: F401 — registers models on Base before create_all
    Base.metadata.create_all(bind=engine)
