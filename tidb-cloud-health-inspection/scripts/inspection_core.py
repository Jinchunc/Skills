#!/usr/bin/env python3
"""Shared deterministic primitives for TiDB Cloud health inspection."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

PLANS = ("Starter", "Essential", "Premium", "Dedicated", "BYOC", "Unknown")
LOGICAL_PLANS = frozenset({"Starter", "Essential", "Premium"})
STATUS_RANK = {"HEALTHY": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3, "N/A": -1}

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES_PATH = ROOT / "config" / "rules.v1.json"


def load_rules(path: str | Path = DEFAULT_RULES_PATH) -> dict[str, Any]:
    """Load the threshold rules registry. Returns {} if the file is absent.

    Single home for threshold values: both the logical collection layer and the
    full-stack collector read their thresholds from here.
    """
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}
    return value.get("rules", value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime
    step_seconds: int

    @property
    def duration_seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def start_iso(self) -> str:
        return self.start.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end.isoformat()

    @property
    def promql_duration(self) -> str:
        seconds = self.duration_seconds
        if seconds % 86400 == 0:
            return f"{seconds // 86400}d"
        if seconds % 3600 == 0:
            return f"{seconds // 3600}h"
        if seconds % 60 == 0:
            return f"{seconds // 60}m"
        return f"{seconds}s"

    def evidence(self) -> dict[str, Any]:
        return {
            "timezone": "UTC",
            "start": self.start_iso,
            "end": self.end_iso,
            "duration_seconds": self.duration_seconds,
            "step": f"{self.step_seconds}s",
            "aggregation": "range distribution; exact counter increase for window totals",
        }


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_relative_window(value: str) -> timedelta:
    if len(value) < 2 or value[-1] not in "hdm" or not value[:-1].isdigit():
        raise ValueError("relative window must be a positive integer followed by m, h, or d")
    amount = int(value[:-1])
    if amount <= 0:
        raise ValueError("relative window must be positive")
    return {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[value[-1]]


def resolve_window(
    start: str | None,
    end: str | None,
    relative_window: str | None,
    step_seconds: int,
    now: datetime | None = None,
) -> TimeWindow:
    if step_seconds <= 0:
        raise ValueError("step must be positive")
    if relative_window:
        if start:
            raise ValueError("--start cannot be combined with --relative-window")
        resolved_end = parse_timestamp(end) if end else (now or datetime.now(timezone.utc))
        resolved_start = resolved_end - parse_relative_window(relative_window)
    else:
        if not start or not end:
            raise ValueError("provide --start and --end, or --relative-window")
        resolved_start, resolved_end = parse_timestamp(start), parse_timestamp(end)
    if resolved_end <= resolved_start:
        raise ValueError("inspection window end must be after start")
    return TimeWindow(resolved_start, resolved_end, step_seconds)


def linear_percentile(values: list[float], quantile: float) -> float:
    """Return an R-7/NumPy-compatible linearly interpolated percentile."""
    if not values:
        raise ValueError("cannot calculate a percentile from an empty series")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: Iterable[float]) -> dict[str, float | int] | None:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return None
    return {
        "samples": len(finite),
        "percentile_method": "linear_r7",
        "min": min(finite),
        "avg": math.fsum(finite) / len(finite),
        "p50": linear_percentile(finite, 0.50),
        "p95": linear_percentile(finite, 0.95),
        "p99": linear_percentile(finite, 0.99),
        "p999": linear_percentile(finite, 0.999),
        "max": max(finite),
    }


def status_high(value: float, notice: float, warning: float, critical: float) -> str:
    if value < notice:
        return "HEALTHY"
    if value < warning:
        return "NOTICE"
    if value < critical:
        return "WARNING"
    return "CRITICAL"


def worst_status(values: Iterable[str]) -> str:
    ranked = [value for value in values if value != "N/A"]
    return max(ranked, key=STATUS_RANK.__getitem__) if ranked else "N/A"


def normalize_plan(metadata: dict[str, Any]) -> str:
    raw = str(metadata.get("deployTypeV2") or metadata.get("deployType") or "").strip().lower()
    aliases = {
        "starter": "Starter",
        "serverless": "Starter",
        "essential": "Essential",
        "premium": "Premium",
        "dedicated": "Dedicated",
        "byoc": "BYOC",
    }
    if raw in aliases:
        return aliases[raw]
    # Tolerate a trailing version suffix only, e.g. "essential v2" → Essential.
    # Anything else (e.g. "premium resource pool", a pool record, not a logical
    # cluster) stays Unknown and is default-denied.
    parts = raw.split()
    if len(parts) == 2 and parts[0] in aliases and parts[1][:1] == "v" and parts[1][1:].isdigit():
        return aliases[parts[0]]
    return "Unknown"
