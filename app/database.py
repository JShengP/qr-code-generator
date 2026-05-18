from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

# `check_same_thread=False` is required for SQLite under FastAPI's
# threadpool but is meaningless (and rejected) for Postgres / MySQL
# URLs, so we set it conditionally.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Tiny migration helper. `Base.metadata.create_all` is idempotent for
# tables but never adds columns to a table that already exists, so a
# pre-existing `qr_code.db` from before a new column would silently
# read NULL on every query and crash on inserts. We add the column
# on startup if it's missing.
#
# Real production would use Alembic. For a single-developer prototype
# with one SQLite file, a self-applying ALTER on boot is the smallest
# thing that keeps `python -m uvicorn ...` Just Working after a pull.
_PENDING_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "url_mappings": [
        ("deleted_at", "DATETIME"),
        # `DEFAULT 302` so existing rows backfill to the documented
        # default behaviour rather than NULL.
        ("redirect_status", "INTEGER NOT NULL DEFAULT 302"),
    ],
}


def apply_lightweight_migrations() -> None:
    """ADD COLUMN any missing columns listed in `_PENDING_COLUMNS`.

    SQLite-only. Postgres / MySQL deployments should run a real
    migration tool; we no-op there to avoid masking config drift.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _PENDING_COLUMNS.items():
            if not inspector.has_table(table):
                # Fresh schema — create_all() will add the column with
                # the right definition; we have nothing to migrate.
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, decl in columns:
                if name in existing:
                    continue
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {name} {decl}'))
