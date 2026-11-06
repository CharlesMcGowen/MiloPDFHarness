from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import sessionmaker

from .db import DecoderOutcome, FeedbackRecord, create_session_factory, utcnow


class FeedbackStore:
    def __init__(
        self,
        database_url: str,
        *,
        quality_threshold: float = 0.5,
        max_reprocess_attempts: int = 3,
    ) -> None:
        self.sessions: sessionmaker = create_session_factory(database_url)
        self.quality_threshold = quality_threshold
        self.max_reprocess_attempts = max_reprocess_attempts

    def record_feedback(
        self,
        *,
        document_path: str | Path,
        feedback_type: str,
        feedback_reason: str,
        quality_rating: float,
        feedback_source: str = "operator",
        relevance_score: float | None = None,
        chunk_id: str | None = None,
        query_used: str | None = None,
        prompt_context: str | None = None,
    ) -> FeedbackRecord:
        quality_rating = self._score(quality_rating)
        if relevance_score is not None:
            relevance_score = self._score(relevance_score)
        path = Path(document_path).expanduser().resolve()
        status = "pending" if quality_rating < self.quality_threshold else "accepted"
        record = FeedbackRecord(
            document_path=str(path),
            document_filename=path.name,
            chunk_id=chunk_id,
            feedback_type=feedback_type[:50],
            feedback_reason=feedback_reason[:4000],
            feedback_source=feedback_source[:100],
            quality_rating=quality_rating,
            relevance_score=relevance_score,
            query_used=self._bounded(query_used),
            prompt_context=self._bounded(prompt_context),
            status=status,
        )
        with self.sessions.begin() as session:
            session.add(record)
        return record

    def pending(self, *, limit: int = 10) -> list[FeedbackRecord]:
        statement = (
            select(FeedbackRecord)
            .where(
                FeedbackRecord.status == "pending",
                FeedbackRecord.quality_rating < self.quality_threshold,
                FeedbackRecord.reprocess_attempts < self.max_reprocess_attempts,
            )
            .order_by(FeedbackRecord.quality_rating, FeedbackRecord.created_at)
            .limit(max(1, min(limit, 100)))
        )
        with self.sessions() as session:
            return list(session.scalars(statement).all())

    def claim(self, record_id: str) -> FeedbackRecord | None:
        with self.sessions.begin() as session:
            record = session.get(FeedbackRecord, record_id)
            if (
                record is None
                or record.status != "pending"
                or record.reprocess_attempts >= self.max_reprocess_attempts
            ):
                return None
            record.status = "reprocessing"
            record.reprocess_attempts += 1
            record.last_reprocessed_at = utcnow()
            record.updated_at = utcnow()
            session.flush()
            session.expunge(record)
            return record

    def complete_attempt(
        self,
        record_id: str,
        *,
        success: bool,
        quality_score: float | None = None,
        decoder: str | None = None,
        error: str | None = None,
    ) -> FeedbackRecord:
        with self.sessions.begin() as session:
            record = session.get(FeedbackRecord, record_id)
            if record is None:
                raise KeyError(record_id)
            if quality_score is not None:
                record.quality_rating = self._score(quality_score)
            record.reprocess_algorithm = decoder[:100] if decoder else None
            record.last_error = error[:1000] if error else None
            record.updated_at = utcnow()

            if success and record.quality_rating >= self.quality_threshold:
                record.status = "reprocessed"
            elif record.reprocess_attempts >= self.max_reprocess_attempts:
                record.status = "failed"
            else:
                record.status = "pending"
            session.flush()
            session.expunge(record)
            return record

    def record_decoder_outcomes(
        self,
        *,
        document_fingerprint: str,
        document_type: str,
        page_count: int,
        scores: dict[str, float],
    ) -> None:
        rows = [
            DecoderOutcome(
                document_fingerprint=document_fingerprint,
                document_type=document_type[:50] or "unknown",
                decoder=decoder[:50],
                quality_score=self._score(score),
                success=score > 0,
                page_count=max(page_count, 0),
            )
            for decoder, score in scores.items()
        ]
        if not rows:
            return
        with self.sessions.begin() as session:
            session.add_all(rows)

    def decoder_rankings(self, *, document_type: str | None = None) -> list[dict[str, Any]]:
        success_value = case((DecoderOutcome.success.is_(True), 1.0), else_=0.0)
        statement = select(
            DecoderOutcome.decoder,
            func.count(DecoderOutcome.id).label("samples"),
            func.avg(DecoderOutcome.quality_score).label("average_quality"),
            func.avg(success_value).label("success_rate"),
        )
        if document_type:
            statement = statement.where(DecoderOutcome.document_type == document_type)
        statement = statement.group_by(DecoderOutcome.decoder).order_by(
            func.avg(DecoderOutcome.quality_score).desc(),
            func.count(DecoderOutcome.id).desc(),
        )
        with self.sessions() as session:
            rows = session.execute(statement).all()
        return [
            {
                "decoder": row.decoder,
                "samples": int(row.samples),
                "average_quality": round(float(row.average_quality or 0), 4),
                "success_rate": round(float(row.success_rate or 0), 4),
            }
            for row in rows
        ]

    def preferred_decoders(self, *, document_type: str | None = None) -> list[str]:
        return [row["decoder"] for row in self.decoder_rankings(document_type=document_type)]

    def stats(self) -> dict[str, Any]:
        with self.sessions() as session:
            status_rows = session.execute(
                select(FeedbackRecord.status, func.count(FeedbackRecord.id)).group_by(
                    FeedbackRecord.status
                )
            ).all()
            average = session.scalar(select(func.avg(FeedbackRecord.quality_rating)))
            outcomes = session.scalar(select(func.count(DecoderOutcome.id)))
        return {
            "feedback_total": sum(int(row[1]) for row in status_rows),
            "feedback_by_status": {row[0]: int(row[1]) for row in status_rows},
            "average_quality": round(float(average or 0), 4),
            "decoder_outcomes": int(outcomes or 0),
            "quality_threshold": self.quality_threshold,
            "max_reprocess_attempts": self.max_reprocess_attempts,
        }

    @staticmethod
    def _score(value: float) -> float:
        return round(max(0.0, min(1.0, float(value))), 4)

    @staticmethod
    def _bounded(value: str | None, limit: int = 12_000) -> str | None:
        return value[:limit] if value else None


def feedback_to_dict(record: FeedbackRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "document_filename": record.document_filename,
        "feedback_type": record.feedback_type,
        "feedback_reason": record.feedback_reason,
        "feedback_source": record.feedback_source,
        "quality_rating": record.quality_rating,
        "relevance_score": record.relevance_score,
        "status": record.status,
        "reprocess_attempts": record.reprocess_attempts,
        "reprocess_algorithm": record.reprocess_algorithm,
        "last_error": record.last_error,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }
