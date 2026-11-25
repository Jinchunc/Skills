#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
# Resolve clinic_api.py from the clinic-api skill scripts dir. Candidate
# layouts, in order:
#   1. agent-stack dba profile / build tree:
#      <root>/providers/tidb-cloud/ops/scripts/clinic_api.py
#      (SCRIPT_DIR = <root>/engines/tidb/instance-inspection/playbooks/dedicated-daily/scripts)
#   2. upstream nutshell-skills layout:
#      <skills>/platform/clinic-api/scripts/clinic_api.py
#   3. installed skill locations (Codex / Claude / OpenCode)
_CLINIC_API_CANDIDATES = [
    SCRIPT_DIR.parents[5] / "providers" / "tidb-cloud" / "ops" / "scripts",
    SCRIPT_DIR.parents[2] / "platform" / "clinic-api" / "scripts",
    Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills" / "clinic-api" / "scripts",
    Path.home() / ".claude" / "skills" / "clinic-api" / "scripts",
    Path.home() / ".config" / "opencode" / "skills" / "clinic-api" / "scripts",
]
for _candidate in _CLINIC_API_CANDIDATES:
    if (_candidate / "clinic_api.py").exists():
        sys.path.insert(0, str(_candidate))
        break
else:
    # Fall back to the in-tree location so the ImportError message points at it.
    sys.path.insert(0, str(_CLINIC_API_CANDIDATES[0]))

from clinic_api import ClinicAPI  # type: ignore  # noqa: E402


DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_ENV_FILE = str(Path.home() / ".env")
DISK_DEVICE_RE = r"nvme.*|xvd.*|sd.*|vd.*|dm-.*"
TIKV_POD_RE = r".*-tikv-.*"
THREAD_GROUPS: dict[str, tuple[re.Pattern[str], float]] = {
    "raft_store": (re.compile(r"^raftstore_.*$"), 0.8),
    "async_apply": (re.compile(r"^apply(_low)?_\d+$"), 0.7),
    "raft_async_writer": (re.compile(r"^store_writer_\d+$"), 0.8),
    "grpc_poll": (re.compile(r"^grpc_server_\d+$"), 0.8),
    "scheduler_pool": (re.compile(r"^sched_worker_(pool|priority|high)_\d+$"), 0.7),
    "unified_read_pool": (re.compile(r"^unified_read_pool_\d+$"), 0.7),
}
THREAD_GROUP_DISPLAY = {
    "raft_store": "Raft store CPU",
    "async_apply": "Async apply CPU",
    "raft_async_writer": "Raft async writer CPU",
    "grpc_poll": "gRPC poll CPU",
    "scheduler_pool": "Scheduler pool CPU",
    "unified_read_pool": "Unified read pool CPU",
}
WRITE_GRPC_TARGET_TYPES = ("kv_prewrite", "kv_commit")
READ_GRPC_TARGET_TYPES = ("coprocessor", "kv_get", "kv_batch_get", "kv_batch_get_command")
GRPC_TARGET_TYPES = WRITE_GRPC_TARGET_TYPES + READ_GRPC_TARGET_TYPES
GRPC_BATCH_GET_TYPES = ("kv_batch_get", "kv_batch_get_command")
UNIFIED_READ_POOL_METRIC_NAME = "unified-read-pool"
RANGE_STEP_SECONDS = 300
ROLLING_15M_POINTS = 3
ROLLING_30M_POINTS = 6
ROLLING_30M_SECONDS = 30 * 60
GIB = 1024 * 1024 * 1024
GRPC_FAIL_30M_WARN_THRESHOLD = 100.0
GRPC_P99_WARN_SECONDS = 1.0
CPU_SUSTAIN_WARN_PCT = 85.0
MEMORY_WARN_PCT = 85.0
MEMORY_ERROR_PCT = 90.0
MEMORY_LEAK_BASELINE_WINDOW = "7d"
MEMORY_LEAK_OBSERVE_DELTA_PCT = 3.0
MEMORY_LEAK_WARN_DELTA_PCT = 5.0
MEMORY_LEAK_EXPLAIN_REGION_DELTA_ABS = 100.0
MEMORY_LEAK_EXPLAIN_REGION_DELTA_RATIO_PCT = 10.0
MEMORY_LEAK_EXPLAIN_QPS_DELTA_ABS = 100.0
MEMORY_LEAK_EXPLAIN_QPS_DELTA_RATIO_PCT = 20.0
MEMORY_LEAK_EXPLAIN_CPU_DELTA_PCT = 5.0
UNIFIED_READ_HOT_PCT = 80.0
UNIFIED_READ_PEER_LOW_PCT = 50.0
STORE_USAGE_WARN_PCT = 80.0
STORE_USAGE_RISE_WARN_PCT = 10.0
COMPACTION_PENDING_SUSTAIN_WARN_BYTES = 200 * GIB
COMPACTION_PENDING_REFERENCE_BYTES = 256 * GIB
COMPACTION_PENDING_PEAK_WARN_BYTES = 512 * GIB
SLOW_SCORE_ALERT_VALUE = 100.0
RAFT_LOG_LAG_P99_ALERT = 5000.0
IOPS_EXCEED_WARN_THRESHOLD = 1.0


def load_shell_env(env_file: str) -> None:
    path = Path(env_file).expanduser()
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_seconds(dt: datetime) -> int:
    return int(dt.timestamp())


def epoch_nanoseconds(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


def epoch_milliseconds(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000)


def to_local_string(dt: datetime, tz_name: str) -> str:
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")


def format_pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value:.2f}%"


def format_pct_point_delta(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value:+.2f} pp"


def format_pct_change(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value:+.2f}%"


def format_ms_from_seconds(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value * 1000:.2f} ms"


def format_gib(value_bytes: float | None) -> str:
    if value_bytes is None or math.isnan(value_bytes):
        return "N/A"
    return f"{value_bytes / 1024 / 1024 / 1024:.2f} GiB"


def format_iops(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value:.2f}"


def format_mib_per_s(value_bytes_per_s: float | None) -> str:
    if value_bytes_per_s is None or math.isnan(value_bytes_per_s):
        return "N/A"
    return f"{value_bytes_per_s / 1024 / 1024:.2f} MiB/s"


def format_count(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    if abs(value - round(value)) < 0.01:
        return str(int(round(value)))
    return f"{value:.2f}"


def format_seconds(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value:.2f} s"


def format_delta(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    if abs(value - round(value)) < 0.01:
        return f"{int(round(value)):+d}"
    return f"{value:+.2f}"


def feishu_color_tag(label: str, color: str) -> str:
    return f'<text color="{color}">{label}</text>'


def severity_badge(severity: str) -> str:
    mapping = {
        "Healthy": ("HEALTHY", "green"),
        "Warning": ("WARNING", "orange"),
        "Error": ("ERROR", "red"),
        "Needs Review": ("REVIEW", "gray"),
        "Unknown": ("UNKNOWN", "gray"),
    }
    label, color = mapping.get(severity, (severity.upper(), "gray"))
    return feishu_color_tag(label, color)


def action_badge(action: str) -> str:
    mapping = {
        "Routine watch": ("ROUTINE", "green"),
        "Keep watching": ("WATCH", "orange"),
        "Check now": ("CHECK NOW", "red"),
        "Review missing signals": ("REVIEW", "gray"),
    }
    label, color = mapping.get(action, (action.upper(), "gray"))
    return feishu_color_tag(label, color)


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, "", "NaN"):
            return None
        return float(value)
    except Exception:
        return None


def format_ts_local(ts: float, tz_name: str) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo(tz_name)).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def get_vector(resp: dict[str, Any]) -> list[dict[str, Any]]:
    return list((resp.get("data") or {}).get("result") or [])


def get_matrix(resp: dict[str, Any]) -> list[dict[str, Any]]:
    return list((resp.get("data") or {}).get("result") or [])


def response_error(resp: dict[str, Any]) -> str | None:
    if not isinstance(resp, dict):
        return "invalid response"
    if resp.get("error"):
        return str(resp["error"])
    status = resp.get("status")
    if status and (("data" in resp) or ("resultType" in resp)) and status != "success":
        return f"status={status}"
    return None


def ensure_success(resp: dict[str, Any], context: str) -> dict[str, Any]:
    error = response_error(resp)
    if error:
        raise RuntimeError(f"{context}: {error}")
    return resp


@dataclass
class Window:
    start: datetime
    end: datetime
    timezone_name: str

    @property
    def seconds(self) -> int:
        return max(1, int(self.end.timestamp() - self.start.timestamp()))

    @property
    def prom_window(self) -> str:
        return f"{self.seconds}s"


def parse_relative_window(value: str) -> timedelta:
    match = re.fullmatch(r"(?i)\s*(\d+)\s*([hd])\s*", value)
    if not match:
        raise ValueError(
            f"invalid relative window: {value!r}. Use values like 2h, 6h, or 1d."
        )
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        raise ValueError("relative window must be greater than 0")
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    raise ValueError(
        f"invalid relative window unit: {unit!r}. Use h for hours or d for days."
    )


class InspectionClient:
    def __init__(self, env_file: str) -> None:
        load_shell_env(env_file)
        self.metrics = ClinicAPI(env_file="__missing__.env")
        self.endpoint = self.metrics.endpoint.rstrip("/")
        self.api_key = os.environ.get("CLINIC_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("CLINIC_API_KEY not set")

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.metrics._get(path, params or {})

    def search_clusters(
        self,
        *,
        query: str | None = None,
        cluster_id: str | None = None,
        show_deleted: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "show_deleted": str(show_deleted).lower(),
            "limit": limit,
            "page": 1,
        }
        if cluster_id:
            params["cluster_id"] = cluster_id
        elif query:
            params["query"] = query
        return self._get_json("/clinic/api/v1/dashboard/clusters", params)

    def get_cluster_detail(self, org_id: str, cluster_id: str) -> dict[str, Any]:
        return self._get_json(f"/clinic/api/v1/orgs/{org_id}/clusters/{cluster_id}")

    def get_topology(self, org_id: str, cluster_id: str) -> dict[str, Any]:
        return self._get_json(
            f"/clinic/api/v1/orgs/{org_id}/clusters/{cluster_id}/resource-pool/components"
        )

    def get_dashboards(self, org_id: str, cluster_id: str, start_time: int, end_time: int) -> Any:
        return self._get_json(
            f"/clinic/api/v1/orgs/{org_id}/clusters/{cluster_id}/dashboard",
            {"startTime": start_time, "endTime": end_time},
        )

    def loki_query_range(
        self,
        cluster_id: str,
        query: str,
        start_ns: int,
        end_ns: int,
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._get_json(
            f"/data-proxy/loki/{cluster_id}/api/v1/query_range",
            {
                "query": query,
                "start": start_ns,
                "end": end_ns,
                "limit": limit,
                "direction": "backward",
            },
        )


def parse_datetime(value: str, tz_name: str) -> datetime:
    if value.endswith("Z"):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def resolve_window(
    report_date: str | None,
    start: str | None,
    end: str | None,
    relative_window: str | None,
    tz_name: str,
) -> Window:
    tz = ZoneInfo(tz_name)
    if start and end and relative_window:
        raise ValueError("`--relative-window` cannot be combined with both `--start` and `--end`")

    if start and relative_window:
        raise ValueError("`--start` cannot be combined with `--relative-window`; use `--end` as the anchor instead")

    if start and end:
        start_dt = parse_datetime(start, tz_name)
        end_dt = parse_datetime(end, tz_name)
        return Window(start=start_dt, end=end_dt, timezone_name=tz_name)

    if start or end:
        if not relative_window:
            raise ValueError("`--start` and `--end` must be provided together unless `--relative-window` is used")
        anchor_dt = parse_datetime(end, tz_name) if end else datetime.now(tz)
        return Window(
            start=anchor_dt - parse_relative_window(relative_window),
            end=anchor_dt,
            timezone_name=tz_name,
        )

    if relative_window:
        end_dt = datetime.now(tz)
        return Window(
            start=end_dt - parse_relative_window(relative_window),
            end=end_dt,
            timezone_name=tz_name,
        )

    now = datetime.now(tz)
    target_date = date.fromisoformat(report_date) if report_date else now.date()
    day_start = datetime.combine(target_date, time.min, tzinfo=tz)
    if target_date < now.date():
        day_end = datetime.combine(target_date, time.max.replace(microsecond=0), tzinfo=tz)
    else:
        day_end = now
    return Window(start=day_start, end=day_end, timezone_name=tz_name)


def resolve_report_date_value(report_date: str | None, window: Window) -> str:
    if report_date:
        return report_date
    return window.end.astimezone(ZoneInfo(window.timezone_name)).date().isoformat()


def normalize_cluster_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in raw.split(","):
            cluster_id = part.strip()
            if not cluster_id or cluster_id in seen:
                continue
            seen.add(cluster_id)
            result.append(cluster_id)
    return result


def choose_cluster_item(search_resp: dict[str, Any], cluster_id: str) -> dict[str, Any]:
    ensure_success(search_resp, f"search cluster {cluster_id}")
    items = list(search_resp.get("items") or [])
    for item in items:
        if str(item.get("clusterID")) == cluster_id:
            return item
    raise RuntimeError(f"cluster not found: {cluster_id}")


def extract_tikv_spec(topology: list[dict[str, Any]]) -> dict[str, Any]:
    for component in topology:
        if component.get("component_type") == "tikv":
            return component
    raise RuntimeError("tikv topology not found")


def extract_tikv_storage_type(detail: dict[str, Any]) -> str:
    components = detail.get("components") or {}
    tikv = components.get("COMPONENT_TYPE_TIKV") or {}
    return str(tikv.get("storageType") or "").strip()


def extract_topology_summary(topology: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for component in topology:
        component_type = str(component.get("component_type") or "").strip().lower()
        if not component_type:
            continue

        count: int | None = None
        for key in ("replicas", "replica_count", "count", "node_count"):
            raw_value = component.get(key)
            if isinstance(raw_value, (int, float)):
                count = int(raw_value)
                break
            if isinstance(raw_value, str) and raw_value.isdigit():
                count = int(raw_value)
                break

        if count is None:
            for key in ("pods", "instances", "hosts", "nodes"):
                raw_value = component.get(key)
                if isinstance(raw_value, list):
                    count = len(raw_value)
                    break

        if count is not None:
            summary[component_type] = count

    return summary


def resolve_tikv_disk_devices(storage_type: str) -> list[str]:
    normalized = storage_type.strip().lower()
    if normalized == "plus":
        return ["nvme2n1"]
    if normalized in {"standard", "performance"}:
        return ["nvme2n1", "nvme3n1"]
    return []


def build_device_regex(devices: list[str]) -> str:
    if not devices:
        return DISK_DEVICE_RE
    return "|".join(re.escape(device) for device in devices)


def vector_stats(resp: dict[str, Any], *, key_field: str = "instance") -> dict[str, float]:
    result: dict[str, float] = {}
    for entry in get_vector(resp):
        metric = entry.get("metric") or {}
        key = str(metric.get(key_field) or metric.get("pod") or metric.get("type") or "unknown")
        value = safe_float((entry.get("value") or [None, None])[1])
        if value is None:
            continue
        result[key] = value
    return result


def total_from_vector(resp: dict[str, Any]) -> float:
    total = 0.0
    for entry in get_vector(resp):
        value = safe_float((entry.get("value") or [None, None])[1])
        if value is not None:
            total += value
    return total


def observed_counter_increase_from_matrix(resp: dict[str, Any]) -> float:
    total = 0.0
    for entry in get_matrix(resp):
        previous_value: float | None = None
        for _, raw_value in entry.get("values") or []:
            value = safe_float(raw_value)
            if value is None:
                continue
            if previous_value is None:
                # Ignore a non-zero first sample: the series may have appeared mid-window.
                previous_value = value
                continue
            delta = value - previous_value
            if delta >= 0:
                total += delta
            else:
                total += value
            previous_value = value
    return total


def alert_counter_total(range_resp: dict[str, Any], instant_resp: dict[str, Any]) -> float:
    if response_error(range_resp) is None:
        return observed_counter_increase_from_matrix(range_resp)
    return total_from_vector(instant_resp)


def max_from_vector(resp: dict[str, Any]) -> float | None:
    values = [
        value
        for entry in get_vector(resp)
        if (value := safe_float((entry.get("value") or [None, None])[1])) is not None
    ]
    return max(values) if values else None


def range_peak_by_series(resp: dict[str, Any], *, metric_key: str) -> tuple[float | None, str | None]:
    peak_value: float | None = None
    peak_key: str | None = None
    for entry in get_matrix(resp):
        metric = entry.get("metric") or {}
        key = str(metric.get(metric_key) or "unknown")
        values = [safe_float(value[1]) for value in entry.get("values") or []]
        numeric = [value for value in values if value is not None]
        if not numeric:
            continue
        local_peak = max(numeric)
        if peak_value is None or local_peak > peak_value:
            peak_value = local_peak
            peak_key = key
    return peak_value, peak_key


def log_line_count(resp: dict[str, Any]) -> int:
    total = 0
    for stream in ((resp.get("data") or {}).get("result") or []):
        total += len(stream.get("values") or [])
    return total


def current_nonzero_hotspots(resp: dict[str, Any]) -> list[dict[str, Any]]:
    hot = []
    for entry in get_vector(resp):
        value = safe_float((entry.get("value") or [None, None])[1])
        if value and value > 0:
            hot.append(
                {
                    "type": (entry.get("metric") or {}).get("type", ""),
                    "store": (entry.get("metric") or {}).get("store", ""),
                    "address": (entry.get("metric") or {}).get("address", ""),
                    "value": value,
                }
            )
    return hot


def build_labeled_matrix_series(
    resp: dict[str, Any], label_fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for entry in get_matrix(resp):
        metric = entry.get("metric") or {}
        points: list[tuple[float, float]] = []
        for ts, raw_value in entry.get("values") or []:
            ts_float = safe_float(ts)
            value = safe_float(raw_value)
            if ts_float is None or value is None:
                continue
            points.append((ts_float, value))
        if not points:
            continue
        points.sort(key=lambda item: item[0])
        series.append(
            {
                "labels": {field: str(metric.get(field) or "") for field in label_fields},
                "points": points,
            }
        )
    return series


def find_threshold_windows(
    points: list[tuple[float, float]],
    *,
    threshold: float,
    min_points: int,
    lookback_seconds: int = 0,
    step_seconds: int = RANGE_STEP_SECONDS,
) -> list[dict[str, float]]:
    windows: list[dict[str, float]] = []
    run: list[tuple[float, float]] = []
    previous_ts: float | None = None

    def flush() -> None:
        nonlocal run
        if len(run) >= min_points:
            start_ts = max(0.0, run[0][0] - lookback_seconds)
            end_ts = run[-1][0]
            windows.append(
                {
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "peak_value": max(value for _, value in run),
                }
            )
        run = []

    for ts, value in points:
        is_contiguous = previous_ts is None or ts - previous_ts <= step_seconds * 1.5
        if value >= threshold and is_contiguous:
            run.append((ts, value))
        elif value >= threshold:
            flush()
            run = [(ts, value)]
        else:
            flush()
        previous_ts = ts
    flush()
    return windows


def find_peak_window(
    points: list[tuple[float, float]],
    *,
    lookback_points: int,
    floor_value: float,
    end_floor_value: float | None = None,
    step_seconds: int = RANGE_STEP_SECONDS,
) -> dict[str, float] | None:
    if len(points) <= lookback_points:
        return None
    best: dict[str, float] | None = None
    for idx in range(lookback_points, len(points)):
        start_ts, start_value = points[idx - lookback_points]
        end_ts, end_value = points[idx]
        delta = end_value - start_value
        if delta < floor_value:
            continue
        if end_floor_value is not None and end_value < end_floor_value:
            continue
        candidate = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "delta_value": delta,
            "end_value": end_value,
            "step_seconds": float(step_seconds),
        }
        if best is None or candidate["delta_value"] > best["delta_value"]:
            best = candidate
    return best


def format_window_window(start_ts: float, end_ts: float, tz_name: str) -> str:
    return f"{format_ts_local(start_ts, tz_name)} ~ {format_ts_local(end_ts, tz_name)}"


def top_watch_items(findings: list[dict[str, str]], limit: int = 4) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for finding in findings:
        title = finding.get("title", "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        items.append(title)
        if len(items) >= limit:
            break
    return items


def top_vector_stat(stats: dict[str, float]) -> tuple[str | None, float | None]:
    if not stats:
        return None, None
    key = max(stats, key=stats.get)
    return key, stats[key]


def ratio_delta_pct(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None or math.isnan(current) or math.isnan(baseline):
        return None
    if baseline <= 0:
        return None
    return ((current - baseline) / baseline) * 100.0


def build_memory_growth_supports(item: dict[str, Any]) -> str:
    supports = item.get("support_signals") or []
    if not supports:
        return "none"

    fragments = []
    for support in supports:
        kind = support.get("kind")
        if kind == "region":
            fragments.append(
                f"region avg {format_count(support.get('current'))} vs {format_count(support.get('baseline'))} "
                f"({format_delta(support.get('delta'))}, {format_pct_change(support.get('delta_ratio_pct'))})"
            )
        elif kind == "qps":
            fragments.append(
                f"gRPC QPS avg {format_count(support.get('current'))} vs {format_count(support.get('baseline'))} "
                f"({format_delta(support.get('delta'))}, {format_pct_change(support.get('delta_ratio_pct'))})"
            )
        elif kind == "cpu":
            fragments.append(
                f"CPU avg {format_pct(support.get('current'))} vs {format_pct(support.get('baseline'))} "
                f"({format_pct_point_delta(support.get('delta_pct'))})"
            )
    return "; ".join(fragments) if fragments else "none"


def compute_thread_pool_group_series(resp: dict[str, Any]) -> dict[str, dict[str, Any]]:
    matched_names: dict[str, dict[str, set[str]]] = {
        group: defaultdict(set) for group in THREAD_GROUPS
    }
    grouped: dict[str, dict[str, dict[float, float]]] = {
        group: defaultdict(lambda: defaultdict(float)) for group in THREAD_GROUPS
    }
    for entry in get_matrix(resp):
        metric = entry.get("metric") or {}
        name = str(metric.get("name") or "")
        instance = str(metric.get("instance") or "")
        for group, (pattern, factor) in THREAD_GROUPS.items():
            if not pattern.match(name):
                continue
            matched_names[group][instance].add(name)
            for ts, raw_value in entry.get("values") or []:
                ts_float = safe_float(ts)
                value = safe_float(raw_value)
                if ts_float is None or value is None:
                    continue
                grouped[group][instance][ts_float] += value
            break

    result: dict[str, dict[str, Any]] = {}
    for group, (_, factor) in THREAD_GROUPS.items():
        instances: dict[str, Any] = {}
        for instance, timestamps in grouped[group].items():
            thread_count = len(matched_names[group][instance])
            points = sorted(timestamps.items(), key=lambda item: item[0])
            peak_cpu_cores = max((value for _, value in points), default=0.0)
            peak_util_pct = (peak_cpu_cores / thread_count * 100.0) if thread_count > 0 else None
            instances[instance] = {
                "thread_count": thread_count,
                "factor": factor,
                "threshold": thread_count * factor,
                "peak_cpu_cores": peak_cpu_cores,
                "peak_util_pct": peak_util_pct,
                "points": points,
            }
        result[group] = {"instances": instances}
    return result


def find_unified_read_pool_hotspots(
    thread_group_series: dict[str, dict[str, Any]],
    *,
    min_points: int = ROLLING_15M_POINTS,
    step_seconds: int = RANGE_STEP_SECONDS,
) -> list[dict[str, Any]]:
    instances = (
        thread_group_series.get("unified_read_pool", {}).get("instances", {})
        if thread_group_series
        else {}
    )
    if len(instances) < 2:
        return []

    point_maps = {
        instance: {ts: value for ts, value in info.get("points") or []}
        for instance, info in instances.items()
    }
    all_timestamps = sorted({ts for info in instances.values() for ts, _ in info.get("points") or []})
    candidate_points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    peer_peaks: dict[str, dict[float, float]] = defaultdict(dict)

    for ts in all_timestamps:
        utilization: dict[str, float] = {}
        for instance, info in instances.items():
            thread_count = int(info.get("thread_count") or 0)
            value = point_maps.get(instance, {}).get(ts)
            if thread_count <= 0 or value is None:
                continue
            utilization[instance] = value / thread_count * 100.0
        if len(utilization) < 2:
            continue
        top_instance, top_pct = max(utilization.items(), key=lambda item: item[1])
        peer_values = [value for instance, value in utilization.items() if instance != top_instance]
        peer_peak = max(peer_values) if peer_values else None
        if peer_peak is None:
            continue
        if top_pct >= UNIFIED_READ_HOT_PCT and peer_peak <= UNIFIED_READ_PEER_LOW_PCT:
            candidate_points[top_instance].append((ts, top_pct))
            peer_peaks[top_instance][ts] = peer_peak

    windows: list[dict[str, Any]] = []
    for instance, points in candidate_points.items():
        raw_windows = find_threshold_windows(
            points,
            threshold=UNIFIED_READ_HOT_PCT,
            min_points=min_points,
            step_seconds=step_seconds,
        )
        for window in raw_windows:
            window_points = [
                (ts, value)
                for ts, value in points
                if window["start_ts"] <= ts <= window["end_ts"]
            ]
            peer_peak = max(
                (peer_peaks.get(instance, {}).get(ts, 0.0) for ts, _ in window_points),
                default=0.0,
            )
            windows.append(
                {
                    "instance": instance,
                    "start_ts": window["start_ts"],
                    "end_ts": window["end_ts"],
                    "peak_pct": window["peak_value"],
                    "peer_peak_pct": peer_peak,
                }
            )
    windows.sort(key=lambda item: item["peak_pct"], reverse=True)
    return windows


def build_thread_pool_display_rows(
    thread_group_series: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for group, payload in thread_group_series.items():
        instances = payload.get("instances") or {}
        if not instances:
            result[group] = {"status": "Unknown"}
            continue
        top_instance, top_info = max(
            instances.items(),
            key=lambda item: float(item[1].get("peak_cpu_cores") or 0.0),
        )
        threshold = float(top_info.get("threshold") or 0.0)
        peak_cpu_cores = float(top_info.get("peak_cpu_cores") or 0.0)
        overload = threshold > 0 and peak_cpu_cores > threshold
        result[group] = {
            "instance": top_instance,
            "thread_count": int(top_info.get("thread_count") or 0),
            "threshold": threshold,
            "peak_cpu_cores": peak_cpu_cores,
            "peak_util_pct": top_info.get("peak_util_pct"),
            "status": "Over threshold" if overload else "Normal",
        }
    return result


def build_disk_join_expr(base_expr: str, namespace: str, device_regex: str) -> str:
    return (
        "sum by (pod,device) ("
        f"({base_expr})"
        ' * on(instance) group_left(pod) '
        f'label_replace(kube_pod_info{{namespace="{namespace}", pod=~"{TIKV_POD_RE}"}}, '
        '"instance", "$1", "node", "(.*)")'
        ")"
    )


def build_peak_disk_query(
    base_expr: str,
    namespace: str,
    prom_window: str,
    *,
    device_regex: str,
    pod_reduce: str,
) -> str:
    joined = build_disk_join_expr(base_expr, namespace, device_regex)
    reduced = f"{pod_reduce} by (pod) ({joined})"
    return f"max by (pod) (max_over_time(({reduced})[{prom_window}:5m]))"


def collect_metric_pods(*responses: dict[str, Any]) -> list[str]:
    pods: set[str] = set()
    for resp in responses:
        for entry in get_vector(resp):
            pod = str((entry.get("metric") or {}).get("pod") or "").strip()
            if pod:
                pods.add(pod)
        for entry in get_matrix(resp):
            pod = str((entry.get("metric") or {}).get("pod") or "").strip()
            if pod:
                pods.add(pod)
    return sorted(pods)


def sum_vector_by_label(resp: dict[str, Any], label: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for entry in get_vector(resp):
        metric = entry.get("metric") or {}
        key = str(metric.get(label) or "unknown")
        value = safe_float((entry.get("value") or [None, None])[1])
        if value is None:
            continue
        totals[key] += value
    return dict(totals)


def pick_peak_vector_value(resp: dict[str, Any]) -> float | None:
    return max_from_vector(resp)


def build_loki_filter(namespace: str) -> str:
    return (
        "{cluster_env=\"prod\", namespace=\""
        f"{namespace}"
        "\", container=\"tikv\"} "
        "|~ `ERROR|FATAL` "
        "!~ \"TLS handshake|local file gc kv files|monotonic time jumped|wait connect timeout|batch_raft send response fail\" "
        "!= \"KV:Raft:StepPeerNotFound\" "
        "!= \"no valid key found for split.\" "
        "!= \"cdc register for a not found region\" "
        "!= \"cdc initialize fail\" "
        "!= \"not_leader {\" "
        "!= \"stale command\" "
        "!= \"region_not_found {\" "
        "!= \"epoch_not_match {\" "
        "!= \"initializer.rs:163]: oneshot canceled\" "
        "!= \"initializer.rs:124]: scan canceled\" "
        "!= \"initializer.rs:213]: scan canceled\""
    )


def replace_url_query_params(url: str, replacements: dict[str, str | int]) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key, value in replacements.items():
        query[key] = [str(value)]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def absolutize_clinic_url(endpoint: str, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{endpoint.rstrip('/')}{url}"


def select_dashboard_link(
    endpoint: str,
    dashboard_resp: Any,
    dashboard_name: str,
    *,
    start_ms: int,
    end_ms: int,
) -> str | None:
    if not isinstance(dashboard_resp, list):
        return None
    for item in dashboard_resp:
        if str(item.get("name") or "") != dashboard_name:
            continue
        raw_url = str(item.get("url") or "").strip()
        if not raw_url:
            return None
        absolute_url = absolutize_clinic_url(endpoint, raw_url)
        return replace_url_query_params(absolute_url, {"from": start_ms, "to": end_ms})
    return None


def build_node_dashboard_link(endpoint: str, cluster_id: str, namespace: str, start_ms: int, end_ms: int) -> str:
    url = (
        f"{endpoint.rstrip('/')}/grafana/d/clinic-dataplane-node/dedicated-dataplane-node-info"
        f"?orgId=1&var-k8s_cluster=All&var-tidb_cluster={cluster_id}&var-namespace={namespace}"
        f"&from={start_ms}&to={end_ms}&var-instance=All"
    )
    return url


def build_loki_explore_link(
    endpoint: str,
    *,
    provider: str,
    region: str,
    namespace: str,
    start_ms: int,
    end_ms: int,
) -> str:
    datasource = f"Loki-{provider.lower()}-{region.lower()}"
    left = {
        "datasource": datasource,
        "queries": [
            {
                "refId": "A",
                "editorMode": "code",
                "expr": build_loki_filter(namespace),
                "queryType": "range",
            }
        ],
        "range": {"from": str(start_ms), "to": str(end_ms)},
    }
    return (
        f"{endpoint.rstrip('/')}/grafana/explore?orgId=1&left="
        f"{urllib.parse.quote(json.dumps(left, separators=(',', ':')))}"
    )


def inspect_cluster(client: InspectionClient, cluster_id: str, window: Window) -> dict[str, Any]:
    search_resp = client.search_clusters(cluster_id=cluster_id, show_deleted=True, limit=10)
    item = choose_cluster_item(search_resp, cluster_id)
    org_id = str(item.get("orgID") or "")
    detail = ensure_success(client.get_cluster_detail(org_id, cluster_id), f"fetch cluster detail {cluster_id}")
    topology_resp = client.get_topology(org_id, cluster_id)
    if isinstance(topology_resp, list):
        topology = topology_resp
    else:
        ensure_success(topology_resp, f"fetch cluster topology {cluster_id}")
        topology = list(topology_resp.get("data") or [])
    tikv_spec = extract_tikv_spec(topology)
    tikv_storage_type = extract_tikv_storage_type(detail)
    tikv_disk_devices = resolve_tikv_disk_devices(tikv_storage_type)
    tikv_disk_device_regex = build_device_regex(tikv_disk_devices)

    data_disk = next(
        (storage for storage in tikv_spec.get("storages") or [] if storage.get("name") == "data"),
        None,
    )
    per_tikv_memory_gi = float(tikv_spec.get("memory_gi") or 0)
    per_tikv_memory_bytes = per_tikv_memory_gi * 1024 * 1024 * 1024
    namespace = f"tidb{cluster_id}"

    end_iso = iso_utc(window.end)
    start_iso = iso_utc(window.start)
    prom_window = window.prom_window
    start_ms = epoch_milliseconds(window.start)
    end_ms = epoch_milliseconds(window.end)

    cpu_usage_q = (
        "((sum by (instance)(rate(process_cpu_seconds_total{component=\"tikv\"}[5m])))/"
        "(sum by (instance)(tikv_server_cpu_cores_quota)))*100"
    )
    cpu_avg_q = f"avg_over_time(({cpu_usage_q})[{prom_window}:5m])"
    cpu_max_q = f"max_over_time(({cpu_usage_q})[{prom_window}:5m])"
    cpu_baseline_7d_q = f"avg_over_time(({cpu_usage_q})[{MEMORY_LEAK_BASELINE_WINDOW}:5m])"
    memory_usage_q = (
        f"((process_resident_memory_bytes{{component=\"tikv\"}} / {per_tikv_memory_bytes}) * 100)"
    )
    memory_max_q = f"max_over_time(({memory_usage_q})[{prom_window}:5m])"
    memory_avg_q = f"avg_over_time(({memory_usage_q})[{prom_window}])"
    memory_baseline_7d_q = (
        f"avg_over_time(({memory_usage_q})[{MEMORY_LEAK_BASELINE_WINDOW}])"
    )
    store_usage_q = (
        '100 * sum by (instance)(tikv_store_size_bytes{type="used"}) / '
        '(sum by (instance)(tikv_store_size_bytes{type="used"}) + '
        'sum by (instance)(tikv_store_size_bytes{type="available"}))'
    )
    restart_q = f'changes(process_start_time_seconds{{component="tikv"}}[{prom_window}])'
    leader_q = 'sum(tikv_raftstore_region_count{type="leader"}) by (instance)'
    region_q = 'sum(tikv_raftstore_region_count{type="region"}) by (instance)'
    region_avg_q = f"avg_over_time(({region_q})[{prom_window}:5m])"
    region_baseline_7d_q = f"avg_over_time(({region_q})[{MEMORY_LEAK_BASELINE_WINDOW}:5m])"

    grpc_type_filter = "|".join(GRPC_TARGET_TYPES)
    grpc_qps_by_instance_q = (
        "sum by (instance)(rate("
        f'tikv_grpc_msg_duration_seconds_count{{type=~"{grpc_type_filter}"}}[5m]'
        "))"
    )
    grpc_qps_avg_q = f"avg_over_time(({grpc_qps_by_instance_q})[{prom_window}:5m])"
    grpc_qps_baseline_7d_q = f"avg_over_time(({grpc_qps_by_instance_q})[{MEMORY_LEAK_BASELINE_WINDOW}:5m])"
    grpc_message_count_q = (
        "sum(increase("
        f'tikv_grpc_msg_duration_seconds_count{{type=~"{grpc_type_filter}"}}[{prom_window}]'
        ")) by (instance,type)"
    )
    grpc_message_total_time_q = (
        "sum(increase("
        f'tikv_grpc_msg_duration_seconds_sum{{type=~"{grpc_type_filter}"}}[{prom_window}]'
        ")) by (instance,type)"
    )
    grpc_p99_raw_q = (
        "histogram_quantile(0.99, "
        f'sum by (le,type,instance)(rate(tikv_grpc_msg_duration_seconds_bucket{{type=~"{grpc_type_filter}"}}[5m]))'
        ")"
    )
    grpc_p99_q = (
        "max_over_time((histogram_quantile(0.99, "
        f'sum by (le,type,instance)(rate(tikv_grpc_msg_duration_seconds_bucket{{type=~"{grpc_type_filter}"}}[5m]))'
        f"))[{prom_window}:5m])"
    )
    critical_error_q = f"sum(increase(tikv_critical_error_total[{prom_window}])) by (type)"
    critical_error_raw_q = "sum by (instance,type)(tikv_critical_error_total)"
    channel_full_q = f"sum(increase(tikv_channel_full_total[{prom_window}])) by (type,db)"
    channel_full_raw_q = "sum by (instance,db,type)(tikv_channel_full_total)"
    scheduler_busy_q = f"sum(increase(tikv_scheduler_too_busy_total[{prom_window}])) by (instance,db,type,stage)"
    scheduler_busy_raw_q = "sum by (instance,db,type,stage)(tikv_scheduler_too_busy_total)"
    coprocessor_busy_q = (
        f'sum(increase(tikv_coprocessor_request_error{{type="full"}}[{prom_window}])) by (instance,db,type)'
    )
    coprocessor_busy_raw_q = 'sum by (instance,db,type)(tikv_coprocessor_request_error{type="full"})'
    server_report_q = f"sum(increase(tikv_server_report_failure_msg_total[{prom_window}])) by (type)"
    server_report_raw_q = "sum by (instance,store_id,type)(tikv_server_report_failure_msg_total)"
    grpc_fail_q = f"sum(increase(tikv_grpc_msg_fail_total[{prom_window}])) by (type)"
    grpc_fail_raw_q = "sum by (instance,type)(tikv_grpc_msg_fail_total)"
    grpc_fail_30m_q = "sum(increase(tikv_grpc_msg_fail_total[30m])) by (type)"
    hotspot_q = "pd_hotspot_status"
    compaction_raw_q = "max by (instance,cf) (tikv_engine_pending_compaction_bytes)"
    compaction_q = (
        f"max by (instance,cf) (max_over_time((tikv_engine_pending_compaction_bytes)[{prom_window}:5m]))"
    )
    write_stall_q = f"max by (instance,db,type) (max_over_time((tikv_engine_write_stall)[{prom_window}:5m]))"
    thread_cpu_q = 'sum by (instance,name)(rate(tikv_thread_cpu_seconds_total{component="tikv"}[5m]))'
    unified_read_running_tasks_q = (
        "max_over_time((sum by (instance)"
        f'(tikv_unified_read_pool_running_tasks{{name="{UNIFIED_READ_POOL_METRIC_NAME}"}})'
        f")[{prom_window}:5m])"
    )
    unified_read_time_used_total_q = (
        "sum by (instance)(increase("
        f'multilevel_level_elapsed{{name="{UNIFIED_READ_POOL_METRIC_NAME}",level="total"}}[{prom_window}]'
        ")) / 1000000"
    )
    mvcc_versions_q = (
        f"max_over_time((sum by (instance)(rate(tikv_storage_mvcc_versions_bucket[5m])))[{prom_window}:5m])"
    )
    mvcc_gc_delete_q = (
        f"max_over_time((sum by (instance)(rate(tikv_storage_mvcc_gc_delete_versions_bucket[5m])))[{prom_window}:5m])"
    )
    raft_append_log_p99_q = (
        "max_over_time((histogram_quantile(0.99, "
        "sum by (le,instance)(rate(tikv_raftstore_append_log_duration_seconds_bucket[5m]))"
        f"))[{prom_window}:5m])"
    )
    raft_commit_log_p99_q = (
        "max_over_time((histogram_quantile(0.99, "
        "sum by (le,instance)(rate(tikv_raftstore_commit_log_duration_seconds_bucket[5m]))"
        f"))[{prom_window}:5m])"
    )
    raft_apply_log_p99_q = (
        "max_over_time((histogram_quantile(0.99, "
        "sum by (le,instance)(rate(tikv_raftstore_apply_log_duration_seconds_bucket[5m]))"
        f"))[{prom_window}:5m])"
    )
    scheduler_throttle_total_q = (
        f"sum by (instance)(increase(tikv_scheduler_throttle_duration_seconds_sum[{prom_window}]))"
    )
    scheduler_discard_ratio_q = (
        f"max by (instance) (max_over_time((tikv_scheduler_discard_ratio)[{prom_window}:5m]))"
    )
    slow_score_q = f"max by (instance) (max_over_time((tikv_raftstore_slow_score)[{prom_window}:5m]))"
    raft_log_lag_p99_q = (
        "max_over_time((histogram_quantile(0.99, "
        "sum by (le,instance)(rate(tikv_raftstore_log_lag_bucket[5m]))"
        f"))[{prom_window}:5m])"
    )

    disk_util_q = build_peak_disk_query(
        f'rate(node_disk_io_time_seconds_total{{device=~"{tikv_disk_device_regex}"}}[5m])',
        namespace,
        prom_window,
        device_regex=tikv_disk_device_regex,
        pod_reduce="max",
    )
    disk_queue_q = build_peak_disk_query(
        f'rate(node_disk_io_time_weighted_seconds_total{{device=~"{tikv_disk_device_regex}"}}[5m])',
        namespace,
        prom_window,
        device_regex=tikv_disk_device_regex,
        pod_reduce="max",
    )
    disk_read_latency_q = build_peak_disk_query(
        f'(rate(node_disk_read_time_seconds_total{{device=~"{tikv_disk_device_regex}"}}[5m]) / rate(node_disk_reads_completed_total{{device=~"{tikv_disk_device_regex}"}}[5m]))',
        namespace,
        prom_window,
        device_regex=tikv_disk_device_regex,
        pod_reduce="max",
    )
    disk_write_latency_q = build_peak_disk_query(
        f'(rate(node_disk_write_time_seconds_total{{device=~"{tikv_disk_device_regex}"}}[5m]) / rate(node_disk_writes_completed_total{{device=~"{tikv_disk_device_regex}"}}[5m]))',
        namespace,
        prom_window,
        device_regex=tikv_disk_device_regex,
        pod_reduce="max",
    )
    disk_throughput_q = build_peak_disk_query(
        f'(irate(node_disk_read_bytes_total{{device=~"{tikv_disk_device_regex}"}}[5m]) + irate(node_disk_written_bytes_total{{device=~"{tikv_disk_device_regex}"}}[5m]))',
        namespace,
        prom_window,
        device_regex=tikv_disk_device_regex,
        pod_reduce="sum",
    )
    disk_iops_q = build_peak_disk_query(
        f'(rate(node_disk_reads_completed_total{{device=~"{tikv_disk_device_regex}"}}[5m]) + rate(node_disk_writes_completed_total{{device=~"{tikv_disk_device_regex}"}}[5m]))',
        namespace,
        prom_window,
        device_regex=tikv_disk_device_regex,
        pod_reduce="sum",
    )

    cpu_avg_resp = client.metrics.query_instant(cluster_id, cpu_avg_q, end_iso)
    cpu_max_resp = client.metrics.query_instant(cluster_id, cpu_max_q, end_iso)
    cpu_baseline_7d_resp = client.metrics.query_instant(cluster_id, cpu_baseline_7d_q, start_iso)
    cpu_range_resp = client.metrics.query_range(cluster_id, cpu_usage_q, start_iso, end_iso, step="300")
    memory_max_resp = client.metrics.query_instant(cluster_id, memory_max_q, end_iso)
    memory_avg_resp = client.metrics.query_instant(cluster_id, memory_avg_q, end_iso)
    memory_baseline_7d_resp = client.metrics.query_instant(cluster_id, memory_baseline_7d_q, start_iso)
    memory_range_resp = client.metrics.query_range(cluster_id, memory_usage_q, start_iso, end_iso, step="300")
    store_usage_resp = client.metrics.query_instant(cluster_id, store_usage_q, end_iso)
    store_usage_range_resp = client.metrics.query_range(cluster_id, store_usage_q, start_iso, end_iso, step="300")
    restart_resp = client.metrics.query_instant(cluster_id, restart_q, end_iso)
    leader_resp = client.metrics.query_instant(cluster_id, leader_q, end_iso)
    region_resp = client.metrics.query_instant(cluster_id, region_q, end_iso)
    region_avg_resp = client.metrics.query_instant(cluster_id, region_avg_q, end_iso)
    region_baseline_7d_resp = client.metrics.query_instant(cluster_id, region_baseline_7d_q, start_iso)
    grpc_qps_avg_resp = client.metrics.query_instant(cluster_id, grpc_qps_avg_q, end_iso)
    grpc_qps_baseline_7d_resp = client.metrics.query_instant(cluster_id, grpc_qps_baseline_7d_q, start_iso)
    grpc_message_count_resp = client.metrics.query_instant(cluster_id, grpc_message_count_q, end_iso)
    grpc_message_total_time_resp = client.metrics.query_instant(cluster_id, grpc_message_total_time_q, end_iso)
    grpc_p99_resp = client.metrics.query_instant(cluster_id, grpc_p99_q, end_iso)
    grpc_p99_range_resp = client.metrics.query_range(cluster_id, grpc_p99_raw_q, start_iso, end_iso, step="300")
    critical_error_resp = client.metrics.query_instant(cluster_id, critical_error_q, end_iso)
    critical_error_range_resp = client.metrics.query_range(cluster_id, critical_error_raw_q, start_iso, end_iso, step="300")
    channel_full_resp = client.metrics.query_instant(cluster_id, channel_full_q, end_iso)
    channel_full_range_resp = client.metrics.query_range(cluster_id, channel_full_raw_q, start_iso, end_iso, step="300")
    scheduler_busy_resp = client.metrics.query_instant(cluster_id, scheduler_busy_q, end_iso)
    scheduler_busy_range_resp = client.metrics.query_range(cluster_id, scheduler_busy_raw_q, start_iso, end_iso, step="300")
    coprocessor_busy_resp = client.metrics.query_instant(cluster_id, coprocessor_busy_q, end_iso)
    coprocessor_busy_range_resp = client.metrics.query_range(cluster_id, coprocessor_busy_raw_q, start_iso, end_iso, step="300")
    server_report_resp = client.metrics.query_instant(cluster_id, server_report_q, end_iso)
    server_report_range_resp = client.metrics.query_range(cluster_id, server_report_raw_q, start_iso, end_iso, step="300")
    grpc_fail_resp = client.metrics.query_instant(cluster_id, grpc_fail_q, end_iso)
    grpc_fail_range_resp = client.metrics.query_range(cluster_id, grpc_fail_raw_q, start_iso, end_iso, step="300")
    grpc_fail_30m_resp = client.metrics.query_range(cluster_id, grpc_fail_30m_q, start_iso, end_iso, step="300")
    hotspot_resp = client.metrics.query_instant(cluster_id, hotspot_q, end_iso)
    compaction_resp = client.metrics.query_instant(cluster_id, compaction_q, end_iso)
    compaction_range_resp = client.metrics.query_range(cluster_id, compaction_raw_q, start_iso, end_iso, step="300")
    write_stall_resp = client.metrics.query_instant(cluster_id, write_stall_q, end_iso)
    thread_cpu_resp = client.metrics.query_range(cluster_id, thread_cpu_q, start_iso, end_iso, step="300")
    unified_read_running_tasks_resp = client.metrics.query_instant(cluster_id, unified_read_running_tasks_q, end_iso)
    unified_read_time_used_total_resp = client.metrics.query_instant(cluster_id, unified_read_time_used_total_q, end_iso)
    mvcc_versions_resp = client.metrics.query_instant(cluster_id, mvcc_versions_q, end_iso)
    mvcc_gc_delete_resp = client.metrics.query_instant(cluster_id, mvcc_gc_delete_q, end_iso)
    raft_append_log_p99_resp = client.metrics.query_instant(cluster_id, raft_append_log_p99_q, end_iso)
    raft_commit_log_p99_resp = client.metrics.query_instant(cluster_id, raft_commit_log_p99_q, end_iso)
    raft_apply_log_p99_resp = client.metrics.query_instant(cluster_id, raft_apply_log_p99_q, end_iso)
    scheduler_throttle_total_resp = client.metrics.query_instant(cluster_id, scheduler_throttle_total_q, end_iso)
    scheduler_discard_ratio_resp = client.metrics.query_instant(cluster_id, scheduler_discard_ratio_q, end_iso)
    slow_score_resp = client.metrics.query_instant(cluster_id, slow_score_q, end_iso)
    raft_log_lag_p99_resp = client.metrics.query_instant(cluster_id, raft_log_lag_p99_q, end_iso)
    disk_util_resp = client.metrics.query_instant(cluster_id, disk_util_q, end_iso)
    disk_queue_resp = client.metrics.query_instant(cluster_id, disk_queue_q, end_iso)
    disk_read_latency_resp = client.metrics.query_instant(cluster_id, disk_read_latency_q, end_iso)
    disk_write_latency_resp = client.metrics.query_instant(cluster_id, disk_write_latency_q, end_iso)
    disk_throughput_resp = client.metrics.query_instant(cluster_id, disk_throughput_q, end_iso)
    disk_iops_resp = client.metrics.query_instant(cluster_id, disk_iops_q, end_iso)
    iops_exceed_resp = client.metrics.query_exceeded_iops_by_pod(
        cluster_id,
        "tikv",
        start_iso,
        end_iso,
        step="300",
        rate_window="2m",
    )
    node_iops_resp = client.metrics.query_node_disk_iops_by_pod(
        cluster_id,
        "tikv",
        start_iso,
        end_iso,
        step="300",
        rate_window="2m",
    )
    log_resp = client.loki_query_range(
        cluster_id,
        build_loki_filter(namespace),
        epoch_nanoseconds(window.start),
        epoch_nanoseconds(window.end),
        limit=200,
    )

    unknown_checks: list[str] = []
    for label, resp in [
        ("TiKV average CPU", cpu_avg_resp),
        ("TiKV max CPU", cpu_max_resp),
        ("TiKV CPU 7d baseline", cpu_baseline_7d_resp),
        ("TiKV CPU range", cpu_range_resp),
        ("TiKV memory", memory_max_resp),
        ("TiKV memory average", memory_avg_resp),
        ("TiKV memory 7d baseline", memory_baseline_7d_resp),
        ("TiKV memory range", memory_range_resp),
        ("TiKV store usage", store_usage_resp),
        ("TiKV store usage range", store_usage_range_resp),
        ("TiKV restart", restart_resp),
        ("Leader balance", leader_resp),
        ("Region balance", region_resp),
        ("Region average", region_avg_resp),
        ("Region 7d baseline", region_baseline_7d_resp),
        ("gRPC QPS average", grpc_qps_avg_resp),
        ("gRPC QPS 7d baseline", grpc_qps_baseline_7d_resp),
        ("gRPC message count", grpc_message_count_resp),
        ("gRPC message total time", grpc_message_total_time_resp),
        ("gRPC P99", grpc_p99_resp),
        ("gRPC P99 range", grpc_p99_range_resp),
        ("critical error", critical_error_resp),
        ("critical error range", critical_error_range_resp),
        ("channel full", channel_full_resp),
        ("channel full range", channel_full_range_resp),
        ("scheduler busy", scheduler_busy_resp),
        ("scheduler busy range", scheduler_busy_range_resp),
        ("coprocessor full", coprocessor_busy_resp),
        ("coprocessor full range", coprocessor_busy_range_resp),
        ("server report failure", server_report_resp),
        ("server report failure range", server_report_range_resp),
        ("gRPC fail", grpc_fail_resp),
        ("gRPC fail range", grpc_fail_range_resp),
        ("gRPC fail 30m", grpc_fail_30m_resp),
        ("hotspot", hotspot_resp),
        ("compaction pending bytes", compaction_resp),
        ("compaction range", compaction_range_resp),
        ("write stall", write_stall_resp),
        ("thread CPU", thread_cpu_resp),
        ("Unified Read Pool running tasks", unified_read_running_tasks_resp),
        ("Unified Read Pool time used total", unified_read_time_used_total_resp),
        ("MVCC versions", mvcc_versions_resp),
        ("MVCC GC delete versions", mvcc_gc_delete_resp),
        ("raft append log P99", raft_append_log_p99_resp),
        ("raft commit log P99", raft_commit_log_p99_resp),
        ("raft apply log P99", raft_apply_log_p99_resp),
        ("flow-control throttle duration", scheduler_throttle_total_resp),
        ("flow-control discard ratio", scheduler_discard_ratio_resp),
        ("TiKV slow score", slow_score_resp),
        ("raft log lag P99", raft_log_lag_p99_resp),
        ("disk util", disk_util_resp),
        ("disk queue", disk_queue_resp),
        ("disk read latency", disk_read_latency_resp),
        ("disk write latency", disk_write_latency_resp),
        ("disk throughput", disk_throughput_resp),
        ("disk iops", disk_iops_resp),
        ("IOPS exceeded", iops_exceed_resp),
        ("node disk IOPS", node_iops_resp),
        ("TiKV ERROR/FATAL logs", log_resp),
    ]:
        error = response_error(resp)
        if error:
            unknown_checks.append(f"{label}: {error}")

    cpu_avg_by_instance = vector_stats(cpu_avg_resp)
    cpu_max_by_instance = vector_stats(cpu_max_resp)
    cpu_baseline_7d_by_instance = vector_stats(cpu_baseline_7d_resp)
    memory_max_by_instance = vector_stats(memory_max_resp)
    memory_avg_by_instance = vector_stats(memory_avg_resp)
    memory_baseline_7d_by_instance = vector_stats(memory_baseline_7d_resp)
    store_usage_by_instance = vector_stats(store_usage_resp)
    restarts_by_instance = vector_stats(restart_resp)
    leader_by_instance = vector_stats(leader_resp)
    region_by_instance = vector_stats(region_resp)
    region_avg_by_instance = vector_stats(region_avg_resp)
    region_baseline_7d_by_instance = vector_stats(region_baseline_7d_resp)
    grpc_qps_avg_by_instance = vector_stats(grpc_qps_avg_resp)
    grpc_qps_baseline_7d_by_instance = vector_stats(grpc_qps_baseline_7d_resp)
    grpc_message_count = [
        {
            "instance": (entry.get("metric") or {}).get("instance", ""),
            "type": (entry.get("metric") or {}).get("type", ""),
            "value": safe_float((entry.get("value") or [None, None])[1]),
        }
        for entry in get_vector(grpc_message_count_resp)
    ]
    grpc_message_total_time = [
        {
            "instance": (entry.get("metric") or {}).get("instance", ""),
            "type": (entry.get("metric") or {}).get("type", ""),
            "value": safe_float((entry.get("value") or [None, None])[1]),
        }
        for entry in get_vector(grpc_message_total_time_resp)
    ]
    grpc_p99 = [
        {
            "instance": (entry.get("metric") or {}).get("instance", ""),
            "type": (entry.get("metric") or {}).get("type", ""),
            "value": safe_float((entry.get("value") or [None, None])[1]),
        }
        for entry in get_vector(grpc_p99_resp)
    ]
    hotspot_nonzero = current_nonzero_hotspots(hotspot_resp)
    thread_group_series = compute_thread_pool_group_series(thread_cpu_resp)
    thread_group_display = build_thread_pool_display_rows(thread_group_series)
    mvcc_versions_by_instance = vector_stats(mvcc_versions_resp)
    mvcc_gc_delete_by_instance = vector_stats(mvcc_gc_delete_resp)
    raft_append_log_p99_by_instance = vector_stats(raft_append_log_p99_resp)
    raft_commit_log_p99_by_instance = vector_stats(raft_commit_log_p99_resp)
    raft_apply_log_p99_by_instance = vector_stats(raft_apply_log_p99_resp)
    unified_read_running_tasks_by_instance = vector_stats(unified_read_running_tasks_resp)
    unified_read_time_used_total_seconds_by_instance = vector_stats(unified_read_time_used_total_resp)
    scheduler_throttle_seconds_by_instance = vector_stats(scheduler_throttle_total_resp)
    scheduler_discard_ratio_by_instance = vector_stats(scheduler_discard_ratio_resp)
    slow_score_by_instance = vector_stats(slow_score_resp)
    raft_log_lag_p99_by_instance = vector_stats(raft_log_lag_p99_resp)

    cpu_sustained_high: list[dict[str, Any]] = []
    for series in build_labeled_matrix_series(cpu_range_resp, ("instance",)):
        for window_item in find_threshold_windows(
            series["points"],
            threshold=CPU_SUSTAIN_WARN_PCT,
            min_points=ROLLING_30M_POINTS,
        ):
            cpu_sustained_high.append(
                {
                    "instance": series["labels"]["instance"],
                    "start_ts": window_item["start_ts"],
                    "end_ts": window_item["end_ts"],
                    "peak_pct": window_item["peak_value"],
                }
            )
    cpu_sustained_high.sort(key=lambda item: item["peak_pct"], reverse=True)

    memory_error_windows: list[dict[str, Any]] = []
    memory_warn_peaks: list[dict[str, Any]] = []
    for series in build_labeled_matrix_series(memory_range_resp, ("instance",)):
        peak_pct = max((value for _, value in series["points"]), default=0.0)
        if peak_pct >= MEMORY_WARN_PCT:
            memory_warn_peaks.append(
                {
                    "instance": series["labels"]["instance"],
                    "peak_pct": peak_pct,
                }
            )
        for window_item in find_threshold_windows(
            series["points"],
            threshold=MEMORY_ERROR_PCT,
            min_points=ROLLING_30M_POINTS,
        ):
            memory_error_windows.append(
                {
                    "instance": series["labels"]["instance"],
                    "start_ts": window_item["start_ts"],
                    "end_ts": window_item["end_ts"],
                    "peak_pct": window_item["peak_value"],
                }
            )
    memory_warn_peaks.sort(key=lambda item: item["peak_pct"], reverse=True)
    memory_error_windows.sort(key=lambda item: item["peak_pct"], reverse=True)

    memory_trend_risers: list[dict[str, Any]] = []
    memory_growth_expected_nodes: list[dict[str, Any]] = []
    memory_leak_warning_nodes: list[dict[str, Any]] = []
    comparable_memory_instances = sorted(
        set(memory_avg_by_instance) & set(memory_baseline_7d_by_instance)
    )
    memory_trend_cluster_current_avg = None
    memory_trend_cluster_baseline_avg = None
    memory_trend_cluster_delta_pct = None
    if comparable_memory_instances:
        comparable_count = len(comparable_memory_instances)
        memory_trend_cluster_current_avg = (
            sum(memory_avg_by_instance[instance] for instance in comparable_memory_instances)
            / comparable_count
        )
        memory_trend_cluster_baseline_avg = (
            sum(memory_baseline_7d_by_instance[instance] for instance in comparable_memory_instances)
            / comparable_count
        )
        memory_trend_cluster_delta_pct = (
            memory_trend_cluster_current_avg - memory_trend_cluster_baseline_avg
        )
        for instance in comparable_memory_instances:
            current_avg_pct = memory_avg_by_instance[instance]
            baseline_avg_pct = memory_baseline_7d_by_instance[instance]
            delta_pct = current_avg_pct - baseline_avg_pct
            if delta_pct < MEMORY_LEAK_OBSERVE_DELTA_PCT:
                continue
            support_signals: list[dict[str, Any]] = []

            region_current = region_avg_by_instance.get(instance)
            region_baseline = region_baseline_7d_by_instance.get(instance)
            region_delta = None if region_current is None or region_baseline is None else region_current - region_baseline
            region_delta_ratio_pct = ratio_delta_pct(region_current, region_baseline)
            if (
                region_delta is not None
                and region_delta_ratio_pct is not None
                and region_delta >= MEMORY_LEAK_EXPLAIN_REGION_DELTA_ABS
                and region_delta_ratio_pct >= MEMORY_LEAK_EXPLAIN_REGION_DELTA_RATIO_PCT
            ):
                support_signals.append(
                    {
                        "kind": "region",
                        "current": region_current,
                        "baseline": region_baseline,
                        "delta": region_delta,
                        "delta_ratio_pct": region_delta_ratio_pct,
                    }
                )

            qps_current = grpc_qps_avg_by_instance.get(instance)
            qps_baseline = grpc_qps_baseline_7d_by_instance.get(instance)
            qps_delta = None if qps_current is None or qps_baseline is None else qps_current - qps_baseline
            qps_delta_ratio_pct = ratio_delta_pct(qps_current, qps_baseline)
            if (
                qps_delta is not None
                and qps_delta_ratio_pct is not None
                and qps_delta >= MEMORY_LEAK_EXPLAIN_QPS_DELTA_ABS
                and qps_delta_ratio_pct >= MEMORY_LEAK_EXPLAIN_QPS_DELTA_RATIO_PCT
            ):
                support_signals.append(
                    {
                        "kind": "qps",
                        "current": qps_current,
                        "baseline": qps_baseline,
                        "delta": qps_delta,
                        "delta_ratio_pct": qps_delta_ratio_pct,
                    }
                )

            cpu_current = cpu_avg_by_instance.get(instance)
            cpu_baseline = cpu_baseline_7d_by_instance.get(instance)
            cpu_delta_pct = None if cpu_current is None or cpu_baseline is None else cpu_current - cpu_baseline
            if (
                cpu_delta_pct is not None
                and cpu_delta_pct >= MEMORY_LEAK_EXPLAIN_CPU_DELTA_PCT
            ):
                support_signals.append(
                    {
                        "kind": "cpu",
                        "current": cpu_current,
                        "baseline": cpu_baseline,
                        "delta_pct": cpu_delta_pct,
                    }
                )
            item = {
                "instance": instance,
                "current_avg_pct": current_avg_pct,
                "baseline_avg_pct": baseline_avg_pct,
                "delta_pct": delta_pct,
                "support_signals": support_signals,
            }
            memory_trend_risers.append(item)
            if delta_pct >= MEMORY_LEAK_WARN_DELTA_PCT:
                if support_signals:
                    memory_growth_expected_nodes.append(item)
                else:
                    memory_leak_warning_nodes.append(item)
    memory_trend_risers.sort(key=lambda item: item["delta_pct"], reverse=True)
    memory_growth_expected_nodes.sort(key=lambda item: item["delta_pct"], reverse=True)
    memory_leak_warning_nodes.sort(key=lambda item: item["delta_pct"], reverse=True)

    grpc_fail_spikes: list[dict[str, Any]] = []
    for series in build_labeled_matrix_series(grpc_fail_30m_resp, ("type",)):
        for window_item in find_threshold_windows(
            series["points"],
            threshold=GRPC_FAIL_30M_WARN_THRESHOLD,
            min_points=1,
            lookback_seconds=ROLLING_30M_SECONDS,
        ):
            grpc_fail_spikes.append(
                {
                    "type": series["labels"]["type"],
                    "start_ts": window_item["start_ts"],
                    "end_ts": window_item["end_ts"],
                    "peak_count": window_item["peak_value"],
                }
            )
    grpc_fail_spikes.sort(key=lambda item: item["peak_count"], reverse=True)

    grpc_p99_sustained: list[dict[str, Any]] = []
    for series in build_labeled_matrix_series(grpc_p99_range_resp, ("instance", "type")):
        grpc_type = series["labels"]["type"]
        if grpc_type not in {"kv_prewrite", "kv_commit"}:
            continue
        for window_item in find_threshold_windows(
            series["points"],
            threshold=GRPC_P99_WARN_SECONDS,
            min_points=ROLLING_15M_POINTS,
        ):
            grpc_p99_sustained.append(
                {
                    "instance": series["labels"]["instance"],
                    "type": grpc_type,
                    "start_ts": window_item["start_ts"],
                    "end_ts": window_item["end_ts"],
                    "peak_seconds": window_item["peak_value"],
                }
            )
    grpc_p99_sustained.sort(key=lambda item: item["peak_seconds"], reverse=True)

    store_usage_fast_growth: list[dict[str, Any]] = []
    for series in build_labeled_matrix_series(store_usage_range_resp, ("instance",)):
        peak_window = find_peak_window(
            series["points"],
            lookback_points=ROLLING_30M_POINTS,
            floor_value=STORE_USAGE_RISE_WARN_PCT,
            end_floor_value=STORE_USAGE_WARN_PCT,
        )
        if peak_window is None:
            continue
        store_usage_fast_growth.append(
            {
                "instance": series["labels"]["instance"],
                "start_ts": peak_window["start_ts"],
                "end_ts": peak_window["end_ts"],
                "delta_pct": peak_window["delta_value"],
                "end_pct": peak_window["end_value"],
            }
        )
    store_usage_fast_growth.sort(key=lambda item: item["delta_pct"], reverse=True)

    compaction_pending_sustained: list[dict[str, Any]] = []
    compaction_pending_peak_breaches: list[dict[str, Any]] = []
    for series in build_labeled_matrix_series(compaction_range_resp, ("instance", "cf")):
        peak_bytes = max((value for _, value in series["points"]), default=0.0)
        if peak_bytes >= COMPACTION_PENDING_PEAK_WARN_BYTES:
            compaction_pending_peak_breaches.append(
                {
                    "instance": series["labels"]["instance"],
                    "cf": series["labels"]["cf"],
                    "peak_bytes": peak_bytes,
                }
            )
        for window_item in find_threshold_windows(
            series["points"],
            threshold=COMPACTION_PENDING_SUSTAIN_WARN_BYTES,
            min_points=ROLLING_30M_POINTS,
        ):
            compaction_pending_sustained.append(
                {
                    "instance": series["labels"]["instance"],
                    "cf": series["labels"]["cf"],
                    "start_ts": window_item["start_ts"],
                    "end_ts": window_item["end_ts"],
                    "peak_bytes": window_item["peak_value"],
                }
            )
    compaction_pending_sustained.sort(key=lambda item: item["peak_bytes"], reverse=True)
    compaction_pending_peak_breaches.sort(key=lambda item: item["peak_bytes"], reverse=True)

    unified_read_hotspots = find_unified_read_pool_hotspots(thread_group_series)

    compaction_entries = []
    for entry in get_vector(compaction_resp):
        value = safe_float((entry.get("value") or [None, None])[1])
        if value is None:
            continue
        compaction_entries.append(
            {
                "instance": (entry.get("metric") or {}).get("instance", ""),
                "cf": (entry.get("metric") or {}).get("cf", ""),
                "value_bytes": value,
            }
        )
    compaction_peak = max((entry["value_bytes"] for entry in compaction_entries), default=0.0)
    compaction_peak_entry = max(compaction_entries, key=lambda item: item["value_bytes"], default=None)

    write_stall_entries = []
    for entry in get_vector(write_stall_resp):
        value = safe_float((entry.get("value") or [None, None])[1])
        if value is None:
            continue
        write_stall_entries.append(
            {
                "instance": (entry.get("metric") or {}).get("instance", ""),
                "type": (entry.get("metric") or {}).get("type", ""),
                "db": (entry.get("metric") or {}).get("db", ""),
                "value": value,
            }
        )
    write_stall_peak = max((entry["value"] for entry in write_stall_entries), default=0.0)

    disk_util_peak, disk_util_pod = max_from_vector(disk_util_resp), None
    if get_vector(disk_util_resp):
        top = max(get_vector(disk_util_resp), key=lambda item: safe_float((item.get("value") or [None, None])[1]) or -1)
        disk_util_pod = (top.get("metric") or {}).get("pod", "")
    disk_queue_peak, disk_queue_pod = max_from_vector(disk_queue_resp), None
    if get_vector(disk_queue_resp):
        top = max(get_vector(disk_queue_resp), key=lambda item: safe_float((item.get("value") or [None, None])[1]) or -1)
        disk_queue_pod = (top.get("metric") or {}).get("pod", "")
    disk_read_latency_peak = max_from_vector(disk_read_latency_resp)
    disk_write_latency_peak = max_from_vector(disk_write_latency_resp)
    disk_throughput_peak = max_from_vector(disk_throughput_resp)
    disk_iops_peak = max_from_vector(disk_iops_resp)
    disk_iops_peak_pod = None
    if get_vector(disk_iops_resp):
        top = max(get_vector(disk_iops_resp), key=lambda item: safe_float((item.get("value") or [None, None])[1]) or -1)
        disk_iops_peak_pod = (top.get("metric") or {}).get("pod", "")

    iops_exceed_peak = 0.0
    iops_exceed_pod = ""
    for entry in get_matrix(iops_exceed_resp):
        values = [safe_float(value[1]) for value in entry.get("values") or []]
        numeric = [value for value in values if value is not None]
        if not numeric:
            continue
        local_peak = max(numeric)
        if local_peak > iops_exceed_peak:
            iops_exceed_peak = local_peak
            iops_exceed_pod = (entry.get("metric") or {}).get("pod", "")

    node_iops_peak = 0.0
    node_iops_pod = ""
    for entry in get_matrix(node_iops_resp):
        metric = entry.get("metric") or {}
        device = str(metric.get("device") or "")
        if device.startswith("dm-"):
            continue
        if tikv_disk_devices and device not in tikv_disk_devices:
            continue
        values = [safe_float(value[1]) for value in entry.get("values") or []]
        numeric = [value for value in values if value is not None]
        if not numeric:
            continue
        local_peak = max(numeric)
        if local_peak > node_iops_peak:
            node_iops_peak = local_peak
            node_iops_pod = str(metric.get("pod") or "")

    unified_read_running_tasks_peak_instance, unified_read_running_tasks_peak = top_vector_stat(
        unified_read_running_tasks_by_instance
    )
    (
        unified_read_time_used_total_peak_instance,
        unified_read_time_used_total_peak,
    ) = top_vector_stat(unified_read_time_used_total_seconds_by_instance)
    unified_read_time_used_avg_workers_peak = (
        (unified_read_time_used_total_peak / max(window.seconds, 1))
        if unified_read_time_used_total_peak is not None
        else None
    )
    scheduler_throttle_peak_instance, scheduler_throttle_peak = top_vector_stat(
        scheduler_throttle_seconds_by_instance
    )
    scheduler_throttle_total_seconds = sum(scheduler_throttle_seconds_by_instance.values())
    scheduler_discard_ratio_peak_instance, scheduler_discard_ratio_peak = top_vector_stat(
        scheduler_discard_ratio_by_instance
    )
    slow_score_peak_instance, slow_score_peak = top_vector_stat(slow_score_by_instance)
    raft_log_lag_p99_peak_instance, raft_log_lag_p99_peak = top_vector_stat(
        raft_log_lag_p99_by_instance
    )

    findings: list[dict[str, str]] = []

    def add_finding(severity: str, title: str, detail_text: str) -> None:
        findings.append({"severity": severity, "title": title, "detail": detail_text})

    store_usage_peak = max(store_usage_by_instance.values()) if store_usage_by_instance else 0.0
    if store_usage_fast_growth:
        top_growth = store_usage_fast_growth[0]
        add_finding(
            "Warning",
            "TiKV store usage is rising too quickly",
            f"Instance {top_growth['instance']} rose by {top_growth['delta_pct']:.2f}% within 30 minutes and reached {top_growth['end_pct']:.2f}% during {format_window_window(top_growth['start_ts'], top_growth['end_ts'], window.timezone_name)}.",
        )

    cpu_avg_peak = max(cpu_avg_by_instance.values()) if cpu_avg_by_instance else 0.0
    cpu_max_peak = max(cpu_max_by_instance.values()) if cpu_max_by_instance else 0.0
    if cpu_sustained_high:
        top_cpu = cpu_sustained_high[0]
        add_finding(
            "Warning",
            "TiKV CPU stayed above 85% for more than 30 minutes",
            f"Instance {top_cpu['instance']} stayed above 85% during {format_window_window(top_cpu['start_ts'], top_cpu['end_ts'], window.timezone_name)} and peaked at {top_cpu['peak_pct']:.2f}%.",
        )

    memory_max_peak = max(memory_max_by_instance.values()) if memory_max_by_instance else 0.0
    if memory_error_windows:
        top_memory_error = memory_error_windows[0]
        add_finding(
            "Error",
            "TiKV memory stayed above 90% for more than 30 minutes",
            f"Instance {top_memory_error['instance']} stayed above 90% during {format_window_window(top_memory_error['start_ts'], top_memory_error['end_ts'], window.timezone_name)} and peaked at {top_memory_error['peak_pct']:.2f}%.",
        )
    elif memory_max_peak >= MEMORY_WARN_PCT:
        top_memory_warning = memory_warn_peaks[0]
        add_finding(
            "Warning",
            "TiKV memory usage reached the watch threshold",
            f"Instance {top_memory_warning['instance']} peaked at {top_memory_warning['peak_pct']:.2f}%, above the 85% watch threshold.",
        )
    if memory_leak_warning_nodes:
        top_memory_trend = memory_leak_warning_nodes[0]
        add_finding(
            "Warning",
            "Potential TiKV memory-leak phenomenon was detected",
            f"Instance {top_memory_trend['instance']} averaged {top_memory_trend['current_avg_pct']:.2f}% memory usage in the inspection window versus {top_memory_trend['baseline_avg_pct']:.2f}% across the previous 7 days (delta {top_memory_trend['delta_pct']:.2f} pp), and no matching region-count, workload-QPS, or CPU growth was found to explain the rise. Compare this node with workload changes and keep it under watch for memory accumulation.",
        )

    thread_overloads = []
    for group, info in thread_group_display.items():
        if group == "unified_read_pool":
            continue
        if info.get("status") == "Unknown":
            continue
        threshold = info["threshold"]
        peak = info["peak_cpu_cores"]
        thread_count = int(info["thread_count"])
        if thread_count <= 0 or threshold <= 0:
            continue
        if peak > threshold:
            thread_overloads.append(
                f"{THREAD_GROUP_DISPLAY.get(group, group)} on {info['instance']} {peak:.2f}/{threshold:.2f} cores ({thread_count} threads)"
            )
    if thread_overloads:
        add_finding(
            "Warning",
            "Critical thread pools show CPU saturation risk",
            "; ".join(thread_overloads[:5]) + ("." if len(thread_overloads) <= 5 else "; additional thread pools omitted."),
        )
    if unified_read_hotspots:
        top_hotspot = unified_read_hotspots[0]
        add_finding(
            "Warning",
            "Unified read pool shows a single-node hotspot",
            f"Instance {top_hotspot['instance']} stayed above {UNIFIED_READ_HOT_PCT:.0f}% while peer nodes stayed at or below {UNIFIED_READ_PEER_LOW_PCT:.0f}% during {format_window_window(top_hotspot['start_ts'], top_hotspot['end_ts'], window.timezone_name)}. This pattern is consistent with hotspot pressure.",
        )

    total_restarts = int(sum(restarts_by_instance.values()))
    if total_restarts > 0:
        add_finding("Warning", "TiKV restarted during the inspection window", f"A total of {total_restarts} TiKV restarts were detected.")

    critical_total = alert_counter_total(critical_error_range_resp, critical_error_resp)
    if critical_total > 0:
        add_finding("Error", "TiKV critical errors were detected", f"The total critical error count was {critical_total:.0f}.")

    channel_full_total = alert_counter_total(channel_full_range_resp, channel_full_resp)
    if channel_full_total > 0:
        add_finding("Warning", "Channel full events were detected", f"The total channel full count was {channel_full_total:.0f}.")

    scheduler_busy_total = alert_counter_total(scheduler_busy_range_resp, scheduler_busy_resp)
    if scheduler_busy_total > 0:
        add_finding("Warning", "Scheduler busy events were detected", f"The total scheduler busy count was {scheduler_busy_total:.0f}.")

    coprocessor_busy_total = alert_counter_total(coprocessor_busy_range_resp, coprocessor_busy_resp)
    if coprocessor_busy_total > 0:
        add_finding("Warning", "Coprocessor full events were detected", f"The total coprocessor full count was {coprocessor_busy_total:.0f}.")

    server_report_total = alert_counter_total(server_report_range_resp, server_report_resp)
    if server_report_total > 0:
        add_finding("Warning", "Server report failures were detected", f"The total server report failure count was {server_report_total:.0f}.")

    grpc_fail_total = alert_counter_total(grpc_fail_range_resp, grpc_fail_resp)
    if grpc_fail_spikes:
        top_fail_spike = grpc_fail_spikes[0]
        add_finding(
            "Warning",
            "gRPC failed-message count spiked within 30 minutes",
            f"Message type {top_fail_spike['type']} reached {top_fail_spike['peak_count']:.0f} failed messages in a rolling 30-minute window ending at {format_ts_local(top_fail_spike['end_ts'], window.timezone_name)}. Keep this metric under watch.",
        )

    kv_prewrite_peak = max((item["value"] or 0.0) for item in grpc_p99 if item["type"] == "kv_prewrite")
    kv_commit_peak = max((item["value"] or 0.0) for item in grpc_p99 if item["type"] == "kv_commit")
    if grpc_p99_sustained:
        top_grpc_p99 = grpc_p99_sustained[0]
        add_finding(
            "Warning",
            "gRPC message duration P99 stayed above 1000 ms",
            f"{top_grpc_p99['type']} on {top_grpc_p99['instance']} stayed above 1000 ms during {format_window_window(top_grpc_p99['start_ts'], top_grpc_p99['end_ts'], window.timezone_name)} and peaked at {top_grpc_p99['peak_seconds'] * 1000:.2f} ms.",
        )

    if scheduler_discard_ratio_peak is not None and scheduler_discard_ratio_peak > 0:
        add_finding(
            "Warning",
            "Flow control discard ratio became positive",
            f"Instance {scheduler_discard_ratio_peak_instance or 'N/A'} peaked at discard ratio {scheduler_discard_ratio_peak:.2f}. Correlate this with compaction pending bytes and write pressure.",
        )
    if slow_score_peak is not None and slow_score_peak >= SLOW_SCORE_ALERT_VALUE:
        add_finding(
            "Warning",
            "TiKV slow score reached the high-risk line",
            f"Instance {slow_score_peak_instance or 'N/A'} reached slow score {slow_score_peak:.0f}. If PD slow-store handling or leader eviction also appeared in this period, check the node immediately.",
        )
    if raft_log_lag_p99_peak is not None and raft_log_lag_p99_peak >= RAFT_LOG_LAG_P99_ALERT:
        add_finding(
            "Warning",
            "Raft log lag P99 is high",
            f"Instance {raft_log_lag_p99_peak_instance or 'N/A'} peaked at raft log lag P99 {raft_log_lag_p99_peak:.0f}. Correlate this with restart, recovery, disk, or hotspot signals.",
        )

    if iops_exceed_peak >= IOPS_EXCEED_WARN_THRESHOLD:
        add_finding("Warning", "The TiKV data disk hit the IOPS ceiling", f"The peak exceeded_iops value was {iops_exceed_peak:.2f} on {iops_exceed_pod or 'N/A'}.")
    if disk_read_latency_peak is not None and disk_read_latency_peak > 0.020:
        add_finding("Warning", "Disk read latency is high", f"The peak read latency was {disk_read_latency_peak * 1000:.2f} ms.")
    if disk_write_latency_peak is not None and disk_write_latency_peak > 0.020:
        add_finding("Warning", "Disk write latency is high", f"The peak write latency was {disk_write_latency_peak * 1000:.2f} ms.")

    if compaction_pending_peak_breaches or compaction_pending_sustained:
        peak_instance = compaction_peak_entry["instance"] if compaction_peak_entry else "N/A"
        peak_cf = compaction_peak_entry["cf"] if compaction_peak_entry else "N/A"
        detail_text = (
            f"The peak pending bytes reached {format_gib(compaction_peak)} on instance {peak_instance} / CF {peak_cf}."
        )
        if compaction_pending_sustained:
            top_compaction = compaction_pending_sustained[0]
            detail_text = (
                f"Pending bytes on instance {top_compaction['instance']} / CF {top_compaction['cf']} stayed above 200 GiB during "
                f"{format_window_window(top_compaction['start_ts'], top_compaction['end_ts'], window.timezone_name)} and peaked at {format_gib(top_compaction['peak_bytes'])}."
            )
        add_finding(
            "Warning",
            "Compaction pending bytes are high",
            detail_text,
        )

    if write_stall_peak > 0:
        add_finding("Warning", "Write stall was detected", f"The peak write stall value was {write_stall_peak:.2f}.")

    log_errors = log_line_count(log_resp)
    if log_errors > 0:
        add_finding("Warning", "TiKV ERROR/FATAL logs were detected", f"A total of {log_errors} ERROR/FATAL log lines matched the filter.")

    error_findings = [item for item in findings if item["severity"] == "Error"]
    warning_findings = [item for item in findings if item["severity"] == "Warning"]
    watch_items = top_watch_items(warning_findings or findings)

    verdict = "Healthy"
    if error_findings:
        verdict = "Error"
    elif warning_findings:
        verdict = "Warning"
    elif unknown_checks:
        verdict = "Needs Review"

    if verdict == "Error":
        health_summary = "The cluster has error-level findings and should be checked immediately."
        recommended_action = "Check now"
    elif verdict == "Warning":
        focus_text = ", ".join(watch_items) if watch_items else "the warning-tagged metrics"
        health_summary = (
            "The cluster has warning-level findings in this window and should stay "
            f"under watch. Primary watch items: {focus_text}."
        )
        recommended_action = "Keep watching"
    elif verdict == "Needs Review":
        health_summary = "The cluster cannot be fully assessed because part of the inspection data is unavailable."
        recommended_action = "Review missing signals"
    else:
        health_summary = "The cluster looks healthy in this inspection window."
        recommended_action = "Routine watch"

    leader_values = list(leader_by_instance.values())
    region_values = list(region_by_instance.values())
    leader_ratio = max(leader_values) / min(leader_values) if leader_values and min(leader_values) else None
    region_ratio = max(region_values) / min(region_values) if region_values and min(region_values) else None
    tikv_pods = collect_metric_pods(
        disk_util_resp,
        disk_queue_resp,
        disk_read_latency_resp,
        disk_write_latency_resp,
        disk_throughput_resp,
        disk_iops_resp,
        iops_exceed_resp,
        node_iops_resp,
    )
    dashboard_links: dict[str, str] = {}
    dashboard_resp = client.get_dashboards(org_id, cluster_id, epoch_seconds(window.start), epoch_seconds(window.end))
    tikv_dashboard_link = select_dashboard_link(
        client.endpoint,
        dashboard_resp,
        "TiKV-Details-Cluster-Clinic",
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if tikv_dashboard_link:
        dashboard_links["tikv_details"] = tikv_dashboard_link
    dashboard_links["node_details"] = build_node_dashboard_link(
        client.endpoint,
        cluster_id,
        namespace,
        start_ms,
        end_ms,
    )
    dashboard_links["log_explore"] = build_loki_explore_link(
        client.endpoint,
        provider=str(item.get("clusterProviderName") or detail.get("provider") or ""),
        region=str(item.get("clusterRegionName") or detail.get("region") or ""),
        namespace=namespace,
        start_ms=start_ms,
        end_ms=end_ms,
    )

    clinic_link = (
        f"{client.endpoint}/portal/#/orgs/{org_id}/clusters/{cluster_id}"
        f"?from={epoch_seconds(window.start)}&to={epoch_seconds(window.end)}"
    )

    analysis = {
        "health_summary": health_summary,
        "recommended_action": recommended_action,
        "watch_items": watch_items,
        "cpu_sustained_high": cpu_sustained_high,
        "memory_error_windows": memory_error_windows,
        "memory_warn_peaks": memory_warn_peaks,
        "memory_trend_risers": memory_trend_risers,
        "memory_growth_expected_nodes": memory_growth_expected_nodes,
        "memory_leak_warning_nodes": memory_leak_warning_nodes,
        "memory_trend_cluster_current_avg": memory_trend_cluster_current_avg,
        "memory_trend_cluster_baseline_avg": memory_trend_cluster_baseline_avg,
        "memory_trend_cluster_delta_pct": memory_trend_cluster_delta_pct,
        "memory_trend_comparable_count": len(comparable_memory_instances),
        "grpc_fail_spikes": grpc_fail_spikes,
        "grpc_p99_sustained": grpc_p99_sustained,
        "unified_read_hotspots": unified_read_hotspots,
        "store_usage_fast_growth": store_usage_fast_growth,
        "compaction_pending_sustained": compaction_pending_sustained,
        "compaction_pending_peak_breaches": compaction_pending_peak_breaches,
    }

    report = {
        "cluster_id": cluster_id,
        "cluster_name": item.get("clusterName") or detail.get("name") or cluster_id,
        "tenant_id": item.get("tenantID", ""),
        "tenant_name": item.get("tenantName", ""),
        "org_id": org_id,
        "provider": item.get("clusterProviderName") or detail.get("provider") or "",
        "region": item.get("clusterRegionName") or detail.get("region") or "",
        "version": item.get("clusterVersion") or detail.get("version") or "",
        "status": item.get("clusterStatus") or detail.get("status") or "",
        "project_id": item.get("projectID") or detail.get("projectID") or "",
        "control_plane_arch": item.get("controlPlaneArch") or detail.get("controlPlaneArch") or "",
        "topology_summary": {
            **extract_topology_summary(topology),
            **(item.get("topology") or {}),
        },
        "tikv_topology": tikv_spec,
        "tikv_storage_type": tikv_storage_type,
        "data_disk": data_disk or {},
        "window": {
            "timezone": window.timezone_name,
            "start_local": to_local_string(window.start, window.timezone_name),
            "end_local": to_local_string(window.end, window.timezone_name),
            "start_utc": start_iso,
            "end_utc": end_iso,
            "seconds": window.seconds,
        },
        "clinic_link": clinic_link,
        "observability_scope": {
            "tikv_target": "tikv-details",
            "node_target": "dedicated-dataplane-node-info",
            "log_target": "tikv",
        },
        "dashboard_links": dashboard_links,
        "time_range_ms": {"from": start_ms, "to": end_ms},
        "verdict": verdict,
        "health_summary": health_summary,
        "recommended_action": recommended_action,
        "watch_items": watch_items,
        "findings": findings,
        "unknown_checks": unknown_checks,
        "analysis": analysis,
        "metrics": {
            "store_usage_by_instance": store_usage_by_instance,
            "cpu_avg_by_instance": cpu_avg_by_instance,
            "cpu_max_by_instance": cpu_max_by_instance,
            "cpu_baseline_7d_by_instance": cpu_baseline_7d_by_instance,
            "memory_max_by_instance": memory_max_by_instance,
            "memory_avg_by_instance": memory_avg_by_instance,
            "memory_baseline_7d_by_instance": memory_baseline_7d_by_instance,
            "restarts_by_instance": restarts_by_instance,
            "leader_by_instance": leader_by_instance,
            "region_by_instance": region_by_instance,
            "region_avg_by_instance": region_avg_by_instance,
            "region_baseline_7d_by_instance": region_baseline_7d_by_instance,
            "leader_ratio": leader_ratio,
            "region_ratio": region_ratio,
            "grpc_qps_avg_by_instance": grpc_qps_avg_by_instance,
            "grpc_qps_baseline_7d_by_instance": grpc_qps_baseline_7d_by_instance,
            "grpc_message_count": grpc_message_count,
            "grpc_message_total_time": grpc_message_total_time,
            "critical_error_total": critical_total,
            "channel_full_total": channel_full_total,
            "scheduler_busy_total": scheduler_busy_total,
            "coprocessor_busy_total": coprocessor_busy_total,
            "server_report_failure_total": server_report_total,
            "grpc_fail_total": grpc_fail_total,
            "grpc_p99": grpc_p99,
            "thread_group_series": thread_group_series,
            "thread_group_display": thread_group_display,
            "mvcc_versions_peak_by_instance": mvcc_versions_by_instance,
            "mvcc_gc_delete_peak_by_instance": mvcc_gc_delete_by_instance,
            "raft_append_log_p99_by_instance": raft_append_log_p99_by_instance,
            "raft_commit_log_p99_by_instance": raft_commit_log_p99_by_instance,
            "raft_apply_log_p99_by_instance": raft_apply_log_p99_by_instance,
            "unified_read_running_tasks_peak_by_instance": unified_read_running_tasks_by_instance,
            "unified_read_running_tasks_peak": unified_read_running_tasks_peak,
            "unified_read_running_tasks_peak_instance": unified_read_running_tasks_peak_instance,
            "unified_read_time_used_total_seconds_by_instance": unified_read_time_used_total_seconds_by_instance,
            "unified_read_time_used_total_seconds_peak": unified_read_time_used_total_peak,
            "unified_read_time_used_total_peak_instance": unified_read_time_used_total_peak_instance,
            "unified_read_time_used_avg_workers_peak": unified_read_time_used_avg_workers_peak,
            "scheduler_throttle_seconds_by_instance": scheduler_throttle_seconds_by_instance,
            "scheduler_throttle_total_seconds": scheduler_throttle_total_seconds,
            "scheduler_throttle_peak_seconds": scheduler_throttle_peak,
            "scheduler_throttle_peak_instance": scheduler_throttle_peak_instance,
            "scheduler_discard_ratio_by_instance": scheduler_discard_ratio_by_instance,
            "scheduler_discard_ratio_peak": scheduler_discard_ratio_peak,
            "scheduler_discard_ratio_peak_instance": scheduler_discard_ratio_peak_instance,
            "slow_score_by_instance": slow_score_by_instance,
            "slow_score_peak": slow_score_peak,
            "slow_score_peak_instance": slow_score_peak_instance,
            "raft_log_lag_p99_by_instance": raft_log_lag_p99_by_instance,
            "raft_log_lag_p99_peak": raft_log_lag_p99_peak,
            "raft_log_lag_p99_peak_instance": raft_log_lag_p99_peak_instance,
            "disk_util_peak": disk_util_peak,
            "disk_util_peak_pod": disk_util_pod,
            "disk_queue_peak": disk_queue_peak,
            "disk_queue_peak_pod": disk_queue_pod,
            "disk_read_latency_peak": disk_read_latency_peak,
            "disk_write_latency_peak": disk_write_latency_peak,
            "disk_throughput_peak_bytes_per_s": disk_throughput_peak,
            "disk_iops_peak": disk_iops_peak,
            "disk_iops_peak_pod": disk_iops_peak_pod,
            "node_iops_peak": node_iops_peak,
            "node_iops_peak_pod": node_iops_pod,
            "iops_exceed_peak": iops_exceed_peak,
            "iops_exceed_peak_pod": iops_exceed_pod,
            "compaction_pending_peak_bytes": compaction_peak,
            "compaction_pending_peak_entry": compaction_peak_entry,
            "write_stall_peak": write_stall_peak,
            "hotspots": hotspot_nonzero,
            "error_log_count": log_errors,
            "tikv_pods": tikv_pods,
            "disk_device_scope": {
                "storage_type": tikv_storage_type or "unknown",
                "devices": tikv_disk_devices,
                "device_regex": tikv_disk_device_regex,
            },
        },
    }
    report["markdown"] = build_markdown(report)
    return report


def build_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    findings = report["findings"]
    unknown_checks = report.get("unknown_checks") or []
    analysis = report.get("analysis") or {}
    topology = report.get("topology_summary") or {}
    data_disk = report.get("data_disk") or {}
    scope = report.get("observability_scope") or {}
    dashboard_links = report.get("dashboard_links") or {}
    time_range_ms = report.get("time_range_ms") or {}

    grpc_p99_peaks: dict[str, float] = {}
    for item in metrics["grpc_p99"]:
        gtype = item["type"]
        value = item["value"] or 0.0
        grpc_p99_peaks[gtype] = max(grpc_p99_peaks.get(gtype, 0.0), value)

    grpc_count_by_type: dict[str, float] = defaultdict(float)
    for item in metrics["grpc_message_count"]:
        value = item["value"]
        if value is not None:
            grpc_count_by_type[item["type"]] += value

    grpc_total_time_by_type: dict[str, float] = defaultdict(float)
    for item in metrics["grpc_message_total_time"]:
        value = item["value"]
        if value is not None:
            grpc_total_time_by_type[item["type"]] += value

    grpc_avg_duration_by_type: dict[str, float] = {}
    for gtype in GRPC_TARGET_TYPES:
        count = grpc_count_by_type.get(gtype, 0.0)
        total_time = grpc_total_time_by_type.get(gtype, 0.0)
        if count > 0:
            grpc_avg_duration_by_type[gtype] = total_time / count

    def grpc_total_count(*grpc_types: str) -> float:
        return sum(grpc_count_by_type.get(gtype, 0.0) for gtype in grpc_types)

    def grpc_total_qps(*grpc_types: str) -> float:
        return grpc_total_count(*grpc_types) / max(report["window"]["seconds"], 1)

    def grpc_avg_duration(*grpc_types: str) -> float | None:
        count = grpc_total_count(*grpc_types)
        total_time = sum(grpc_total_time_by_type.get(gtype, 0.0) for gtype in grpc_types)
        if count <= 0:
            return None
        return total_time / count

    def grpc_peak_p99(*grpc_types: str) -> float | None:
        values = [
            grpc_p99_peaks[gtype]
            for gtype in grpc_types
            if gtype in grpc_p99_peaks
        ]
        return max(values) if values else None

    thread_lines = []
    for group, info in (metrics.get("thread_group_display") or {}).items():
        if info.get("status") == "Unknown":
            thread_lines.append(
                f"- {THREAD_GROUP_DISPLAY.get(group, group)}: {severity_badge('Unknown')}"
            )
            continue
        peak_util_pct = info.get("peak_util_pct")
        peak_util_text = f"{peak_util_pct:.2f}%" if isinstance(peak_util_pct, (int, float)) else "N/A"
        thread_lines.append(
            f"- {THREAD_GROUP_DISPLAY.get(group, group)}: top instance `{info['instance']}`, peak {info['peak_cpu_cores']:.2f} cores, "
            f"threshold {info['threshold']:.2f} cores, thread count {int(info['thread_count'])}, utilization {peak_util_text}, "
            f"status {severity_badge('Warning') if info['status'] == 'Over threshold' else severity_badge('Healthy')}"
        )
    thread_over_threshold = any(
        info.get("status") == "Over threshold"
        for info in (metrics.get("thread_group_display") or {}).values()
        if isinstance(info, dict)
    )

    hotspot_lines = []
    for item in metrics["hotspots"][:5]:
        hotspot_lines.append(
            f"- {item['type']}: store={item['store']}, address={item['address']}, value={item['value']}"
        )

    finding_lines = []
    if findings:
        for item in findings:
            finding_lines.append(
                f"- {severity_badge(item['severity'])} {item['title']}: {item['detail']}"
            )
    else:
        finding_lines.append(
            f"- {severity_badge('Healthy')} No error or warning findings were produced in this inspection run."
        )

    unknown_lines = [f"- {item}" for item in unknown_checks]
    watch_items = analysis.get("watch_items") or report.get("watch_items") or []
    watch_items_text = ", ".join(watch_items) if watch_items else "None"

    leader_ratio = metrics["leader_ratio"]
    region_ratio = metrics["region_ratio"]
    leader_ratio_text = f"{leader_ratio:.3f}" if leader_ratio is not None else "N/A"
    region_ratio_text = f"{region_ratio:.3f}" if region_ratio is not None else "N/A"

    store_values = list(metrics["store_usage_by_instance"].values())
    store_range = f"{min(store_values):.2f}% ~ {max(store_values):.2f}%" if store_values else "N/A"
    store_peak = max(store_values) if store_values else None
    cpu_avg_values = list(metrics["cpu_avg_by_instance"].values())
    cpu_max_values = list(metrics["cpu_max_by_instance"].values())
    mem_values = list(metrics["memory_max_by_instance"].values())
    mem_avg_values = list((metrics.get("memory_avg_by_instance") or {}).values())
    cpu_avg_peak = max(cpu_avg_values) if cpu_avg_values else None
    cpu_max_peak = max(cpu_max_values) if cpu_max_values else None
    mem_peak = max(mem_values) if mem_values else None
    mem_avg_peak = max(mem_avg_values) if mem_avg_values else None
    memory_trend_cluster_current_avg = analysis.get("memory_trend_cluster_current_avg")
    memory_trend_cluster_baseline_avg = analysis.get("memory_trend_cluster_baseline_avg")
    memory_trend_cluster_delta_pct = analysis.get("memory_trend_cluster_delta_pct")
    memory_trend_comparable_count = int(analysis.get("memory_trend_comparable_count") or 0)
    restart_total = int(sum(metrics["restarts_by_instance"].values()))
    disk_scope = metrics.get("disk_device_scope") or {}
    focused_devices = ", ".join(disk_scope.get("devices") or []) or "all non-dm devices"
    mvcc_versions_peak = max(metrics["mvcc_versions_peak_by_instance"].values()) if metrics["mvcc_versions_peak_by_instance"] else None
    mvcc_gc_delete_peak = max(metrics["mvcc_gc_delete_peak_by_instance"].values()) if metrics["mvcc_gc_delete_peak_by_instance"] else None
    raft_append_log_p99_peak = max(metrics["raft_append_log_p99_by_instance"].values()) if metrics["raft_append_log_p99_by_instance"] else None
    raft_commit_log_p99_peak = max(metrics["raft_commit_log_p99_by_instance"].values()) if metrics["raft_commit_log_p99_by_instance"] else None
    raft_apply_log_p99_peak = max(metrics["raft_apply_log_p99_by_instance"].values()) if metrics["raft_apply_log_p99_by_instance"] else None

    error_summary = "The cluster looks stable in the error-signal panels."
    if metrics["critical_error_total"] > 0:
        error_summary = "Error-level signals exist and should be checked immediately."
    elif (
        metrics["channel_full_total"] > 0
        or metrics["scheduler_busy_total"] > 0
        or metrics["coprocessor_busy_total"] > 0
        or metrics["server_report_failure_total"] > 0
    ):
        error_summary = "Busy or report-failure signals exist and should stay under watch."

    grpc_summary = "gRPC volume and latency look broadly stable."
    if analysis.get("grpc_fail_spikes") or analysis.get("grpc_p99_sustained"):
        grpc_summary = "The gRPC path has sustained latency or short-window failure spikes and should be correlated with hotspots and thread-pool pressure."

    cpu_summary = "TiKV CPU and memory usage look broadly healthy."
    if (
        analysis.get("cpu_sustained_high")
        or analysis.get("memory_error_windows")
        or analysis.get("memory_leak_warning_nodes")
        or (mem_peak or 0.0) >= MEMORY_WARN_PCT
    ):
        cpu_summary = "Some TiKV nodes show sustained CPU or memory pressure, or a rising average-memory trend versus the previous 7-day baseline, and should be checked together with hotspot and thread-pool signals."
    elif analysis.get("memory_growth_expected_nodes"):
        cpu_summary = "Some TiKV nodes show a rising average-memory trend versus the previous 7-day baseline, but the same nodes also show strong region-count, workload-QPS, or CPU growth, so the memory increase currently looks workload-driven rather than leak-like."

    thread_summary = "No critical thread pool shows obvious CPU saturation."
    if analysis.get("unified_read_hotspots") or thread_over_threshold:
        thread_summary = "Thread-pool pressure is visible. Prioritize unified read pool imbalance because it usually points to hotspot concentration."

    disk_usage_summary = "Disk usage and Region/Leader distribution look broadly healthy."
    if analysis.get("store_usage_fast_growth") or (region_ratio or 0.0) > 1.2 or (leader_ratio or 0.0) > 1.2:
        disk_usage_summary = "Disk usage or Region/Leader distribution shows skew or fast growth and should be checked against hotspot history and scaling events."

    disk_load_summary = "No obvious TiKV data-disk or raft-write bottleneck is visible."
    if (
        (metrics["disk_read_latency_peak"] or 0.0) > 0.020
        or (metrics["disk_write_latency_peak"] or 0.0) > 0.020
        or metrics["iops_exceed_peak"] >= IOPS_EXCEED_WARN_THRESHOLD
        or metrics["write_stall_peak"] > 0
        or (metrics.get("scheduler_discard_ratio_peak") or 0.0) > 0
        or (metrics.get("scheduler_throttle_total_seconds") or 0.0) > 0
        or (metrics.get("slow_score_peak") or 0.0) >= SLOW_SCORE_ALERT_VALUE
        or (metrics.get("raft_log_lag_p99_peak") or 0.0) >= RAFT_LOG_LAG_P99_ALERT
        or analysis.get("compaction_pending_sustained")
        or analysis.get("compaction_pending_peak_breaches")
    ):
        disk_load_summary = "The TiKV data-disk or raft-write path shows pressure and should be checked together with compaction and write-stall signals."

    log_summary = "No escalated ERROR/FATAL logs matched the default filter."
    if metrics["error_log_count"] > 0:
        log_summary = "ERROR/FATAL logs matched the default filter and should be reviewed together with the metric findings."

    problem_lines: list[str] = []
    if analysis.get("grpc_fail_spikes") or analysis.get("grpc_p99_sustained"):
        problem_lines.append("- Prioritize the gRPC tail-latency and failure path, and correlate it with hotspot distribution and critical thread pools.")
    if (
        analysis.get("compaction_pending_sustained")
        or analysis.get("compaction_pending_peak_breaches")
        or metrics["write_stall_peak"] > 0
    ):
        peak_compaction = metrics.get("compaction_pending_peak_entry") or {}
        problem_lines.append(
            f"- Prioritize TiKV data-disk and compaction pressure on `{metrics['disk_util_peak_pod'] or 'N/A'}` "
            f"and `{peak_compaction.get('instance') or 'N/A'}`."
        )
    if analysis.get("memory_error_windows") or analysis.get("cpu_sustained_high") or analysis.get("unified_read_hotspots"):
        problem_lines.append("- Check whether the high-CPU or high-memory nodes overlap with hotspots, replica skew, or concentrated coprocessor load.")
    if analysis.get("memory_leak_warning_nodes"):
        problem_lines.append("- Compare the rising-memory nodes with the previous 7-day baseline and current workload changes. If the same nodes keep rising, collect heap or memory profiles to rule out leak-like accumulation.")
    elif analysis.get("memory_growth_expected_nodes"):
        problem_lines.append("- The rising-memory nodes also show strong region-count, workload-QPS, or CPU growth, so the current memory increase looks workload-driven. Keep watching capacity if that workload trend continues.")
    if (metrics.get("scheduler_throttle_total_seconds") or 0.0) > 0 or (metrics.get("scheduler_discard_ratio_peak") or 0.0) > 0:
        problem_lines.append("- Correlate flow-control throttle and discard signals with compaction pending bytes, write stalls, and ServerIsBusy counters.")
    if (metrics.get("slow_score_peak") or 0.0) >= SLOW_SCORE_ALERT_VALUE or (metrics.get("raft_log_lag_p99_peak") or 0.0) >= RAFT_LOG_LAG_P99_ALERT:
        problem_lines.append("- Check whether slow-score or raft-log-lag outliers overlap with restart, recovery, disk, or hotspot evidence on the same TiKV instance.")
    if not problem_lines and not unknown_lines:
        problem_lines.append("- No further follow-up action is required from this inspection run.")

    lines = [
        f"## {report['cluster_name']} (`{report['cluster_id']}`)",
        "",
        f"- Inspection Window: {report['window']['start_local']} ~ {report['window']['end_local']}",
        f"- Cluster ID: `{report['cluster_id']}`",
        f"- Provider / Region: {report['provider']} / {report['region']}",
        f"- Version: {report['version']}",
        f"- Clinic: [cluster overview]({report['clinic_link']})",
        f"- TiKV Monitoring Target: `{scope.get('tikv_target', 'tikv-details')}`",
        f"- TiKV Dashboard: [tikv-details]({dashboard_links['tikv_details']})" if dashboard_links.get("tikv_details") else "- TiKV Dashboard: N/A",
        f"- Node Monitoring Target: `{scope.get('node_target', 'dedicated-dataplane-node-info')}`",
        f"- Node Dashboard: [dedicated-dataplane-node-info]({dashboard_links['node_details']})" if dashboard_links.get("node_details") else "- Node Dashboard: N/A",
        f"- Log Filter Target: `{scope.get('log_target', 'tikv')}`",
        f"- Log Explore Link: [tikv explore]({dashboard_links['log_explore']})" if dashboard_links.get("log_explore") else "- Log Explore Link: N/A",
        f"- Scale: TiDB {topology.get('tidb', 'N/A')} / TiKV {topology.get('tikv', 'N/A')} / PD {topology.get('pd', 'N/A')} / TiFlash {topology.get('tiflash', 'N/A')}",
        f"- TiKV Data-Disk Topology: {report.get('tikv_storage_type') or 'unknown'}; focus devices `{focused_devices}`",
        f"- TiKV Data-Disk Spec: {data_disk.get('type', 'N/A')} / {data_disk.get('size_gi', 'N/A')} Gi / {data_disk.get('iops', 'N/A')} IOPS / {data_disk.get('throughput_mbs', 'N/A')} MB/s",
        "",
        "### Summary",
        f"- Verdict: {severity_badge(str(report['verdict']))} {report['verdict']}",
        f"- Health Summary: {report.get('health_summary') or analysis.get('health_summary') or 'N/A'}",
        f"- Recommended Action: {action_badge(str(report.get('recommended_action') or analysis.get('recommended_action') or 'Review missing signals'))} {report.get('recommended_action') or analysis.get('recommended_action') or 'N/A'}",
        f"- Watch Items: {watch_items_text}",
        "",
        "### Key Findings",
        *finding_lines,
        "",
        "### Error Signals",
        f"- TiKV critical errors total: {metrics['critical_error_total']:.0f}",
        f"- Server-is-busy signals: channel full {metrics['channel_full_total']:.0f}, scheduler busy {metrics['scheduler_busy_total']:.0f}, coprocessor full {metrics['coprocessor_busy_total']:.0f}",
        f"- Server report failures: {metrics['server_report_failure_total']:.0f}",
        f"- TiKV restarts: {restart_total}",
        f"- Summary: {error_summary}",
        "",
        "### gRPC QPS and Latency",
        f"- Write-path gRPC message count: kv_prewrite {format_count(grpc_total_count('kv_prewrite'))}, kv_commit {format_count(grpc_total_count('kv_commit'))}",
        f"- Read-path gRPC message count: coprocessor {format_count(grpc_total_count('coprocessor'))}, kv_get {format_count(grpc_total_count('kv_get'))}, kv_batch_get {format_count(grpc_total_count(*GRPC_BATCH_GET_TYPES))}",
        f"- Write-path average gRPC QPS: kv_prewrite {format_count(grpc_total_qps('kv_prewrite'))}, kv_commit {format_count(grpc_total_qps('kv_commit'))}",
        f"- Read-path average gRPC QPS: coprocessor {format_count(grpc_total_qps('coprocessor'))}, kv_get {format_count(grpc_total_qps('kv_get'))}, kv_batch_get {format_count(grpc_total_qps(*GRPC_BATCH_GET_TYPES))}",
        f"- gRPC message failures: {metrics['grpc_fail_total']:.0f}",
        f"- Write-path gRPC P99: kv_prewrite {format_ms_from_seconds(grpc_peak_p99('kv_prewrite'))}, kv_commit {format_ms_from_seconds(grpc_peak_p99('kv_commit'))}",
        f"- Read-path gRPC P99: coprocessor {format_ms_from_seconds(grpc_peak_p99('coprocessor'))}, kv_get {format_ms_from_seconds(grpc_peak_p99('kv_get'))}, kv_batch_get {format_ms_from_seconds(grpc_peak_p99(*GRPC_BATCH_GET_TYPES))}",
        f"- Write-path average gRPC duration: kv_prewrite {format_ms_from_seconds(grpc_avg_duration('kv_prewrite'))}, kv_commit {format_ms_from_seconds(grpc_avg_duration('kv_commit'))}",
        f"- Read-path average gRPC duration: coprocessor {format_ms_from_seconds(grpc_avg_duration('coprocessor'))}, kv_get {format_ms_from_seconds(grpc_avg_duration('kv_get'))}, kv_batch_get {format_ms_from_seconds(grpc_avg_duration(*GRPC_BATCH_GET_TYPES))}",
        (
            f"- Sustained gRPC P99 windows: "
            + "; ".join(
                f"{item['type']} on {item['instance']} during {format_window_window(item['start_ts'], item['end_ts'], report['window']['timezone'])} (peak {item['peak_seconds'] * 1000:.2f} ms)"
                for item in (analysis.get('grpc_p99_sustained') or [])[:3]
            )
        ) if analysis.get("grpc_p99_sustained") else "- Sustained gRPC P99 windows: None",
        (
            f"- 30-minute gRPC fail spikes: "
            + "; ".join(
                f"{item['type']} during {format_window_window(item['start_ts'], item['end_ts'], report['window']['timezone'])} (peak {item['peak_count']:.0f})"
                for item in (analysis.get('grpc_fail_spikes') or [])[:3]
            )
        ) if analysis.get("grpc_fail_spikes") else "- 30-minute gRPC fail spikes: None",
        f"- Summary: {grpc_summary}",
        "",
        "### CPU and Memory",
        f"- CPU: peak per-instance average {format_pct(cpu_avg_peak)}, peak per-instance instantaneous {format_pct(cpu_max_peak)}",
        f"- Memory: peak per-instance usage {format_pct(mem_peak)}",
        f"- Memory average usage in inspection window: peak per-instance {format_pct(mem_avg_peak)}, cluster average {format_pct(memory_trend_cluster_current_avg)}",
        (
            f"- Memory average vs previous 7d baseline: current {format_pct(memory_trend_cluster_current_avg)}, baseline {format_pct(memory_trend_cluster_baseline_avg)}, delta {format_pct_point_delta(memory_trend_cluster_delta_pct)}, comparable TiKV nodes {memory_trend_comparable_count}"
        ) if memory_trend_comparable_count else "- Memory average vs previous 7d baseline: Unknown",
        (
            f"- Sustained CPU windows: "
            + "; ".join(
                f"{item['instance']} during {format_window_window(item['start_ts'], item['end_ts'], report['window']['timezone'])} (peak {item['peak_pct']:.2f}%)"
                for item in (analysis.get('cpu_sustained_high') or [])[:3]
            )
        ) if analysis.get("cpu_sustained_high") else "- Sustained CPU windows: None",
        (
            f"- Memory >=90% windows: "
            + "; ".join(
                f"{item['instance']} during {format_window_window(item['start_ts'], item['end_ts'], report['window']['timezone'])} (peak {item['peak_pct']:.2f}%)"
                for item in (analysis.get('memory_error_windows') or [])[:3]
            )
        ) if analysis.get("memory_error_windows") else "- Memory >=90% windows: None",
        (
            f"- Rising average-memory nodes vs previous 7d baseline: "
            + "; ".join(
                f"{item['instance']} current {item['current_avg_pct']:.2f}% vs baseline {item['baseline_avg_pct']:.2f}% ({format_pct_point_delta(item['delta_pct'])})"
                for item in (analysis.get('memory_trend_risers') or [])[:3]
            )
        ) if analysis.get("memory_trend_risers") else "- Rising average-memory nodes vs previous 7d baseline: None",
        (
            f"- Memory growth explained by region / QPS / CPU growth: "
            + "; ".join(
                f"{item['instance']} {build_memory_growth_supports(item)}"
                for item in (analysis.get('memory_growth_expected_nodes') or [])[:3]
            )
        ) if analysis.get("memory_growth_expected_nodes") else "- Memory growth explained by region / QPS / CPU growth: None",
        (
            f"- Potential memory-leak warning nodes: "
            + "; ".join(
                f"{item['instance']} current {item['current_avg_pct']:.2f}% vs baseline {item['baseline_avg_pct']:.2f}% ({format_pct_point_delta(item['delta_pct'])})"
                for item in (analysis.get('memory_leak_warning_nodes') or [])[:3]
            )
        ) if analysis.get("memory_leak_warning_nodes") else "- Potential memory-leak warning nodes: None",
        f"- Summary: {cpu_summary}",
        "",
        "### Critical Thread Pool CPU",
        *thread_lines,
        f"- Unified Read Pool running tasks peak: {format_count(metrics.get('unified_read_running_tasks_peak'))}, top instance `{metrics.get('unified_read_running_tasks_peak_instance') or 'N/A'}`",
        f"- Unified Read Pool time used total: {format_seconds(metrics.get('unified_read_time_used_total_seconds_peak'))}, average busy workers {format_count(metrics.get('unified_read_time_used_avg_workers_peak'))}, top instance `{metrics.get('unified_read_time_used_total_peak_instance') or 'N/A'}`",
        (
            f"- Unified read pool hotspot windows: "
            + "; ".join(
                f"{item['instance']} during {format_window_window(item['start_ts'], item['end_ts'], report['window']['timezone'])} (peak {item['peak_pct']:.2f}%, peer peak {item['peer_peak_pct']:.2f}%)"
                for item in (analysis.get('unified_read_hotspots') or [])[:3]
            )
        ) if analysis.get("unified_read_hotspots") else "- Unified read pool hotspot windows: None",
        f"- Summary: {thread_summary}",
        "",
        "### Disk Usage and Region Count",
        f"- Store size usage range: {store_range}",
        f"- Region distribution max/min: {region_ratio_text}",
        f"- Leader distribution max/min: {leader_ratio_text}",
        (
            f"- Fast store-growth windows: "
            + "; ".join(
                f"{item['instance']} during {format_window_window(item['start_ts'], item['end_ts'], report['window']['timezone'])} (delta {item['delta_pct']:.2f}%, end {item['end_pct']:.2f}%)"
                for item in (analysis.get('store_usage_fast_growth') or [])[:3]
            )
        ) if analysis.get("store_usage_fast_growth") else "- Fast store-growth windows: None",
        f"- Summary: {disk_usage_summary}",
        "",
        "### Disk Load",
        f"- Raft Engine P99: append log {format_ms_from_seconds(raft_append_log_p99_peak)}, commit log {format_ms_from_seconds(raft_commit_log_p99_peak)}, apply log {format_ms_from_seconds(raft_apply_log_p99_peak)}",
        f"- Flow-control throttle duration total: {format_seconds(metrics.get('scheduler_throttle_total_seconds'))}, top instance `{metrics.get('scheduler_throttle_peak_instance') or 'N/A'}`",
        f"- Flow-control discard ratio peak: {format_count(metrics.get('scheduler_discard_ratio_peak'))}, top instance `{metrics.get('scheduler_discard_ratio_peak_instance') or 'N/A'}`",
        f"- TiKV slow score peak: {format_count(metrics.get('slow_score_peak'))}, top instance `{metrics.get('slow_score_peak_instance') or 'N/A'}`",
        f"- Raft log lag P99 peak: {format_count(metrics.get('raft_log_lag_p99_peak'))}, top instance `{metrics.get('raft_log_lag_p99_peak_instance') or 'N/A'}`",
        f"- Disk I/O Util: {format_pct((metrics['disk_util_peak'] or 0.0) * 100)}, top node `{metrics['disk_util_peak_pod'] or 'N/A'}`",
        f"- Aggregated read/write disk IOPS peak: {format_iops(metrics['disk_iops_peak'])}, top node `{metrics['disk_iops_peak_pod'] or 'N/A'}`",
        f"- Single-device IOPS peak: {format_iops(metrics['node_iops_peak'])}, top node `{metrics['node_iops_peak_pod'] or 'N/A'}`",
        f"- Read disk wait latency: {format_ms_from_seconds(metrics['disk_read_latency_peak'])}",
        f"- Write disk wait latency: {format_ms_from_seconds(metrics['disk_write_latency_peak'])}",
        f"- Aggregated read/write disk bandwidth peak: {format_mib_per_s(metrics['disk_throughput_peak_bytes_per_s'])}",
        f"- Disk queue depth peak: {metrics['disk_queue_peak']:.2f}" if metrics["disk_queue_peak"] is not None else "- Disk queue depth peak: N/A",
        f"- Exceeded IOPS peak: {metrics['iops_exceed_peak']:.2f}",
        f"- Compaction pending bytes peak: {format_gib(metrics['compaction_pending_peak_bytes'])}",
        (
            f"- Compaction pending peak breaches: "
            + "; ".join(
                f"{item['instance']} / {item['cf']} ({format_gib(item['peak_bytes'])})"
                for item in (analysis.get('compaction_pending_peak_breaches') or [])[:3]
            )
        ) if analysis.get("compaction_pending_peak_breaches") else "- Compaction pending peak breaches: None",
        (
            f"- Compaction pending windows: "
            + "; ".join(
                f"{item['instance']} / {item['cf']} during {format_window_window(item['start_ts'], item['end_ts'], report['window']['timezone'])} (peak {format_gib(item['peak_bytes'])})"
                for item in (analysis.get('compaction_pending_sustained') or [])[:3]
            )
        ) if analysis.get("compaction_pending_sustained") else "- Compaction pending windows: None",
        f"- Write stall peak: {metrics['write_stall_peak']:.2f}",
        f"- MVCC versions peak: {format_count(mvcc_versions_peak)}; MVCC GC delete versions peak: {format_count(mvcc_gc_delete_peak)}",
        f"- Summary: {disk_load_summary}",
        "",
        "### ERROR/WARN Logs",
        f"- Log filter scope: `{scope.get('log_target', 'tikv')}`, time window from={time_range_ms.get('from', 'N/A')} / to={time_range_ms.get('to', 'N/A')}",
        f"- Automatic ERROR/FATAL matches: {metrics['error_log_count']}",
        f"- Summary: {log_summary}",
        "",
        "### Follow-up Investigation",
        *problem_lines,
    ]

    if hotspot_lines:
        lines.extend(["", "Observed hotspots:", *hotspot_lines])
    else:
        lines.append("- `pd_hotspot_status` has no non-zero hotspot entries in the current snapshot.")

    if unknown_lines:
        lines.extend(["", "Data completeness notes:", *unknown_lines])

    return "\n".join(lines).strip() + "\n"


def build_daily_report_markdown(
    reports: list[dict[str, Any]],
    *,
    report_date: str,
    window: Window,
    failures: list[dict[str, str]] | None = None,
) -> str:
    verdict_counts: dict[str, int] = defaultdict(int)
    for report in reports:
        verdict_counts[str(report.get("verdict") or "Unknown")] += 1

    lines = [
        f"- Inspection Date: {report_date}",
        f"- Inspection Window: {to_local_string(window.start, window.timezone_name)} ~ {to_local_string(window.end, window.timezone_name)}",
        "- Data Sources: Clinic API / Clinic Data Proxy / Loki",
        "- Cluster Summary: "
        f"{severity_badge('Healthy')} {verdict_counts.get('Healthy', 0)}, "
        f"{severity_badge('Warning')} {verdict_counts.get('Warning', 0)}, "
        f"{severity_badge('Error')} {verdict_counts.get('Error', 0)}, "
        f"{severity_badge('Needs Review')} {verdict_counts.get('Needs Review', 0)}",
        "",
    ]
    if failures:
        lines.extend(["## Failed Clusters", ""])
        for item in failures:
            lines.append(f"- `{item['cluster_id']}`: {item['error']}")
        lines.append("")
    for report in reports:
        lines.append(report["markdown"].rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_discover(args: argparse.Namespace) -> int:
    client = InspectionClient(args.env_file)
    resp = client.search_clusters(query=args.query, show_deleted=args.show_deleted, limit=args.limit)
    error = response_error(resp)
    if error:
        print(json.dumps({"error": error}, ensure_ascii=False, indent=2))
        return 1
    items = []
    for item in resp.get("items") or []:
        if args.active_only and str(item.get("clusterStatus", "")).lower() != "active":
            continue
        if args.dedicated_only and str(item.get("clusterDeployTypeV2", "")).lower() != "dedicated":
            continue
        items.append(
            {
                "cluster_id": item.get("clusterID", ""),
                "cluster_name": item.get("clusterName", ""),
                "tenant_id": item.get("tenantID", ""),
                "tenant_name": item.get("tenantName", ""),
                "org_id": item.get("orgID", ""),
                "status": item.get("clusterStatus", ""),
                "deploy_type_v2": item.get("clusterDeployTypeV2", ""),
                "provider": item.get("clusterProviderName", ""),
                "region": item.get("clusterRegionName", ""),
                "version": item.get("clusterVersion", ""),
                "project_id": item.get("projectID", ""),
                "topology": item.get("topology", {}),
            }
        )
    print(json.dumps({"items": items}, ensure_ascii=False, indent=2))
    return 0


def run_inspect(args: argparse.Namespace) -> int:
    client = InspectionClient(args.env_file)
    try:
        window = resolve_window(
            args.report_date,
            args.start,
            args.end,
            args.relative_window,
            args.timezone,
        )
        report = inspect_cluster(client, args.cluster_id, window)
    except (RuntimeError, ValueError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["markdown"], end="")
    return 0


def run_daily_report(args: argparse.Namespace) -> int:
    client = InspectionClient(args.env_file)
    try:
        window = resolve_window(
            args.report_date,
            args.start,
            args.end,
            args.relative_window,
            args.timezone,
        )
    except ValueError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    report_date = resolve_report_date_value(args.report_date, window)
    cluster_ids = normalize_cluster_ids(args.cluster_id)
    if not cluster_ids:
        print("error: no cluster IDs were provided", file=sys.stderr)
        return 1

    reports: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for cluster_id in cluster_ids:
        try:
            reports.append(inspect_cluster(client, cluster_id, window))
        except RuntimeError as exc:
            if not args.continue_on_error:
                if args.json:
                    print(json.dumps({"error": str(exc), "cluster_id": cluster_id}, ensure_ascii=False, indent=2))
                else:
                    print(f"error: {cluster_id}: {exc}", file=sys.stderr)
                return 1
            failures.append({"cluster_id": cluster_id, "error": str(exc)})

    markdown = build_daily_report_markdown(reports, report_date=report_date, window=window, failures=failures)
    payload = {
        "report_date": report_date,
        "window": {
            "timezone": window.timezone_name,
            "start_local": to_local_string(window.start, window.timezone_name),
            "end_local": to_local_string(window.end, window.timezone_name),
            "start_utc": iso_utc(window.start),
            "end_utc": iso_utc(window.end),
            "seconds": window.seconds,
        },
        "cluster_ids": cluster_ids,
        "reports": reports,
        "failures": failures,
        "markdown": markdown,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(markdown, end="")
    return 0 if reports else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dedicated daily inspection for TiDB Cloud Dedicated clusters")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Discover active dedicated clusters from a Clinic search query")
    discover.add_argument("--query", required=True, help="Clinic search query, typically tenant ID or cluster search text")
    discover.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    discover.add_argument("--limit", type=int, default=50)
    discover.add_argument("--show-deleted", action="store_true")
    discover.add_argument("--active-only", action="store_true", default=True)
    discover.add_argument("--dedicated-only", action="store_true", default=True)
    discover.set_defaults(func=run_discover)

    inspect = subparsers.add_parser("inspect", help="Inspect one dedicated cluster and output an English markdown section")
    inspect.add_argument("--cluster-id", required=True)
    inspect.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    inspect.add_argument("--report-date")
    inspect.add_argument("--start")
    inspect.add_argument("--end")
    inspect.add_argument(
        "--relative-window",
        help="Relative inspection window ending at now or at `--end`, for example 2h, 6h, or 1d",
    )
    inspect.add_argument("--timezone", default=DEFAULT_TZ)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=run_inspect)

    daily_report = subparsers.add_parser(
        "daily-report",
        help="Inspect one or more dedicated clusters and output an English daily markdown report",
    )
    daily_report.add_argument(
        "--cluster-id",
        action="append",
        required=True,
        help="Cluster ID. Repeat the flag or pass a comma-separated list.",
    )
    daily_report.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    daily_report.add_argument("--report-date")
    daily_report.add_argument("--start")
    daily_report.add_argument("--end")
    daily_report.add_argument(
        "--relative-window",
        help="Relative inspection window ending at now or at `--end`, for example 2h, 6h, or 1d",
    )
    daily_report.add_argument("--timezone", default=DEFAULT_TZ)
    daily_report.add_argument("--continue-on-error", action="store_true")
    daily_report.add_argument("--json", action="store_true")
    daily_report.set_defaults(func=run_daily_report)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
