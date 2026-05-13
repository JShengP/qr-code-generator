from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def _utc_now_naive() -> datetime:
    """Naive UTC `now` for column defaults.

    `datetime.utcnow` is deprecated in Python 3.12+. We keep the value
    naive because the columns are declared without `timezone=True`; the
    application code in routes.py uses the same convention so all
    comparisons stay on the naive side of the line.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UrlMapping(Base):
    __tablename__ = "url_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(8), unique=True, nullable=False, index=True)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class ScanEvent(Base):
    __tablename__ = "scan_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(8), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    __table_args__ = (Index("idx_token_scanned", "token", "scanned_at"),)
