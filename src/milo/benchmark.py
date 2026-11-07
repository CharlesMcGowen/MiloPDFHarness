from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .adapters import run_adapters
from .converter import PDFConverter


class BenchmarkManifestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BenchmarkDocument:
    identifier: str
    url: str
    filename: str
    sha256: str
    expected_pages: int
    expected_document_type: str
    minimum_quality: float
    maximum_credit_card_like: int
    minimum_likely_visual_pages: int
    minimum_adapter_items: dict[str, int]
    required_patterns: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    suite: str
    allowed_hosts: tuple[str, ...]
    documents: tuple[BenchmarkDocument, ...]


def load_manifest(path: str | Path) -> BenchmarkManifest:
    manifest_path = Path(path).expanduser().resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise BenchmarkManifestError("manifest must contain a JSON object")
    suite = str(payload.get("suite", "")).strip()
    allowed_hosts = tuple(str(item).lower() for item in payload.get("allowed_hosts", []))
    raw_documents = payload.get("documents")
    if not suite or not allowed_hosts or not isinstance(raw_documents, list) or not raw_documents:
        raise BenchmarkManifestError("suite, allowed_hosts, and documents are required")

    documents: list[BenchmarkDocument] = []
    identifiers: set[str] = set()
    for raw in raw_documents:
        if not isinstance(raw, dict):
            raise BenchmarkManifestError("each document must be an object")
        identifier = str(raw.get("id", "")).strip()
        url = str(raw.get("url", "")).strip()
        filename = str(raw.get("filename", "")).strip()
        sha256 = str(raw.get("sha256", "")).strip().lower()
        if not identifier or identifier in identifiers:
            raise BenchmarkManifestError("document ids must be unique and non-empty")
        if not _url_is_allowed(url, allowed_hosts):
            raise BenchmarkManifestError(f"document {identifier} uses a disallowed URL")
        if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
            raise BenchmarkManifestError(f"document {identifier} has an unsafe filename")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise BenchmarkManifestError(f"document {identifier} has an invalid sha256")
        patterns = raw.get("required_patterns", [])
        if not isinstance(patterns, list) or not patterns:
            raise BenchmarkManifestError(f"document {identifier} needs required_patterns")
        normalized_patterns = tuple(
            {"id": str(item["id"]), "pattern": str(item["pattern"])} for item in patterns
        )
        documents.append(
            BenchmarkDocument(
                identifier=identifier,
                url=url,
                filename=filename,
                sha256=sha256,
                expected_pages=int(raw["expected_pages"]),
                expected_document_type=str(raw.get("expected_document_type", "financial")),
                minimum_quality=float(raw.get("minimum_quality", 0.85)),
                maximum_credit_card_like=int(raw.get("maximum_credit_card_like", 0)),
                minimum_likely_visual_pages=int(raw.get("minimum_likely_visual_pages", 1)),
                minimum_adapter_items={
                    str(name): int(count)
                    for name, count in raw.get("minimum_adapter_items", {}).items()
                },
                required_patterns=normalized_patterns,
            )
        )
        identifiers.add(identifier)
    return BenchmarkManifest(suite=suite, allowed_hosts=allowed_hosts, documents=tuple(documents))


async def run_benchmark(
    *,
    converter: PDFConverter,
    manifest: BenchmarkManifest,
    cache_root: str | Path,
    output_root: str | Path,
    adapter_names: tuple[str, ...] = (),
    maximum_download_mb: int = 100,
) -> dict[str, Any]:
    cache = Path(cache_root).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for document in manifest.documents:
        source = _download(
            document,
            cache,
            allowed_hosts=manifest.allowed_hosts,
            maximum_download_mb=maximum_download_mb,
        )
        destination = output / f"{document.identifier}.json"
        started = time.perf_counter()
        conversion = await converter.convert(
            source,
            output_format="json",
            output_path=destination,
            enhanced=True,
        )
        elapsed = time.perf_counter() - started
        payload = json.loads(destination.read_text(encoding="utf-8"))
        checks = evaluate_conversion(document, conversion.as_dict(), payload)
        adapter_results = run_adapters(
            source,
            names=adapter_names,
            output_root=output / "adapters",
        )
        for adapter_result in adapter_results:
            minimum_items = document.minimum_adapter_items.get(adapter_result["adapter"], 0)
            adapter_result["minimum_item_count"] = minimum_items
            adapter_result["passed"] = (
                adapter_result["status"] == "ok"
                and int(adapter_result["item_count"]) >= minimum_items
            )
        adapters_passed = all(item["passed"] for item in adapter_results)
        results.append(
            {
                "id": document.identifier,
                "filename": document.filename,
                "passed": all(item["passed"] for item in checks)
                and (adapters_passed or not adapter_names),
                "elapsed_seconds": round(elapsed, 3),
                "selected_decoder": conversion.selected_decoder,
                "quality_score": conversion.quality_score,
                "candidate_scores": conversion.candidate_scores,
                "checks": checks,
                "adapters": adapter_results,
            }
        )

    return {
        "suite": manifest.suite,
        "passed": all(item["passed"] for item in results),
        "document_count": len(results),
        "adapters_requested": list(adapter_names),
        "documents": results,
    }


def evaluate_conversion(
    document: BenchmarkDocument,
    conversion: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    text = str(payload.get("text", ""))
    content = conversion.get("content_detection", {})
    sensitive = content.get("sensitive_data", {})
    visual = content.get("visual_content", {})
    checks = [
        _check("page_count", conversion.get("page_count") == document.expected_pages),
        _check(
            "document_type",
            conversion.get("document_type") == document.expected_document_type,
        ),
        _check(
            "minimum_quality",
            float(conversion.get("quality_score", 0)) >= document.minimum_quality,
        ),
        _check(
            "credit_card_false_positives",
            int(sensitive.get("counts", {}).get("credit_card_like", 0))
            <= document.maximum_credit_card_like,
        ),
        _check(
            "visual_page_detection",
            int(visual.get("likely_chart_or_figure_page_count", 0))
            >= document.minimum_likely_visual_pages,
        ),
    ]
    for expected in document.required_patterns:
        matched = re.search(expected["pattern"], text, flags=re.IGNORECASE | re.DOTALL) is not None
        checks.append(_check(f"fact:{expected['id']}", matched))
    return checks


def write_report(path: str | Path, report: dict[str, Any]) -> None:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(destination)


def _download(
    document: BenchmarkDocument,
    cache_root: Path,
    *,
    allowed_hosts: tuple[str, ...],
    maximum_download_mb: int,
) -> Path:
    destination = cache_root / document.filename
    if destination.is_file():
        if _sha256(destination) != document.sha256:
            raise BenchmarkManifestError(f"cached hash mismatch for {document.identifier}")
        return destination

    limit = maximum_download_mb * 1024 * 1024
    temporary = cache_root / f".{document.filename}.download"
    size = 0
    try:
        with httpx.stream(
            "GET",
            document.url,
            follow_redirects=True,
            timeout=120,
            headers={"User-Agent": "MiloPDFHarness/0.2 benchmark"},
        ) as response:
            redirect_chain = [*response.history, response]
            if any(not _url_is_allowed(str(item.url), allowed_hosts) for item in redirect_chain):
                raise BenchmarkManifestError(
                    f"download redirect uses a disallowed URL for {document.identifier}"
                )
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise BenchmarkManifestError(
                            f"download exceeds limit for {document.identifier}"
                        )
                    handle.write(chunk)
        with temporary.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise BenchmarkManifestError(f"invalid PDF signature for {document.identifier}")
        if _sha256(temporary) != document.sha256:
            raise BenchmarkManifestError(f"downloaded hash mismatch for {document.identifier}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _url_is_allowed(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in allowed_hosts


def _check(name: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed)}
