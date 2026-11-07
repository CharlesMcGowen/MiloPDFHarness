from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .quality import HeuristicQualityJudge, LLMQualityJudge, QualityAssessment


class PDFConversionError(RuntimeError):
    pass


@dataclass(slots=True)
class ExtractionCandidate:
    decoder: str
    text: str
    page_count: int
    pages: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assessment: QualityAssessment | None = None


@dataclass(frozen=True, slots=True)
class ConversionResult:
    filename: str
    output_path: str
    output_format: str
    fingerprint: str
    selected_decoder: str
    page_count: int
    text_length: int
    quality_score: float
    quality: dict[str, Any]
    candidate_scores: dict[str, float]
    document_type: str
    detected_language: str | None
    content_detection: dict[str, Any]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["warnings"] = list(self.warnings)
        return result


class PDFConverter:
    DECODERS = ("pdfplumber", "pymupdf", "pypdf")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.heuristic = HeuristicQualityJudge()
        self.llm: LLMQualityJudge | None = None
        if settings.llm_base_url and settings.llm_model:
            self.llm = LLMQualityJudge(
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                api_key=settings.llm_api_key,
            )

    async def convert(
        self,
        pdf_path: str | Path,
        *,
        output_format: str = "markdown",
        output_path: str | Path | None = None,
        enhanced: bool = False,
        preferred_decoders: list[str] | None = None,
        use_llm_judge: bool = False,
    ) -> ConversionResult:
        path = Path(pdf_path).expanduser().resolve()
        self._validate_pdf(path)
        output_format = output_format.lower()
        if output_format not in {"markdown", "json"}:
            raise ValueError("output_format must be markdown or json")

        ordered = self._decoder_order(preferred_decoders)
        candidates = self._extract_candidates(path, ordered, enhanced=enhanced)
        if not candidates:
            raise PDFConversionError("no installed decoder could extract text from the PDF")

        for candidate in candidates:
            candidate.assessment = self.heuristic.assess(
                candidate.text, page_count=candidate.page_count
            )
        selected = max(
            candidates,
            key=lambda item: item.assessment.overall_quality if item.assessment else 0,
        )

        offline_quality = selected.assessment or self.heuristic.assess("")
        final_quality = offline_quality
        warnings = list(selected.warnings)
        quality_details: dict[str, Any] = {"offline": offline_quality.as_dict()}

        if use_llm_judge:
            if self.llm is None:
                warnings.append("llm_quality_judge_not_configured")
            else:
                try:
                    llm_quality = await self.llm.assess(
                        selected.text, page_count=selected.page_count
                    )
                    quality_details["llm"] = llm_quality.as_dict()
                    final_quality = QualityAssessment(
                        overall_quality=round(
                            (offline_quality.overall_quality + llm_quality.overall_quality) / 2,
                            4,
                        ),
                        relevance_score=llm_quality.relevance_score,
                        completeness_score=round(
                            (offline_quality.completeness_score + llm_quality.completeness_score)
                            / 2,
                            4,
                        ),
                        clarity_score=round(
                            (offline_quality.clarity_score + llm_quality.clarity_score) / 2,
                            4,
                        ),
                        issues=tuple(dict.fromkeys(offline_quality.issues + llm_quality.issues)),
                        judge="offline_and_configured_llm",
                    )
                except Exception:
                    warnings.append("llm_quality_judge_failed")

        fingerprint = self._sha256(path)
        document_type = self._classify_document(selected.text)
        language = self._detect_language(selected.text)
        content_detection = {
            "redactions": self._detect_redactions(selected.text),
            "sensitive_data": self._detect_sensitive_data(selected.text),
            "visual_content": self._detect_visual_content(path, selected.pages),
        }
        if content_detection["visual_content"]["likely_chart_or_figure_page_count"]:
            warnings.append("likely_chart_or_figure_pages_require_structured_extraction")
        candidate_scores = {
            item.decoder: item.assessment.overall_quality for item in candidates if item.assessment
        }

        destination = self._destination(path, output_format, output_path)
        if output_format == "markdown":
            rendered = self._render_markdown(path.name, selected, final_quality, content_detection)
        else:
            rendered = json.dumps(
                {
                    "metadata": {
                        "filename": path.name,
                        "source_sha256": fingerprint,
                        "extracted_at": datetime.now(UTC).isoformat(),
                        "selected_decoder": selected.decoder,
                        "page_count": selected.page_count,
                        "text_length": len(selected.text),
                        "document_type": document_type,
                        "language": language,
                    },
                    "quality": final_quality.as_dict(),
                    "candidate_scores": candidate_scores,
                    "content_detection": content_detection,
                    "pages": selected.pages,
                    "text": selected.text,
                },
                indent=2,
                ensure_ascii=False,
            )
        self._atomic_write(destination, rendered)

        return ConversionResult(
            filename=path.name,
            output_path=str(destination),
            output_format=output_format,
            fingerprint=fingerprint,
            selected_decoder=selected.decoder,
            page_count=selected.page_count,
            text_length=len(selected.text),
            quality_score=final_quality.overall_quality,
            quality={**quality_details, "combined": final_quality.as_dict()},
            candidate_scores=candidate_scores,
            document_type=document_type,
            detected_language=language,
            content_detection=content_detection,
            warnings=tuple(warnings),
        )

    def _extract_candidates(
        self, path: Path, ordered: list[str], *, enhanced: bool
    ) -> list[ExtractionCandidate]:
        candidates: list[ExtractionCandidate] = []
        errors: list[str] = []
        for decoder in ordered:
            try:
                candidate = getattr(self, f"_extract_{decoder}")(path)
                if candidate.text.strip():
                    candidates.append(candidate)
                    if not enhanced:
                        break
            except (ImportError, ModuleNotFoundError):
                continue
            except Exception:
                errors.append(f"{decoder}_failed")
        if not candidates and errors:
            raise PDFConversionError("all installed decoders failed")
        return candidates

    @staticmethod
    def _decoder_order(preferred: list[str] | None) -> list[str]:
        requested = [item for item in (preferred or []) if item in PDFConverter.DECODERS]
        return list(dict.fromkeys(requested + list(PDFConverter.DECODERS)))

    @staticmethod
    def _extract_pdfplumber(path: Path) -> ExtractionCandidate:
        import pdfplumber

        pages: list[dict[str, Any]] = []
        with pdfplumber.open(path) as document:
            for index, page in enumerate(document.pages, start=1):
                text = page.extract_text() or ""
                pages.append({"page_number": index, "text": text})
        return ExtractionCandidate(
            decoder="pdfplumber",
            text="\n\n".join(page["text"] for page in pages if page["text"]),
            page_count=len(pages),
            pages=pages,
        )

    @staticmethod
    def _extract_pymupdf(path: Path) -> ExtractionCandidate:
        import pymupdf

        pages: list[dict[str, Any]] = []
        with pymupdf.open(path) as document:
            for index, page in enumerate(document, start=1):
                pages.append({"page_number": index, "text": page.get_text() or ""})
        return ExtractionCandidate(
            decoder="pymupdf",
            text="\n\n".join(page["text"] for page in pages if page["text"]),
            page_count=len(pages),
            pages=pages,
        )

    @staticmethod
    def _extract_pypdf(path: Path) -> ExtractionCandidate:
        from pypdf import PdfReader

        document = PdfReader(str(path))
        pages = [
            {"page_number": index, "text": page.extract_text() or ""}
            for index, page in enumerate(document.pages, start=1)
        ]
        return ExtractionCandidate(
            decoder="pypdf",
            text="\n\n".join(page["text"] for page in pages if page["text"]),
            page_count=len(pages),
            pages=pages,
        )

    @staticmethod
    def _validate_pdf(path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.suffix.lower() != ".pdf":
            raise ValueError("input must use a .pdf extension")
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError("input does not have a PDF signature")

    def _destination(
        self, source: Path, output_format: str, output_path: str | Path | None
    ) -> Path:
        suffix = ".md" if output_format == "markdown" else ".json"
        if output_path is not None:
            destination = Path(output_path).expanduser().resolve()
        else:
            self.settings.output_root.mkdir(parents=True, exist_ok=True)
            destination = self.settings.output_root / f"{source.stem}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    @staticmethod
    def _atomic_write(destination: Path, content: str) -> None:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(destination)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _detect_redactions(text: str) -> dict[str, Any]:
        markers = len(re.findall(r"\[(?:redacted|removed)\]|█{2,}", text, re.IGNORECASE))
        return {"possible_redaction_markers": markers, "detected": markers > 0}

    @staticmethod
    def _detect_sensitive_data(text: str) -> dict[str, Any]:
        patterns = {
            "email": r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            "api_key_like": r"\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,})\b",
            "private_key_header": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        }
        counts = {
            name: len(re.findall(pattern, text, re.IGNORECASE))
            for name, pattern in patterns.items()
        }
        counts["credit_card_like"] = sum(
            PDFConverter._is_credit_card_candidate(match.group(0))
            for match in re.finditer(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", text)
        )
        return {
            "detected_categories": sorted(name for name, count in counts.items() if count),
            "counts": counts,
            "matched_values_included": False,
        }

    @staticmethod
    def _is_credit_card_candidate(value: str) -> bool:
        stripped = value.strip()
        digits = re.sub(r"[ -]", "", stripped)
        if not digits.isdigit() or not 13 <= len(digits) <= 19:
            return False
        groups = [item for item in re.split(r"[ -]+", stripped) if item]
        if len(groups) > 1:
            allowed_shapes = {(4, 4, 4, 4), (4, 6, 5), (4, 4, 4, 3)}
            if tuple(map(len, groups)) not in allowed_shapes:
                return False
            if all(len(group) == 4 and 1900 <= int(group) <= 2099 for group in groups):
                return False
        checksum = 0
        parity = len(digits) % 2
        for index, character in enumerate(digits):
            number = int(character)
            if index % 2 == parity:
                number *= 2
                if number > 9:
                    number -= 9
            checksum += number
        return checksum % 10 == 0

    @staticmethod
    def _classify_document(text: str) -> str:
        sample = text[:40_000].lower()
        categories = {
            "financial": {
                "quarterly banking profile": 5,
                "deposit insurance fund": 4,
                "return on assets": 4,
                "balance sheet": 3,
                "cash flow": 3,
                "net income": 3,
                "net interest margin": 3,
                "revenue": 2,
                "fiscal": 2,
                "loans": 1,
                "deposits": 1,
            },
            "research_paper": {
                "abstract": 3,
                "methodology": 2,
                "references": 2,
                "conclusion": 1,
            },
            "legal": {"agreement": 2, "party": 1, "whereas": 3, "liability": 1},
            "technical": {
                "api": 2,
                "configuration": 2,
                "implementation": 2,
                "architecture": 2,
            },
            "report": {"executive summary": 3, "findings": 2, "recommendations": 2},
        }
        scored = {
            name: sum(weight for term, weight in terms.items() if term in sample)
            for name, terms in categories.items()
        }
        winner = max(scored, key=scored.get)
        return winner if scored[winner] else "unknown"

    @staticmethod
    def _detect_visual_content(path: Path, pages: list[dict[str, Any]]) -> dict[str, Any]:
        page_text = {int(page.get("page_number", 0)): str(page.get("text", "")) for page in pages}
        likely_pages: list[int] = []
        image_pages = 0
        drawing_heavy_pages = 0
        try:
            import pymupdf

            with pymupdf.open(path) as document:
                for page_number, page in enumerate(document, start=1):
                    image_count = len(page.get_images(full=True))
                    drawing_count = len(page.get_drawings())
                    text_length = len(page_text.get(page_number, "").strip())
                    if image_count:
                        image_pages += 1
                    if drawing_count >= 20:
                        drawing_heavy_pages += 1
                    if (image_count and text_length < 1_200) or (
                        drawing_count >= 20 and text_length < 2_000
                    ):
                        likely_pages.append(page_number)
        except Exception:
            return {
                "analysis_available": False,
                "image_page_count": 0,
                "drawing_heavy_page_count": 0,
                "likely_chart_or_figure_page_count": 0,
                "likely_chart_or_figure_pages": [],
            }
        return {
            "analysis_available": True,
            "image_page_count": image_pages,
            "drawing_heavy_page_count": drawing_heavy_pages,
            "likely_chart_or_figure_page_count": len(likely_pages),
            "likely_chart_or_figure_pages": likely_pages[:200],
        }

    @staticmethod
    def _detect_language(text: str) -> str | None:
        words = re.findall(r"\b[a-zA-ZÀ-ÿ]+\b", text[:20_000].lower())
        if not words:
            return None
        sets = {
            "en": {"the", "and", "of", "to", "in", "is", "for", "that"},
            "es": {"el", "la", "de", "que", "y", "en", "un", "es"},
            "fr": {"le", "la", "de", "et", "les", "des", "un", "une"},
        }
        counts = {
            language: sum(word in markers for word in words) for language, markers in sets.items()
        }
        language = max(counts, key=counts.get)
        return language if counts[language] >= 3 else None

    @staticmethod
    def _render_markdown(
        filename: str,
        candidate: ExtractionCandidate,
        quality: QualityAssessment,
        content_detection: dict[str, Any],
    ) -> str:
        title = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
        metadata = (
            f"<!-- decoder: {candidate.decoder}; pages: {candidate.page_count}; "
            f"quality: {quality.overall_quality:.4f}; "
            f"sensitive-categories: "
            f"{','.join(content_detection['sensitive_data']['detected_categories']) or 'none'} -->"
        )
        return f"# {title}\n\n{metadata}\n\n{candidate.text.strip()}\n"
