import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from milo.adapters import parse_adapter_names, run_adapters


class FakeValues:
    @staticmethod
    def tolist() -> list[list[str]]:
        return [["Metric", "Value"], ["Net income", "$70.6 billion"]]


def test_camelot_adapter_writes_structured_tables(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")
    table = SimpleNamespace(
        df=SimpleNamespace(values=FakeValues()),
        page=1,
        parsing_report={"accuracy": 99.5, "whitespace": 2.0},
    )
    camelot = ModuleType("camelot")
    camelot.read_pdf = lambda *args, **kwargs: [table]
    monkeypatch.setitem(sys.modules, "camelot", camelot)

    result = run_adapters(source, names=("camelot",), output_root=tmp_path / "output")

    assert result[0]["status"] == "ok"
    payload = json.loads(Path(result[0]["output_path"]).read_text(encoding="utf-8"))
    assert payload["table_count"] == 1
    assert payload["tables"][0]["rows"][1] == ["Net income", "$70.6 billion"]


def test_docling_adapter_writes_markdown(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")
    document = SimpleNamespace(
        tables=[object(), object()],
        export_to_markdown=lambda: "# Quarterly Banking Profile\n",
    )

    class FakeConverter:
        def convert(self, source_path: str):
            return SimpleNamespace(document=document)

    package = ModuleType("docling")
    package.__path__ = []
    module = ModuleType("docling.document_converter")
    module.DocumentConverter = FakeConverter
    monkeypatch.setitem(sys.modules, "docling", package)
    monkeypatch.setitem(sys.modules, "docling.document_converter", module)

    result = run_adapters(source, names="docling", output_root=tmp_path / "output")

    assert result[0]["status"] == "ok"
    assert result[0]["item_count"] == 2
    assert Path(result[0]["output_path"]).read_text(encoding="utf-8").startswith("# Quarterly")


def test_unknown_adapter_is_rejected() -> None:
    try:
        parse_adapter_names("unknown")
    except ValueError as exc:
        assert "unsupported adapters" in str(exc)
    else:
        raise AssertionError("unknown adapter was accepted")
