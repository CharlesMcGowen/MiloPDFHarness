import json
from pathlib import Path

import pytest

from milo.benchmark import BenchmarkManifestError, _download, evaluate_conversion, load_manifest


def _manifest(url: str = "https://www.fdic.gov/report.pdf") -> dict:
    return {
        "suite": "test-suite",
        "allowed_hosts": ["www.fdic.gov"],
        "documents": [
            {
                "id": "fdic-test",
                "url": url,
                "filename": "report.pdf",
                "sha256": "a" * 64,
                "expected_pages": 2,
                "expected_document_type": "financial",
                "minimum_quality": 0.8,
                "maximum_credit_card_like": 0,
                "minimum_likely_visual_pages": 1,
                "minimum_adapter_items": {"camelot": 1},
                "required_patterns": [
                    {"id": "net_income", "pattern": "net income.*\\$70\\.6 billion"}
                ],
            }
        ],
    }


def test_manifest_restricts_download_hosts(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest("https://example.test/report.pdf")), encoding="utf-8")

    with pytest.raises(BenchmarkManifestError):
        load_manifest(path)


def test_download_rejects_redirect_to_disallowed_host(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    document = load_manifest(path).documents[0]

    class FakeResponse:
        history = ()
        url = "https://example.test/report.pdf"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("milo.benchmark.httpx.stream", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(BenchmarkManifestError, match="redirect uses a disallowed URL"):
        _download(
            document,
            tmp_path / "cache",
            allowed_hosts=("www.fdic.gov",),
            maximum_download_mb=1,
        )


def test_conversion_evaluation_checks_financial_facts(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    document = load_manifest(path).documents[0]
    conversion = {
        "page_count": 2,
        "document_type": "financial",
        "quality_score": 0.95,
        "content_detection": {
            "sensitive_data": {"counts": {"credit_card_like": 0}},
            "visual_content": {"likely_chart_or_figure_page_count": 1},
        },
    }
    payload = {"text": "Quarterly net income was $70.6 billion."}

    checks = evaluate_conversion(document, conversion, payload)

    assert all(item["passed"] for item in checks)
