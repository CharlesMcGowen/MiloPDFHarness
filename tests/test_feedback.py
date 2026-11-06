from pathlib import Path

from milo.feedback import FeedbackStore


def store_for(tmp_path: Path) -> FeedbackStore:
    return FeedbackStore(
        f"sqlite:///{tmp_path / 'milo.sqlite3'}",
        quality_threshold=0.5,
        max_reprocess_attempts=3,
    )


def test_feedback_reprocessing_state_machine(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    document = tmp_path / "document.pdf"
    document.write_bytes(b"%PDF-test")
    record = store.record_feedback(
        document_path=document,
        feedback_type="low_quality",
        feedback_reason="fragmented extraction",
        quality_rating=0.2,
    )

    assert record.status == "pending"
    claimed = store.claim(record.id)
    assert claimed is not None
    assert claimed.reprocess_attempts == 1

    pending = store.complete_attempt(
        record.id,
        success=True,
        quality_score=0.4,
        decoder="pypdf",
    )
    assert pending.status == "pending"

    claimed_again = store.claim(record.id)
    assert claimed_again is not None
    completed = store.complete_attempt(
        record.id,
        success=True,
        quality_score=0.9,
        decoder="pdfplumber",
    )
    assert completed.status == "reprocessed"


def test_decoder_rankings_learn_from_outcomes(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    store.record_decoder_outcomes(
        document_fingerprint="a" * 64,
        document_type="technical",
        page_count=2,
        scores={"pypdf": 0.4, "pdfplumber": 0.9},
    )
    store.record_decoder_outcomes(
        document_fingerprint="b" * 64,
        document_type="technical",
        page_count=3,
        scores={"pypdf": 0.5, "pdfplumber": 0.8},
    )

    rankings = store.decoder_rankings(document_type="technical")
    assert rankings[0]["decoder"] == "pdfplumber"
    assert store.preferred_decoders(document_type="technical")[0] == "pdfplumber"
