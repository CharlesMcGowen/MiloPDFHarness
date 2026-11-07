from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SUPPORTED_ADAPTERS = ("camelot", "docling")


class OptionalAdapterUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AdapterSummary:
    adapter: str
    status: str
    output_path: str | None
    item_count: int
    content_length: int
    warnings: tuple[str, ...] = ()
    error_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["warnings"] = list(self.warnings)
        return result


def parse_adapter_names(value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        requested = [item.strip().lower() for item in value.split(",") if item.strip()]
    else:
        requested = [str(item).strip().lower() for item in value if str(item).strip()]
    unknown = sorted(set(requested) - set(SUPPORTED_ADAPTERS))
    if unknown:
        raise ValueError(f"unsupported adapters: {', '.join(unknown)}")
    return tuple(dict.fromkeys(requested))


def run_adapters(
    pdf_path: str | Path,
    *,
    names: str | list[str] | tuple[str, ...],
    output_root: str | Path,
) -> list[dict[str, Any]]:
    source = Path(pdf_path).expanduser().resolve()
    destination_root = Path(output_root).expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []

    for name in parse_adapter_names(names):
        try:
            if name == "camelot":
                summary = _run_camelot(source, destination_root)
            else:
                summary = _run_docling(source, destination_root)
        except OptionalAdapterUnavailable:
            summary = AdapterSummary(
                adapter=name,
                status="unavailable",
                output_path=None,
                item_count=0,
                content_length=0,
                warnings=(f"install_milo_{name}_extra",),
                error_type="OptionalAdapterUnavailable",
            )
        except Exception as exc:
            summary = AdapterSummary(
                adapter=name,
                status="failed",
                output_path=None,
                item_count=0,
                content_length=0,
                warnings=(f"{name}_adapter_failed",),
                error_type=type(exc).__name__,
            )
        summaries.append(summary.as_dict())
    return summaries


def _run_camelot(source: Path, output_root: Path) -> AdapterSummary:
    try:
        import camelot
    except (ImportError, ModuleNotFoundError) as exc:
        raise OptionalAdapterUnavailable("Camelot is not installed") from exc

    extracted = camelot.read_pdf(str(source), pages="all", flavor="auto")
    tables: list[dict[str, Any]] = []
    total_cells = 0
    truncated = False
    maximum_cells = 20_000
    for index, table in enumerate(extracted, start=1):
        rows = [[str(value) for value in row] for row in table.df.values.tolist()]
        available = max(0, maximum_cells - total_cells)
        cells = sum(len(row) for row in rows)
        if cells > available:
            kept: list[list[str]] = []
            for row in rows:
                if len(row) > available:
                    break
                kept.append(row)
                available -= len(row)
            rows = kept
            truncated = True
        total_cells += sum(len(row) for row in rows)
        report = getattr(table, "parsing_report", {}) or {}
        tables.append(
            {
                "index": index,
                "page": str(getattr(table, "page", "")),
                "shape": [len(rows), max((len(row) for row in rows), default=0)],
                "accuracy": report.get("accuracy"),
                "whitespace": report.get("whitespace"),
                "rows": rows,
            }
        )
        if truncated:
            break

    payload = {
        "adapter": "camelot",
        "source_filename": source.name,
        "source_sha256": _sha256(source),
        "table_count": len(extracted),
        "included_table_count": len(tables),
        "output_truncated": truncated,
        "tables": tables,
    }
    destination = output_root / f"{source.stem}.camelot.json"
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    _atomic_write(destination, rendered)
    warnings = ("camelot_output_truncated",) if truncated else ()
    return AdapterSummary(
        adapter="camelot",
        status="ok",
        output_path=str(destination),
        item_count=len(extracted),
        content_length=len(rendered),
        warnings=warnings,
    )


def _run_docling(source: Path, output_root: Path) -> AdapterSummary:
    try:
        from docling.document_converter import DocumentConverter
    except (ImportError, ModuleNotFoundError) as exc:
        raise OptionalAdapterUnavailable("Docling is not installed") from exc

    conversion = DocumentConverter().convert(str(source))
    document = conversion.document
    markdown = document.export_to_markdown()
    tables = getattr(document, "tables", ()) or ()
    destination = output_root / f"{source.stem}.docling.md"
    _atomic_write(destination, markdown)
    return AdapterSummary(
        adapter="docling",
        status="ok",
        output_path=str(destination),
        item_count=len(tables),
        content_length=len(markdown),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
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
