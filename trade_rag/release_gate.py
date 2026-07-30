"""Deterministic M6 PDF release-gate metrics and privacy-safe report helpers."""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


REPORT_SCHEMA = "pdf-release-gate-v1"


def percentile(values: Iterable[float], quantile: float = 0.95) -> float:
    rows = sorted(float(value) for value in values)
    if not rows:
        return 0.0
    index = max(0, min(len(rows) - 1, math.ceil(len(rows) * quantile) - 1))
    return rows[index]


@dataclass(frozen=True)
class GateMetric:
    name: str
    value: float
    threshold: float
    comparator: str
    unit: str

    @property
    def passed(self) -> bool:
        if self.comparator == ">=":
            return self.value >= self.threshold
        if self.comparator == "<=":
            return self.value <= self.threshold
        if self.comparator == "==":
            return self.value == self.threshold
        raise ValueError("unsupported release-gate comparator")


class PdfReleaseGateReport:
    """Aggregate-only report: no questions, answers, paths, hashes or document text."""

    def __init__(self, metrics: Iterable[GateMetric], *, fixture_set: str,
                 performance: dict | None = None) -> None:
        self.metrics = tuple(metrics)
        self.fixture_set = fixture_set
        self.performance = dict(performance or {})

    @property
    def passed(self) -> bool:
        return bool(self.metrics) and all(metric.passed for metric in self.metrics)

    def as_dict(self) -> dict:
        return {
            "schema_version": REPORT_SCHEMA,
            "fixture_set": self.fixture_set,
            "status": "passed" if self.passed else "failed",
            "metrics": [{**asdict(metric), "passed": metric.passed}
                        for metric in self.metrics],
            "performance": self.performance,
            "production_ready": False,
            "production_blockers": [
                "real_embedding_not_validated",
                "production_index_backends_not_validated",
                "external_ocr_not_validated",
                "site_acceptance_not_completed",
            ],
        }

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.as_dict(), ensure_ascii=False, indent=2,
                             sort_keys=True).encode("utf-8") + b"\n"
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)
        return target


def performance_summary(parse_ms: Iterable[float], index_ms: Iterable[float],
                        peak_bytes: Iterable[int]) -> dict:
    parse = list(parse_ms); index = list(index_ms); memory = list(peak_bytes)
    return {
        "sample_count": min(len(parse), len(index), len(memory)),
        "parse_ms": {"median": round(statistics.median(parse), 3) if parse else 0.0,
                     "p95": round(percentile(parse), 3)},
        "index_ms": {"median": round(statistics.median(index), 3) if index else 0.0,
                     "p95": round(percentile(index), 3)},
        "peak_bytes": {"median": int(statistics.median(memory)) if memory else 0,
                       "p95": int(percentile(memory))},
        "scope": "synthetic_local_mock",
    }
