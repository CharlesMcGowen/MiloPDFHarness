from pathlib import Path

import pytest

from milo.config import Settings
from milo.converter import ExtractionCandidate, PDFConverter


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'milo.sqlite3'}",
        input_root=tmp_path / "input",
        output_root=tmp_path / "output",
        max_upload_mb=5,
        quality_threshold=0.5,
        max_reprocess_attempts=3,
        llm_base_url=None,
        llm_model=None,
        llm_api_key=None,
        api_token=None,
    )


@pytest.mark.asyncio
async def test_enhanced_conversion_selects_best_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-test")
    converter = PDFConverter(settings_for(tmp_path))
    weak = "x"
    strong = (
        "This report contains coherent technical implementation details and architecture. "
    ) * 30

    monkeypatch.setattr(
        converter,
        "_extract_pdfplumber",
        lambda path: ExtractionCandidate("pdfplumber", strong, 2),
    )
    monkeypatch.setattr(
        converter,
        "_extract_pymupdf",
        lambda path: ExtractionCandidate("pymupdf", weak, 10),
    )
    monkeypatch.setattr(
        converter,
        "_extract_pypdf",
        lambda path: ExtractionCandidate("pypdf", weak, 10),
    )

    result = await converter.convert(source, output_format="json", enhanced=True)

    assert result.selected_decoder == "pdfplumber"
    assert result.quality_score > 0.7
    assert Path(result.output_path).is_file()
    assert set(result.candidate_scores) == {"pdfplumber", "pymupdf", "pypdf"}


def test_sensitive_detection_never_returns_values() -> None:
    text = "Contact alice@example.test and use sk-" + "a" * 24
    result = PDFConverter._detect_sensitive_data(text)

    assert "email" in result["detected_categories"]
    assert "api_key_like" in result["detected_categories"]
    assert result["matched_values_included"] is False
    assert "alice@example.test" not in str(result)


def test_financial_years_are_not_credit_cards() -> None:
    text = "Chart years: 2019 2020 2021 2022. Test card: 4111 1111 1111 1111."
    result = PDFConverter._detect_sensitive_data(text)

    assert result["counts"]["credit_card_like"] == 1


def test_fdic_report_is_classified_as_financial() -> None:
    text = (
        "Quarterly Banking Profile. Net income increased while return on assets improved. "
        "The Deposit Insurance Fund reserve ratio and net interest margin also increased."
    )

    assert PDFConverter._classify_document(text) == "financial"
