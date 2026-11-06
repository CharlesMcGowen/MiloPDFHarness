from milo.quality import HeuristicQualityJudge


def test_quality_flags_sparse_text() -> None:
    result = HeuristicQualityJudge().assess("x", page_count=4)

    assert result.overall_quality < 0.5
    assert "very_little_text" in result.issues
    assert "sparse_pages" in result.issues


def test_quality_accepts_coherent_text() -> None:
    text = (
        "This technical report explains the implementation and architecture. "
        "The system extracts structured text from documents and evaluates its quality. "
    ) * 20
    result = HeuristicQualityJudge().assess(text, page_count=2)

    assert result.overall_quality > 0.7
    assert result.clarity_score > 0.9
