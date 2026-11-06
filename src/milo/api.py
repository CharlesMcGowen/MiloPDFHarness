from __future__ import annotations

import re
import secrets
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .config import Settings
from .converter import PDFConverter
from .feedback import FeedbackStore, feedback_to_dict
from .worker import ReprocessingWorker

_BEARER = HTTPBearer(auto_error=False)


class FeedbackRequest(BaseModel):
    stored_document: str = Field(min_length=1, max_length=300)
    feedback_type: str = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=4000)
    quality_score: float = Field(ge=0, le=1)
    relevance_score: float | None = Field(default=None, ge=0, le=1)


class ReprocessRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=100)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_runtime_dirs()
    store = FeedbackStore(
        settings.database_url,
        quality_threshold=settings.quality_threshold,
        max_reprocess_attempts=settings.max_reprocess_attempts,
    )
    converter = PDFConverter(settings)
    worker = ReprocessingWorker(settings=settings, store=store, converter=converter)
    app = FastAPI(
        title="Milo PDF Loop",
        version="0.1.0",
        description="Local-first PDF extraction and adaptive reprocessing",
    )
    app.state.settings = settings
    app.state.store = store
    app.state.converter = converter
    app.state.worker = worker

    async def authorize(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> None:
        if settings.api_token:
            supplied = credentials.credentials if credentials else ""
            if not secrets.compare_digest(supplied, settings.api_token):
                raise HTTPException(status_code=401, detail="invalid bearer token")
            return
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(
                status_code=403,
                detail="unauthenticated access is restricted to loopback",
            )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "milo-pdf-loop",
            "version": "0.1.0",
            "llm_quality_judge_enabled": bool(settings.llm_base_url and settings.llm_model),
            "api_auth_enabled": bool(settings.api_token),
        }

    @app.post("/v1/convert", dependencies=[Depends(authorize)])
    async def convert_pdf(
        upload: Annotated[UploadFile, File()],
        output_format: Annotated[str, Form()] = "json",
        enhanced: Annotated[bool, Form()] = True,
        use_llm_judge: Annotated[bool, Form()] = False,
    ) -> dict[str, object]:
        safe_name = _safe_pdf_name(upload.filename or "document.pdf")
        stored_name = f"{uuid.uuid4()}-{safe_name}"
        destination = settings.input_root / stored_name
        temporary = settings.input_root / f".{stored_name}.upload"
        size = 0
        first = True
        try:
            with temporary.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    if first:
                        first = False
                        if not chunk.startswith(b"%PDF-"):
                            raise HTTPException(status_code=400, detail="invalid PDF signature")
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(status_code=413, detail="PDF exceeds upload limit")
                    handle.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="empty upload")
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

        try:
            result = await converter.convert(
                destination,
                output_format=output_format,
                enhanced=enhanced,
                preferred_decoders=store.preferred_decoders(),
                use_llm_judge=use_llm_judge,
            )
            store.record_decoder_outcomes(
                document_fingerprint=result.fingerprint,
                document_type=result.document_type,
                page_count=result.page_count,
                scores=result.candidate_scores,
            )
            feedback = None
            if result.quality_score < settings.quality_threshold:
                feedback = store.record_feedback(
                    document_path=destination,
                    feedback_type="low_quality",
                    feedback_reason="automatic extraction quality threshold",
                    quality_rating=result.quality_score,
                    feedback_source="milo_api",
                )
            response = result.as_dict()
            response["stored_document"] = stored_name
            response["output_path"] = Path(result.output_path).name
            response["feedback_id"] = feedback.id if feedback else None
            return response
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    @app.post("/v1/feedback", dependencies=[Depends(authorize)])
    async def record_feedback(body: FeedbackRequest) -> dict[str, object]:
        document = _managed_document(settings.input_root, body.stored_document)
        if not document.is_file():
            raise HTTPException(status_code=404, detail="stored document not found")
        record = store.record_feedback(
            document_path=document,
            feedback_type=body.feedback_type,
            feedback_reason=body.reason,
            quality_rating=body.quality_score,
            relevance_score=body.relevance_score,
            feedback_source="api",
        )
        return feedback_to_dict(record)

    @app.post("/v1/reprocess", dependencies=[Depends(authorize)])
    async def reprocess(body: ReprocessRequest) -> dict[str, object]:
        return await worker.process_pending(limit=body.limit)

    @app.get("/v1/stats", dependencies=[Depends(authorize)])
    async def stats() -> dict[str, object]:
        return store.stats()

    @app.get("/v1/decoders", dependencies=[Depends(authorize)])
    async def decoders() -> list[dict[str, object]]:
        return store.decoder_rankings()

    return app


def _safe_pdf_name(filename: str) -> str:
    name = Path(filename).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip(".-") or "document"
    return f"{stem[:180]}.pdf"


def _managed_document(root: Path, name: str) -> Path:
    if Path(name).name != name:
        raise HTTPException(status_code=400, detail="invalid stored document name")
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid stored document path") from exc
    return candidate
