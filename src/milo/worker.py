from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .converter import PDFConverter
from .feedback import FeedbackStore


class ReprocessingWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        store: FeedbackStore,
        converter: PDFConverter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.converter = converter

    async def process_pending(self, *, limit: int = 10) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for pending in self.store.pending(limit=limit):
            claimed = self.store.claim(pending.id)
            if claimed is None:
                continue
            source = Path(claimed.document_path).expanduser().resolve()
            if not self._is_managed_input(source):
                completed = self.store.complete_attempt(
                    claimed.id,
                    success=False,
                    error="document is outside the managed input directory",
                )
                results.append(
                    {
                        "feedback_id": claimed.id,
                        "status": completed.status,
                        "success": False,
                        "error": "unmanaged_document_path",
                    }
                )
                continue

            try:
                destination = (
                    self.settings.output_root
                    / f"{source.stem}.reprocessed-{claimed.reprocess_attempts}.md"
                )
                conversion = await self.converter.convert(
                    source,
                    output_format="markdown",
                    output_path=destination,
                    enhanced=True,
                    preferred_decoders=self.store.preferred_decoders(),
                    use_llm_judge=bool(self.settings.llm_base_url),
                )
                self.store.record_decoder_outcomes(
                    document_fingerprint=conversion.fingerprint,
                    document_type=conversion.document_type,
                    page_count=conversion.page_count,
                    scores=conversion.candidate_scores,
                )
                completed = self.store.complete_attempt(
                    claimed.id,
                    success=True,
                    quality_score=conversion.quality_score,
                    decoder=conversion.selected_decoder,
                )
                results.append(
                    {
                        "feedback_id": claimed.id,
                        "status": completed.status,
                        "success": True,
                        "quality_score": conversion.quality_score,
                        "decoder": conversion.selected_decoder,
                        "output_path": conversion.output_path,
                    }
                )
            except Exception as exc:
                completed = self.store.complete_attempt(
                    claimed.id,
                    success=False,
                    error=type(exc).__name__,
                )
                results.append(
                    {
                        "feedback_id": claimed.id,
                        "status": completed.status,
                        "success": False,
                        "error": type(exc).__name__,
                    }
                )

        return {
            "processed": len(results),
            "successful": sum(item["success"] for item in results),
            "results": results,
        }

    def _is_managed_input(self, path: Path) -> bool:
        try:
            path.relative_to(self.settings.input_root)
            return path.is_file()
        except ValueError:
            return False
