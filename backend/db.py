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
    _migrate()


def _migrate():
    """create_all() only adds missing tables, not missing columns on tables
    that already exist in production — so new columns need an explicit,
    idempotent ALTER here. Each statement is wrapped individually so an
    "already exists" failure on one doesn't block the rest."""
    from sqlalchemy import text
    bool_default = "0" if engine.dialect.name == "sqlite" else "false"
    statements = [
        f"ALTER TABLE users ADD COLUMN is_blocked BOOLEAN NOT NULL DEFAULT {bool_default}",
        "ALTER TABLE users ADD COLUMN access_until TIMESTAMP",
        "ALTER TABLE users ADD COLUMN max_cabinets INTEGER",
        "ALTER TABLE ozon_sales_cache ADD COLUMN accrual_entries JSON",
    ]
    with engine.connect() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()  # column already exists
