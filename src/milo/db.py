from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class FeedbackRecord(Base):
    __tablename__ = "milo_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_path: Mapped[str] = mapped_column(Text, nullable=False)
    document_filename: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    chunk_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    feedback_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    feedback_reason: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_source: Mapped[str] = mapped_column(String(100), nullable=False, default="operator")
    quality_rating: Mapped[float] = mapped_column(Float, nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    query_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    reprocess_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reprocessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reprocess_algorithm: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class DecoderOutcome(Base):
    __tablename__ = "milo_decoder_outcomes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="unknown", index=True
    )
    decoder: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


def _prepare_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(database_url: str) -> Engine:
    _prepare_sqlite_parent(database_url)
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if make_url(database_url).drivername.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)


def create_session_factory(database_url: str) -> sessionmaker:
    engine = create_db_engine(database_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
