from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .converter import PDFConverter
from .feedback import FeedbackStore, feedback_to_dict
from .worker import ReprocessingWorker


def _components() -> tuple[Settings, FeedbackStore, PDFConverter, ReprocessingWorker]:
    settings = Settings.from_env()
    settings.ensure_runtime_dirs()
    store = FeedbackStore(
        settings.database_url,
        quality_threshold=settings.quality_threshold,
        max_reprocess_attempts=settings.max_reprocess_attempts,
    )
    converter = PDFConverter(settings)
    worker = ReprocessingWorker(settings=settings, store=store, converter=converter)
    return settings, store, converter, worker


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


async def _convert(args: argparse.Namespace) -> int:
    settings, store, converter, _ = _components()
    result = await converter.convert(
        args.input,
        output_format=args.format,
        output_path=args.output,
        enhanced=args.enhanced,
        preferred_decoders=store.preferred_decoders(),
        use_llm_judge=args.llm_judge,
    )
    store.record_decoder_outcomes(
        document_fingerprint=result.fingerprint,
        document_type=result.document_type,
        page_count=result.page_count,
        scores=result.candidate_scores,
    )
    response = result.as_dict()
    if result.quality_score < settings.quality_threshold:
        managed = _copy_to_managed_input(Path(args.input), settings.input_root)
        feedback = store.record_feedback(
            document_path=managed,
            feedback_type="low_quality",
            feedback_reason="automatic extraction quality threshold",
            quality_rating=result.quality_score,
            feedback_source="milo_cli",
        )
        response["feedback_id"] = feedback.id
        response["managed_document"] = managed.name
    _print(response)
    return 0


def _feedback(args: argparse.Namespace) -> int:
    settings, store, _, _ = _components()
    managed = _copy_to_managed_input(Path(args.document), settings.input_root)
    record = store.record_feedback(
        document_path=managed,
        feedback_type=args.type,
        feedback_reason=args.reason,
        quality_rating=args.score,
        relevance_score=args.relevance,
        feedback_source="operator_cli",
    )
    _print(feedback_to_dict(record))
    return 0


async def _reprocess(args: argparse.Namespace) -> int:
    _, _, _, worker = _components()
    _print(await worker.process_pending(limit=args.limit))
    return 0


def _serve(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not settings.api_token:
        raise SystemExit("MILO_API_TOKEN is required when binding the API beyond loopback")
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


def _copy_to_managed_input(source: Path, input_root: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        source.relative_to(input_root)
        return source
    except ValueError:
        destination = input_root / f"{uuid.uuid4()}-{source.name}"
        shutil.copy2(source, destination)
        return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milo")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="create runtime directories and database")

    convert = commands.add_parser("convert", help="convert and score a PDF")
    convert.add_argument("input")
    convert.add_argument("--format", choices=("markdown", "json"), default="markdown")
    convert.add_argument("--output")
    convert.add_argument("--enhanced", action="store_true")
    convert.add_argument("--llm-judge", action="store_true")

    feedback = commands.add_parser("feedback", help="record operator feedback")
    feedback.add_argument("document")
    feedback.add_argument("--type", required=True)
    feedback.add_argument("--reason", required=True)
    feedback.add_argument("--score", required=True, type=float)
    feedback.add_argument("--relevance", type=float)

    reprocess = commands.add_parser("reprocess", help="process pending feedback")
    reprocess.add_argument("--limit", type=int, default=10)

    commands.add_parser("stats", help="show feedback statistics")
    commands.add_parser("decoders", help="show learned decoder rankings")

    serve = commands.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8091)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        settings, store, _, _ = _components()
        _print({"initialized": True, **settings.public_summary(), "stats": store.stats()})
        return 0
    if args.command == "convert":
        return asyncio.run(_convert(args))
    if args.command == "feedback":
        return _feedback(args)
    if args.command == "reprocess":
        return asyncio.run(_reprocess(args))
    if args.command == "stats":
        _, store, _, _ = _components()
        _print(store.stats())
        return 0
    if args.command == "decoders":
        _, store, _, _ = _components()
        _print(store.decoder_rankings())
        return 0
    if args.command == "serve":
        return _serve(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
