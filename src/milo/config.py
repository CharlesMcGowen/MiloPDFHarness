from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _integer(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _number(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    input_root: Path
    output_root: Path
    max_upload_mb: int
    quality_threshold: float
    max_reprocess_attempts: int
    llm_base_url: str | None
    llm_model: str | None
    llm_api_key: str | None
    api_token: str | None

    @classmethod
    def from_env(cls, *, load_dotenv: bool = True) -> Settings:
        if load_dotenv:
            try:
                from dotenv import load_dotenv as load

                load(override=False)
            except ImportError:
                pass

        llm_base_url = os.getenv("MILO_LLM_BASE_URL", "").strip() or None
        llm_model = os.getenv("MILO_LLM_MODEL", "").strip() or None
        llm_api_key = os.getenv("MILO_LLM_API_KEY", "").strip() or None
        api_token = os.getenv("MILO_API_TOKEN", "").strip() or None

        return cls(
            database_url=os.getenv("MILO_DATABASE_URL", "sqlite:///./var/milo.sqlite3").strip(),
            input_root=Path(os.getenv("MILO_INPUT_ROOT", "./var/input")).resolve(),
            output_root=Path(os.getenv("MILO_OUTPUT_ROOT", "./var/output")).resolve(),
            max_upload_mb=_integer("MILO_MAX_UPLOAD_MB", 50),
            quality_threshold=_number("MILO_QUALITY_THRESHOLD", 0.50),
            max_reprocess_attempts=_integer("MILO_MAX_REPROCESS_ATTEMPTS", 3),
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            api_token=api_token,
        )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_runtime_dirs(self) -> None:
        self.input_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)

    def public_summary(self) -> dict[str, object]:
        """Return non-secret configuration suitable for health output."""
        return {
            "input_root": str(self.input_root),
            "output_root": str(self.output_root),
            "max_upload_mb": self.max_upload_mb,
            "quality_threshold": self.quality_threshold,
            "max_reprocess_attempts": self.max_reprocess_attempts,
            "llm_quality_judge_enabled": bool(self.llm_base_url and self.llm_model),
            "api_auth_enabled": bool(self.api_token),
        }
