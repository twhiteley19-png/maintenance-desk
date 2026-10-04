"""SQLAlchemy engine + session factory."""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


def get_session():
    return SessionLocal()


# New columns added after Phase 0. SQLite has no real ALTER COLUMN, so for
# existing databases we ADD COLUMN when it's missing; create_all handles
# fresh databases.
_NEW_COLUMNS = {
    "after_hours_hold": "INTEGER DEFAULT 0",
    "emergency_acknowledged": "INTEGER DEFAULT 0",
    "last_alert_at": "DATETIME",
    "vendor_dispatch_sent_at": "DATETIME",
    "vendor_confirmed": "INTEGER DEFAULT 0",
    "tried_vendor_ids": "TEXT DEFAULT ''",
    "fallback_count": "INTEGER DEFAULT 0",
    "fix_check_due_at": "DATETIME",
    "fix_check_sent_at": "DATETIME",
    "fix_check_reply": "VARCHAR(10)",
}


def init_db():
    from . import models  # noqa: F401  (registers models on Base)

    Base.metadata.create_all(bind=engine)
    if DATABASE_URL.startswith("sqlite"):
        existing = {c["name"] for c in inspect(engine).get_columns("requests")}
        with engine.begin() as conn:
            for name, ddl in _NEW_COLUMNS.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE requests ADD COLUMN {name} {ddl}"))
