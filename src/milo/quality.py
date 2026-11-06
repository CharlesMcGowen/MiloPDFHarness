from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    overall_quality: float
    relevance_score: float
    completeness_score: float
    clarity_score: float
    issues: tuple[str, ...]
    judge: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["issues"] = list(self.issues)
        return result


class HeuristicQualityJudge:
    """Offline extraction-quality scoring that never transmits document data."""

    _word = re.compile(r"\b[\w'-]+\b", re.UNICODE)

    def assess(self, text: str, *, page_count: int = 0) -> QualityAssessment:
        issues: list[str] = []
        length = len(text.strip())
        words = self._word.findall(text)

        length_score = min(1.0, math.log10(max(length, 1)) / 4.0)
        if length < 200:
            issues.append("very_little_text")

        printable = sum(ch.isprintable() or ch in "\n\t" for ch in text)
        printable_ratio = printable / max(len(text), 1)
        if printable_ratio < 0.98:
            issues.append("non_printable_characters")

        replacement_ratio = text.count("\ufffd") / max(len(text), 1)
        if replacement_ratio > 0.001:
            issues.append("encoding_replacement_characters")

        alpha_words = sum(any(ch.isalpha() for ch in word) for word in words)
        word_ratio = alpha_words / max(len(words), 1)
        if words and word_ratio < 0.7:
            issues.append("low_word_coherence")

        short_lines = [line for line in text.splitlines() if 0 < len(line.strip()) <= 2]
        line_count = max(len(text.splitlines()), 1)
        fragmentation = len(short_lines) / line_count
        if fragmentation > 0.25:
            issues.append("fragmented_layout")

        page_coverage = 1.0
        if page_count:
            chars_per_page = length / page_count
            page_coverage = min(1.0, chars_per_page / 500.0)
            if chars_per_page < 80:
                issues.append("sparse_pages")

        clarity = max(
            0.0,
            min(
                1.0,
                printable_ratio - min(0.4, replacement_ratio * 100) - min(0.3, fragmentation),
            ),
        )
        completeness = (length_score + page_coverage) / 2.0
        relevance = word_ratio if words else 0.0
        overall = 0.35 * clarity + 0.40 * completeness + 0.25 * relevance

        return QualityAssessment(
            overall_quality=round(max(0.0, min(1.0, overall)), 4),
            relevance_score=round(relevance, 4),
            completeness_score=round(completeness, 4),
            clarity_score=round(clarity, 4),
            issues=tuple(issues),
            judge="offline_heuristic",
        )


class LLMQualityJudge:
    """Optional OpenAI-compatible judge with explicit opt-in configuration."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def assess(self, text: str, *, page_count: int = 0) -> QualityAssessment:
        excerpt = text[:12_000]
        prompt = (
            "Evaluate PDF text extraction quality. Judge extraction integrity, not the "
            "document's opinions. Return JSON only with numeric fields overall_quality, "
            "relevance_score, completeness_score, clarity_score from 0 to 1, plus an "
            "issues array of short identifiers.\n\n"
            f"Page count: {page_count}\nExtracted text:\n{excerpt}"
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a strict PDF extraction quality judge.",
                },
                {"role": "user", "content": prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/v1/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

        parsed = self._parse_json(content)
        return QualityAssessment(
            overall_quality=self._score(parsed.get("overall_quality")),
            relevance_score=self._score(parsed.get("relevance_score")),
            completeness_score=self._score(parsed.get("completeness_score")),
            clarity_score=self._score(parsed.get("clarity_score")),
            issues=tuple(str(item)[:80] for item in parsed.get("issues", [])[:20]),
            judge="configured_llm",
        )

    @staticmethod
    def _score(value: object) -> float:
        return round(max(0.0, min(1.0, float(value))), 4)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        content = content.strip()
        fence = chr(96) * 3
        if content.startswith(fence):
            content = content.removeprefix(fence).lstrip()
            content = content.removeprefix("json").lstrip()
            content = content.removesuffix(fence).rstrip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("quality judge did not return an object")
        return parsed
