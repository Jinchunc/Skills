#!/usr/bin/env python3
"""Run a metrics-first TiFlash health inspection through Clinic Data Proxy."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

try:
    from clinic_api import ClinicAPI
except ModuleNotFoundError:
    current_dir = Path(__file__).resolve().parent
    candidate_roots = [
        # agent-stack dba profile / build tree:
        # <root>/providers/tidb-cloud/ops (this file lives under <root>/engines/tidb/instance-inspection/modules)
        current_dir.parents[3] / "providers/tidb-cloud/ops",
        # upstream nutshell-skills layout: <skills>/platform/clinic-api
        current_dir.parents[1] / "platform/clinic-api",
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills/clinic-api",
        Path.home() / ".claude/skills/clinic-api",
        Path.home() / ".config/opencode/skills/clinic-api",
    ]
    for root in candidate_roots:
        scripts_dir = root / "scripts"
        if (scripts_dir / "clinic_api.py").exists():
            sys.path.insert(0, str(scripts_dir))
            break
    from clinic_api import ClinicAPI


DEFAULT_ORG_SEARCH_ID = "1372813089209061633"
MPP_TASK_MONITOR_STALE_SECONDS = 2 * 24 * 60 * 60
M3_EXCHANGE_QUEUE_WARNING_BYTES = 128 * 1024 * 1024
CORE_METRICS = [
    "tiflash_coprocessor_request_duration_seconds_bucket",
    "tiflash_coprocessor_request_count",
    "tiflash_proxy_process_cpu_seconds_total",
    "tiflash_system_current_metric_LogicalCPUCores",
    "tiflash_proxy_process_resident_memory_bytes",
    "tiflash_system_current_metric_MemoryCapacity",
]


def utc_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def normalize_deploy_type(item: dict[str, Any]) -> str:
    value = (
        item.get("clusterDeployTypeV2")
        or item.get("clusterDeployType")
        or item.get("deployType")
        or ""
    )
    return str(value).lower().replace("-", "_").replace(" ", "_")


def cluster_status(item: dict[str, Any]) -> str:
    return str(item.get("clusterStatus") or item.get("status") or "").lower()


def metadata_tiflash_count(item: dict[str, Any]) -> int | None:
    topology = item.get("topology")
    if not isinstance(topology, dict) or "tiflash" not in topology:
        return None
    try:
        return int(topology.get("tiflash") or 0)
    except (TypeError, ValueError):
        return None


def topology_component_count(row: dict[str, Any]) -> int:
    for key in ("node_count", "nodeCount", "replicas", "count"):
        if key in row:
            try:
                return int(row.get(key) or 0)
            except (TypeError, ValueError):
                return 0
    return 1


def prom_value(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def prom_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def regex_union(values: list[str]) -> str:
    if not values:
        return ".*"
    return "|".join(re.escape(v) for v in sorted(set(values)))


class ClinicClient(ClinicAPI):
    def __init__(self, env_file: Path):
        super().__init__(str(env_file))
        self.api_base = f"{self.endpoint}/clinic/api/v1"

    def search_clusters(self, query: str, show_deleted: bool, limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self._get(
                "/clinic/api/v1/dashboard/clusters",
                {
                    "query": query,
                    "show_deleted": str(show_deleted).lower(),
                    "limit": limit,
                    "page": page,
                },
            )
            if result.get("error"):
                raise RuntimeError(f"cluster search failed: {result['error']}")
            page_items = result.get("items") or result.get("data") or []
            if not isinstance(page_items, list):
                raise RuntimeError("cluster search returned an unexpected payload")
            items.extend(page_items)
            if len(page_items) < limit:
                break
            page += 1
            if page > 20:
                break
        return items

    def get_topology(self, org_id: str, cluster_id: str) -> tuple[list[dict[str, Any]], str | None]:
        result = self._get(
            f"/clinic/api/v1/orgs/{org_id}/clusters/{cluster_id}/resource-pool/components",
            {},
        )
        if isinstance(result, list):
            return result, None
        if result.get("error"):
            return [], result["error"]
        if isinstance(result.get("items"), list):
            return result["items"], None
        if isinstance(result.get("data"), list):
            return result["data"], None
        return [], "topology returned an unexpected payload"


def result_series(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result.get("data", {}).get("result", []) if isinstance(result, dict) else []


def series_labels(result: dict[str, Any]) -> list[dict[str, str]]:
    data = result.get("data", []) if isinstance(result, dict) else []
    return [item for item in data if isinstance(item, dict)]


def matrix_stats(result: dict[str, Any], step_seconds: int, threshold: float | None = None) -> dict[str, Any]:
    values: list[float] = []
    affected: set[str] = set()
    above = 0
    for series in result_series(result):
        metric = series.get("metric", {})
        label = (
            metric.get("instance")
            or metric.get("pod")
            or metric.get("type")
            or metric.get("reason")
            or metric.get("store_id")
            or "{}"
        )
        local_values = []
        for _, raw in series.get("values", []):
            value = prom_value(raw)
            if value is None:
                continue
            local_values.append(value)
            values.append(value)
            if threshold is not None and value > threshold:
                above += 1
        if threshold is not None and any(v > threshold for v in local_values):
            affected.add(str(label))
    if not values:
        return {"sample_count": 0, "affected": []}
    return {
        "sample_count": len(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "p95": statistics.quantiles(values, n=20)[18] if len(values) >= 20 else max(values),
        "duration_above_threshold": above * step_seconds,
        "affected": sorted(affected),
    }


def metric_display_label(metric: dict[str, Any]) -> str:
    for key in ("instance", "pod", "type", "reason", "store_id", "name"):
        value = metric.get(key)
        if value:
            return str(value)
    if metric:
        return json.dumps(metric, sort_keys=True)
    return "{}"


def storage_pod_label(metric: dict[str, Any]) -> str:
    pod = str(metric.get("pod") or "")
    node = str(metric.get("instance") or "")
    volume = str(metric.get("volume_id") or metric.get("volumename") or metric.get("persistentvolume") or "")
    details = []
    if node:
        details.append(f"node={node}")
    if volume:
        details.append(f"volume={volume}")
    if pod:
        return f"{pod} ({', '.join(details)})" if details else pod
    if details:
        return ", ".join(details)
    return "{}"


def sustained_series_stats(
    result: dict[str, Any],
    step_seconds: int,
    threshold: float,
    label_func=metric_display_label,
) -> dict[str, Any]:
    values: list[float] = []
    affected: set[str] = set()
    total_above = 0
    max_series_above = 0
    max_series_consecutive = 0
    for series in result_series(result):
        metric = series.get("metric", {})
        label = label_func(metric)
        local_above = 0
        local_consecutive = 0
        local_max_consecutive = 0
        for _, raw in series.get("values", []):
            value = prom_value(raw)
            if value is None:
                continue
            values.append(value)
            if value > threshold:
                local_above += step_seconds
                local_consecutive += step_seconds
                local_max_consecutive = max(local_max_consecutive, local_consecutive)
            else:
                local_consecutive = 0
        if local_above > 0:
            affected.add(label)
        total_above += local_above
        max_series_above = max(max_series_above, local_above)
        max_series_consecutive = max(max_series_consecutive, local_max_consecutive)
    if not values:
        return {"sample_count": 0, "affected": []}
    return {
        "sample_count": len(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "p95": statistics.quantiles(values, n=20)[18] if len(values) >= 20 else max(values),
        "duration_above_threshold": max_series_consecutive,
        "total_duration_above_threshold": total_above,
        "max_series_duration_above_threshold": max_series_above,
        "affected": sorted(affected),
    }


def stale_filtered_sustained_series_stats(
    result: dict[str, Any],
    step_seconds: int,
    threshold: float,
    max_value: float,
    label_func=metric_display_label,
) -> dict[str, Any]:
    values: list[float] = []
    affected: set[str] = set()
    ignored_samples = 0
    total_above = 0
    max_series_above = 0
    max_series_consecutive = 0
    for series in result_series(result):
        metric = series.get("metric", {})
        label = label_func(metric)
        local_above = 0
        local_consecutive = 0
        local_max_consecutive = 0
        for _, raw in series.get("values", []):
            value = prom_value(raw)
            if value is None:
                continue
            if value > max_value:
                ignored_samples += 1
                local_consecutive = 0
                continue
            values.append(value)
            if value > threshold:
                local_above += step_seconds
                local_consecutive += step_seconds
                local_max_consecutive = max(local_max_consecutive, local_consecutive)
            else:
                local_consecutive = 0
        if local_above > 0:
            affected.add(label)
        total_above += local_above
        max_series_above = max(max_series_above, local_above)
        max_series_consecutive = max(max_series_consecutive, local_max_consecutive)
    if not values:
        return {"sample_count": 0, "affected": [], "ignored_sample_count": ignored_samples}
    return {
        "sample_count": len(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "p95": statistics.quantiles(values, n=20)[18] if len(values) >= 20 else max(values),
        "duration_above_threshold": max_series_consecutive,
        "total_duration_above_threshold": total_above,
        "max_series_duration_above_threshold": max_series_above,
        "affected": sorted(affected),
        "ignored_sample_count": ignored_samples,
    }


def max_by_series(result: dict[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for series in result_series(result):
        metric = series.get("metric", {})
        label = (
            metric.get("instance")
            or metric.get("pod")
            or metric.get("type")
            or metric.get("reason")
            or json.dumps(metric, sort_keys=True)
        )
        vals = [prom_value(v[1]) for v in series.get("values", [])]
        vals = [v for v in vals if v is not None]
        if vals:
            output[str(label)] = max(vals)
    return output


def metric_name_from_query(query: str) -> str:
    match = re.search(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{", query)
    return match.group(1) if match else ""


def unknown_reason_for_query(
    client: ClinicClient,
    cluster_id: str,
    query: str,
    start: int,
    end: int,
    series_lookup: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[str, str]:
    if 'instance=~".*"' in query:
        return "selector unresolved", "metric selector could not be narrowed to TiFlash instances"
    metric = metric_name_from_query(query)
    if not metric:
        return "metric samples", "query returned no usable samples"
    series = series_lookup(metric) if series_lookup else client.query_series(cluster_id, metric, start, end)
    if series.get("error"):
        return "series discovery failed", series["error"]
    labels = series_labels(series)
    if not labels:
        return "metric missing", f"{metric} has no series in the inspected window"
    return "metric samples", f"{metric} series exist, but the rule query returned no usable samples; check selector labels or no-event histogram behavior"


def status_from_threshold(stats: dict[str, Any], threshold: float, duration_seconds: int, severity: str = "Warning") -> str:
    if not stats.get("sample_count"):
        return "Unknown"
    if stats.get("duration_above_threshold", 0) >= duration_seconds:
        return severity
    return "Healthy"


def build_selector(labels: list[dict[str, str]], cluster_id: str) -> tuple[str, str, str]:
    if not labels:
        return 'instance=~".*"', "", ".*"
    first = labels[0]
    parts: list[str] = []
    for key in ("k8s_cluster", "tidb_cluster", "tidb_cluster_id", "cluster_id"):
        value = first.get(key)
        if value:
            parts.append(f'{key}="{prom_label_value(value)}"')
    instances = [item.get("instance", "") for item in labels if item.get("instance")]
    instance_regex = regex_union(instances)
    parts.append(f'instance=~"{prom_label_value(instance_regex)}"')
    namespace = first.get("namespace") or f"tidb{cluster_id}"
    return ", ".join(parts), namespace, instance_regex


def selector_for_metric(
    client: ClinicClient,
    cluster_id: str,
    metric: str,
    start: int,
    end: int,
    fallback: str,
    series_lookup: Callable[[str], dict[str, Any]] | None = None,
) -> str:
    discovery = series_lookup(metric) if series_lookup else client.query_series(cluster_id, metric, start, end)
    labels = series_labels(discovery)
    if not labels:
        return fallback
    return build_selector(labels, cluster_id)[0]


def skipped_cluster_result(item: dict[str, Any], reason: str, missing: str, attempt: str) -> dict[str, Any]:
    cluster_id = str(item.get("clusterID") or "")
    deploy_type = normalize_deploy_type(item)
    status = cluster_status(item)
    selector, namespace, _ = build_selector([], cluster_id)
    return {
        "cluster_id": cluster_id,
        "cluster_name": item.get("clusterName") or item.get("name") or cluster_id,
        "status": "Skipped",
        "coverage": "Missing",
        "tier_run": "Discovery",
        "top_finding": reason,
        "likely_bottleneck": "Unknown",
        "metadata": metadata_fields(item, deploy_type, status),
        "tiflash_instances": [],
        "selector": selector,
        "namespace": namespace,
        "rules": [],
        "unknowns": [{"scope": cluster_id, "missing": missing, "attempt": attempt, "effect": "cluster skipped"}],
        "queries": [],
    }


def inspect_cluster(client: ClinicClient, item: dict[str, Any], start: int, end: int, trend_start: int, step: str) -> dict[str, Any]:
    step_seconds = int(re.sub(r"\D", "", step) or "60")
    cluster_id = str(item.get("clusterID") or "")
    org_id = str(item.get("orgID") or "")
    deploy_type = normalize_deploy_type(item)
    status = cluster_status(item)

    topology: list[dict[str, Any]] = []
    topology_error = None
    if org_id and deploy_type not in {"shared", "starter", "essential"}:
        topology, topology_error = client.get_topology(org_id, cluster_id)
    tiflash_components = [
        row for row in topology
        if str(row.get("component_type") or row.get("componentType") or "").lower() == "tiflash"
    ]
    active_tiflash_components = [row for row in tiflash_components if topology_component_count(row) > 0]
    metadata_tiflash = metadata_tiflash_count(item)
    topology_says_no_tiflash = metadata_tiflash == 0 or (bool(tiflash_components) and not active_tiflash_components)
    if topology_says_no_tiflash:
        return skipped_cluster_result(
            item,
            "Metadata/topology shows TiFlash node count is 0",
            "TiFlash topology",
            "metadata/topology precheck",
        )

    series_cache: dict[str, dict[str, Any]] = {}

    def cached_query_series(metric: str) -> dict[str, Any]:
        if metric not in series_cache:
            series_cache[metric] = client.query_series(cluster_id, metric, start, end)
        return series_cache[metric]

    discovery = cached_query_series("tiflash_proxy_process_cpu_seconds_total")
    if not series_labels(discovery):
        discovery = cached_query_series("tiflash_coprocessor_request_duration_seconds_bucket")
    labels = series_labels(discovery)
    selector, namespace, instance_regex = build_selector(labels, cluster_id)

    coverage: dict[str, str] = {}
    for metric in CORE_METRICS:
        found = cached_query_series(metric)
        coverage[metric] = "Covered" if series_labels(found) else "Missing"

    has_tiflash_metrics = any(v == "Covered" for v in coverage.values())
    has_tiflash_topology = bool(active_tiflash_components)
    if not has_tiflash_metrics and not has_tiflash_topology:
        return skipped_cluster_result(
            item,
            "No TiFlash topology or TiFlash metric series found",
            "TiFlash metrics/topology",
            "metadata topology and core series discovery",
        )

    queries: list[tuple[str, str]] = []
    rules: list[dict[str, Any]] = []
    unknowns: list[dict[str, str]] = []

    def run_rule(rule_id: str, title: str, query: str, threshold: float | None, duration: int, severity: str = "Warning") -> dict[str, Any]:
        queries.append((rule_id, query))
        result = client.query_range(cluster_id, query, start, end, step)
        if result.get("error"):
            rule = {
                "rule": f"{rule_id} {title}",
                "status": "Unknown",
                "observed": result["error"],
                "threshold": threshold if threshold is not None else "data present",
                "evidence": rule_id,
            }
            unknowns.append({"scope": rule_id, "missing": "query failed", "attempt": query, "effect": result["error"]})
            rules.append(rule)
            return rule
        stats = matrix_stats(result, step_seconds) if threshold is None else sustained_series_stats(result, step_seconds, threshold)
        if threshold is None:
            rule_status = "Unknown"
        else:
            rule_status = status_from_threshold(stats, threshold, duration, severity)
        if rule_status == "Unknown":
            if threshold is None and stats.get("sample_count"):
                missing = "rule logic not implemented"
                effect = "baseline, drain, or correlation logic is required before this rule can be judged"
            else:
                missing, effect = unknown_reason_for_query(client, cluster_id, query, start, end, cached_query_series)
            unknowns.append({"scope": rule_id, "missing": missing, "attempt": query, "effect": effect})
        observed = format_observed(stats)
        rule = {
            "rule": f"{rule_id} {title}",
            "status": rule_status,
            "observed": observed,
            "threshold": threshold_text(threshold, duration, severity),
            "evidence": rule_id,
            "stats": stats,
        }
        rules.append(rule)
        return rule

    def mark_histogram_no_activity_healthy(
        rule: dict[str, Any],
        rule_id: str,
        metric: str,
        metric_type: str | None = None,
        group_by: str | None = None,
    ) -> None:
        if rule.get("status") != "Unknown":
            return
        type_selector = f', type="{metric_type}"' if metric_type else ""
        resolved_group_by = group_by or ("le, type" if metric_type else "le, type, instance")
        label = metric_type or metric
        rate_query = f"sum(rate({metric}{{{selector}{type_selector}}}[1m])) by ({resolved_group_by})"
        result = client.query_range(cluster_id, rate_query, start, end, step)
        stats = matrix_stats(result, step_seconds)
        if stats.get("sample_count") and stats.get("max", 0) <= 0:
            rule["status"] = "Healthy"
            rule["observed"] = f"no observed {label} events; bucket rate max=0, samples={stats['sample_count']}"
            unknowns[:] = [unknown for unknown in unknowns if unknown.get("scope") != rule_id]

    def throughput_near_zero(rule_id: str, query: str) -> tuple[bool, dict[str, Any]]:
        queries.append((rule_id, query))
        result = client.query_range(cluster_id, query, start, end, step)
        if result.get("error"):
            unknowns.append({"scope": rule_id, "missing": "query failed", "attempt": query, "effect": result["error"]})
            return False, {"sample_count": 0}
        stats = matrix_stats(result, step_seconds)
        return bool(stats.get("sample_count")) and stats.get("max", 0) <= 0.01, stats

    def run_mpp_task_monitor_rule() -> dict[str, Any]:
        rule_id = "M1"
        query = f"tiflash_mpp_task_monitor{{{selector}}}"
        queries.append((rule_id, query))
        result = client.query_range(cluster_id, query, start, end, step)
        if result.get("error"):
            rule = {
                "rule": "M1 longest live MPP task is suspicious",
                "status": "Unknown",
                "observed": result["error"],
                "threshold": "query failed",
                "evidence": rule_id,
            }
            unknowns.append({"scope": rule_id, "missing": "query failed", "attempt": query, "effect": result["error"]})
            rules.append(rule)
            return rule

        stats = stale_filtered_sustained_series_stats(
            result,
            step_seconds,
            threshold=600,
            max_value=MPP_TASK_MONITOR_STALE_SECONDS,
        )
        ignored = stats.get("ignored_sample_count", 0)
        if stats.get("sample_count"):
            rule_status = status_from_threshold(stats, 600, 300)
            observed = format_observed(stats)
            if ignored:
                observed += f", ignored_stale_samples={ignored}"
        elif ignored:
            rule_status = "Healthy"
            observed = f"no non-stale samples; ignored_stale_samples={ignored}"
        else:
            rule_status = "Unknown"
            observed = "no samples"
            missing, effect = unknown_reason_for_query(client, cluster_id, query, start, end, cached_query_series)
            unknowns.append({"scope": rule_id, "missing": missing, "attempt": query, "effect": effect})

        rule = {
            "rule": "M1 longest live MPP task is suspicious",
            "status": rule_status,
            "observed": observed,
            "threshold": f"Warning when > 600 for >= 300s; ignore samples > {MPP_TASK_MONITOR_STALE_SECONDS}s",
            "evidence": rule_id,
            "stats": stats,
        }
        rules.append(rule)
        return rule

    def aggregate_values_by_timestamp(result: dict[str, Any]) -> list[tuple[int, float]]:
        totals: dict[int, float] = {}
        for series in result_series(result):
            for raw_ts, raw in series.get("values", []):
                value = prom_value(raw)
                if value is None:
                    continue
                ts = int(float(raw_ts))
                totals[ts] = totals.get(ts, 0.0) + value
        return sorted(totals.items())

    def run_drain_rule(rule_id: str, title: str, gauge_query: str) -> dict[str, Any]:
        queries.append((rule_id, gauge_query))
        gauge_result = client.query_range(cluster_id, gauge_query, start, end, step)
        if gauge_result.get("error"):
            rule = {
                "rule": f"{rule_id} {title}",
                "status": "Unknown",
                "observed": gauge_result["error"],
                "threshold": "query failed",
                "evidence": rule_id,
            }
            unknowns.append({"scope": rule_id, "missing": "query failed", "attempt": gauge_query, "effect": gauge_result["error"]})
            rules.append(rule)
            return rule

        qps_query = f"sum(rate(tiflash_coprocessor_request_count{{{selector}}}[1m])) by (type)"
        queries.append((f"{rule_id}-request-qps", qps_query))
        qps_result = client.query_range(cluster_id, qps_query, start, end, step)
        if qps_result.get("error"):
            rule = {
                "rule": f"{rule_id} {title}",
                "status": "Unknown",
                "observed": qps_result["error"],
                "threshold": "request QPS query failed",
                "evidence": rule_id,
            }
            unknowns.append({"scope": rule_id, "missing": "query failed", "attempt": qps_query, "effect": qps_result["error"]})
            rules.append(rule)
            return rule

        qps_values = aggregate_values_by_timestamp(qps_result)
        qps_max = max((value for _, value in qps_values), default=0.0)
        qps_last = qps_values[-1][1] if qps_values else 0.0
        qps_dropped = qps_max > 0 and qps_last <= qps_max * 0.7

        values: list[float] = []
        affected: set[str] = set()
        max_duration = 0
        for series in result_series(gauge_result):
            metric = series.get("metric", {})
            label = metric_display_label(metric)
            local_values = []
            for _, raw in series.get("values", []):
                value = prom_value(raw)
                if value is None:
                    continue
                values.append(value)
                local_values.append(value)
            if not local_values:
                continue
            local_max = max(local_values)
            if local_max <= 0:
                continue
            threshold = local_max * 0.8
            consecutive = 0
            local_max_duration = 0
            for value in local_values:
                if value > 0 and value >= threshold:
                    consecutive += step_seconds
                    local_max_duration = max(local_max_duration, consecutive)
                else:
                    consecutive = 0
            if local_max_duration > 0:
                affected.add(label)
            max_duration = max(max_duration, local_max_duration)

        if not values:
            missing, effect = unknown_reason_for_query(client, cluster_id, gauge_query, start, end, cached_query_series)
            rule_status = "Unknown"
            stats = {"sample_count": 0, "affected": []}
            unknowns.append({"scope": rule_id, "missing": missing, "attempt": gauge_query, "effect": effect})
        else:
            stats = {
                "sample_count": len(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "duration_above_threshold": max_duration if qps_dropped else 0,
                "affected": sorted(affected),
            }
            rule_status = "Warning" if qps_dropped and max_duration >= 300 else "Healthy"

        observed = format_observed(stats)
        observed += f", qps_max={qps_max:.4g}, qps_last={qps_last:.4g}, qps_dropped={str(qps_dropped).lower()}"
        rule = {
            "rule": f"{rule_id} {title}",
            "status": rule_status,
            "observed": observed,
            "threshold": "Warning when request QPS drops >=30% and count stays >=80% of window max for >=300s",
            "evidence": rule_id,
            "stats": stats,
        }
        rules.append(rule)
        return rule

    core_missing = [metric for metric, state in coverage.items() if state == "Missing"]
    if core_missing:
        unknowns.append({
            "scope": "D1",
            "missing": ", ".join(core_missing),
            "attempt": "query_series over incident window",
            "effect": "minimal core coverage incomplete",
        })

    q1 = run_rule("Q1", "TiFlash request error QPS is nonzero", f"sum(rate(tiflash_coprocessor_request_error{{{selector}}}[1m])) by (reason)", 0, 300)
    weak_q1_reasons = {"epoch_not_match", "region_not_found", "meet_lock"}
    q1_affected = set(q1.get("stats", {}).get("affected", []))
    if q1["status"] == "Warning" and q1_affected and q1_affected <= weak_q1_reasons:
        q1["status"] = "Healthy"
        q1["weak_signal"] = True
        q1["threshold"] = "weak retry/data-movement reasons only; supporting evidence"
    q2 = run_rule("Q2", "request duration p99 is elevated", f"histogram_quantile(0.99, sum(rate(tiflash_coprocessor_request_duration_seconds_bucket{{{selector}}}[1m])) by (le, type))", 10, 300)
    mark_histogram_no_activity_healthy(q2, "Q2", "tiflash_coprocessor_request_duration_seconds_bucket", group_by="le, type")
    q3 = run_rule("Q3", "MPP establish connection is slow", f"histogram_quantile(0.99, sum(rate(tiflash_coprocessor_request_duration_seconds_bucket{{{selector}, type=\"mpp_establish_conn\"}}[1m])) by (le, type))", 3, 300)
    mark_histogram_no_activity_healthy(q3, "Q3", "tiflash_coprocessor_request_duration_seconds_bucket", "mpp_establish_conn")
    run_drain_rule("Q4", "active handling requests remain", f"sum(tiflash_coprocessor_handling_request_count{{{selector}}}) by (type)")
    run_mpp_task_monitor_rule()
    run_drain_rule("M2", "MPP query count stays high", f"max(tiflash_mpp_task_manager{{{selector}}}) by (instance, type)")
    m3 = run_rule("M3", "exchange queueing data grows or remains large", f"tiflash_exchange_queueing_data_bytes{{{selector}}}", M3_EXCHANGE_QUEUE_WARNING_BYTES, 300)
    if m3.get("status") == "Warning":
        exchange_near_zero, exchange_stats = throughput_near_zero("M3-exchange-throughput", f"sum(rate(tiflash_exchange_data_bytes{{{selector}}}[1m])) by (type)")
        response_near_zero, response_stats = throughput_near_zero("M3-response-throughput", f"sum(rate(tiflash_coprocessor_response_bytes{{{selector}}}[1m])) by (type)")
        if exchange_near_zero or response_near_zero:
            m3["status"] = "Critical"
            m3["threshold"] = "Critical when queueing data is large and exchange/response throughput is near zero"
            m3["observed"] = (
                f"{m3['observed']}; exchange_throughput={format_observed(exchange_stats)}; "
                f"response_throughput={format_observed(response_stats)}"
            )
    process_selector = selector_for_metric(client, cluster_id, "tiflash_proxy_process_cpu_seconds_total", start, end, selector, cached_query_series)
    cpu_capacity_selector = selector_for_metric(client, cluster_id, "tiflash_system_current_metric_LogicalCPUCores", start, end, selector, cached_query_series)
    memory_selector = selector_for_metric(client, cluster_id, "tiflash_proxy_process_resident_memory_bytes", start, end, selector, cached_query_series)
    memory_capacity_selector = selector_for_metric(client, cluster_id, "tiflash_system_current_metric_MemoryCapacity", start, end, selector, cached_query_series)
    store_used_selector = selector_for_metric(client, cluster_id, "tiflash_system_current_metric_StoreSizeUsed", start, end, selector, cached_query_series)
    store_capacity_selector = selector_for_metric(client, cluster_id, "tiflash_system_current_metric_StoreSizeCapacity", start, end, selector, cached_query_series)

    r1 = run_rule("R1", "CPU usage ratio is high", f"sum by (instance) (rate(tiflash_proxy_process_cpu_seconds_total{{{process_selector}}}[1m])) / on(instance) sum by (instance) (tiflash_system_current_metric_LogicalCPUCores{{{cpu_capacity_selector}}})", 0.85, 180)
    run_rule("R2", "memory usage ratio is high", f"max by (instance) (tiflash_proxy_process_resident_memory_bytes{{{memory_selector}}}) / on(instance) sum by (instance) (tiflash_system_current_metric_MemoryCapacity{{{memory_capacity_selector}}})", 0.80, 180)
    run_rule("R3", "store capacity usage is high", f"sum(tiflash_system_current_metric_StoreSizeUsed{{{store_used_selector}}}) by (instance) / on(instance) sum(tiflash_system_current_metric_StoreSizeCapacity{{{store_capacity_selector}}}) by (instance)", 0.90, 300)

    s2 = run_rule("S2", "fsync duration is elevated", f"histogram_quantile(1.00, sum(round(1000000000 * rate(tiflash_system_seconds_bucket{{{selector}, type=~\"fsync\"}}[1m]))) by (le, instance) / 1000000000)", 1, 300)
    mark_histogram_no_activity_healthy(s2, "S2", "tiflash_system_seconds_bucket", "fsync", "le, instance")
    s3 = run_rule("S3", "write stall duration is high", f"histogram_quantile(1.00, sum(round(1000000000 * rate(tiflash_storage_write_stall_duration_seconds_bucket{{{selector}}}[1m]))) by (le, type, instance) / 1000000000)", 10, 300)
    mark_histogram_no_activity_healthy(s3, "S3", "tiflash_storage_write_stall_duration_seconds_bucket")
    s4_rules = []
    for metric in [
        "tiflash_system_current_metric_IOLimiterPendingFgWriteReq",
        "tiflash_system_current_metric_IOLimiterPendingFgReadReq",
    ]:
        s4_rules.append(run_rule("S4", f"{metric} persists", f"{metric}{{{selector}}}", 0, 300))
    run_rule("N2", "TiFlash Proxy reports server failures", f"sum(rate(tiflash_proxy_tikv_server_report_failure_msg_total{{{selector}}}[1m])) by (type, instance, store_id)", 0, 180)
    a1 = run_rule("A1", "Raft wait index duration is high", f"histogram_quantile(0.99, sum(rate(tiflash_raft_wait_index_duration_seconds_bucket{{{selector}}}[1m])) by (le))", 30, 300)
    mark_histogram_no_activity_healthy(a1, "A1", "tiflash_raft_wait_index_duration_seconds_bucket", group_by="le")
    run_rule("A2", "Raft wait index timeout occurs", f"sum(increase(tiflash_system_profile_event_RaftWaitIndexTimeout{{{selector}}}[1m])) by (instance)", 0, 60, "Critical")
    p1 = run_rule("P1", "Pipeline CPU queue duration is high", f"histogram_quantile(0.99, sum(rate(tiflash_pipeline_task_duration_seconds_bucket{{{selector}, type=\"cpu_queue\"}}[1m])) by (le, type))", 1, 300)
    mark_histogram_no_activity_healthy(p1, "P1", "tiflash_pipeline_task_duration_seconds_bucket", "cpu_queue")
    p2 = run_rule("P2", "Pipeline IO queue duration is high", f"histogram_quantile(0.99, sum(rate(tiflash_pipeline_task_duration_seconds_bucket{{{selector}, type=\"io_queue\"}}[1m])) by (le, type))", 1, 300)
    mark_histogram_no_activity_healthy(p2, "P2", "tiflash_pipeline_task_duration_seconds_bucket", "io_queue")
    p3 = run_rule("P3", "Pipeline await duration is high", f"histogram_quantile(0.99, sum(rate(tiflash_pipeline_task_duration_seconds_bucket{{{selector}, type=\"await\"}}[1m])) by (le, type))", 1, 300)
    mark_histogram_no_activity_healthy(p3, "P3", "tiflash_pipeline_task_duration_seconds_bucket", "await")

    def abnormal(rule: dict[str, Any]) -> bool:
        return rule.get("status") in {"Warning", "Critical"}

    def downgrade_supporting(rule: dict[str, Any], reason: str) -> None:
        if rule.get("status") != "Warning":
            return
        rule["status"] = "Healthy"
        rule["weak_signal"] = True
        rule["threshold"] = reason

    downgrade_supporting(
        s2,
        "fsync exceeded fallback threshold, but Q2/S3/P2 did not corroborate it; supporting evidence",
    ) if not any(abnormal(rule) for rule in (q2, s3, p2)) else None
    if s3.get("status") == "Warning" and any(abnormal(rule) for rule in (s2, q2)):
        s3["status"] = "Critical"
        s3["threshold"] = "Critical when write stall coincides with S2 or Q2"
    downgrade_supporting(
        p1,
        "cpu_queue exceeded 1s, but CPU ratio did not corroborate it; supporting evidence",
    ) if not abnormal(r1) else None
    downgrade_supporting(
        p2,
        "io_queue exceeded 1s, but storage signals did not corroborate it; supporting evidence",
    ) if not any(abnormal(rule) for rule in [s2, s3, *s4_rules]) else None
    downgrade_supporting(
        p3,
        "await exceeded 1s, but exchange backlog did not corroborate it; supporting evidence",
    ) if not abnormal(m3) else None

    warning_or_critical = [r for r in rules if r["status"] in {"Warning", "Critical"}]
    if any(r["status"] == "Critical" for r in rules):
        overall = "Critical"
    elif warning_or_critical:
        overall = "Warning"
    elif core_missing:
        overall = "Unknown"
    else:
        overall = "Healthy"

    likely = likely_bottleneck(rules)
    top_finding = warning_or_critical[0]["rule"] if warning_or_critical else ("Minimal core coverage incomplete" if core_missing else "No warning or critical rule fired")
    return {
        "cluster_id": cluster_id,
        "cluster_name": item.get("clusterName") or item.get("name") or cluster_id,
        "status": overall,
        "coverage": "Partial" if core_missing or unknowns else "Covered",
        "tier_run": "Tier1+Tier2",
        "top_finding": top_finding,
        "likely_bottleneck": likely,
        "metadata": metadata_fields(item, deploy_type, status),
        "tiflash_instances": sorted({row.get("instance") for row in labels if row.get("instance")}),
        "selector": selector,
        "namespace": namespace,
        "rules": rules,
        "unknowns": unknowns,
        "queries": queries,
    }


def metadata_fields(item: dict[str, Any], deploy_type: str, status: str) -> dict[str, str]:
    return {
        "Provider / Region": f"{item.get('clusterProviderName') or item.get('provider') or ''}/{item.get('clusterRegionName') or item.get('region') or ''}",
        "Deploy Type": deploy_type,
        "Cluster Status": status,
        "Tenant ID": str(item.get("tenantID") or ""),
        "Org ID": str(item.get("orgID") or ""),
        "Parent ID": str(item.get("parentID") or ""),
    }


def format_observed(stats: dict[str, Any]) -> str:
    if not stats.get("sample_count"):
        return "no samples"
    max_value = stats.get("max", 0)
    avg_value = stats.get("avg", 0)
    duration = stats.get("duration_above_threshold", 0)
    total_duration = stats.get("total_duration_above_threshold")
    affected = ",".join(stats.get("affected", [])[:5]) or "-"
    total_text = ""
    if total_duration is not None and total_duration != duration:
        total_text = f", total_above={total_duration}s"
    return f"max={max_value:.4g}, avg={avg_value:.4g}, above={duration}s{total_text}, affected={affected}, samples={stats.get('sample_count')}"


def threshold_text(threshold: float | None, duration: int, severity: str) -> str:
    if threshold is None:
        return "samples present"
    return f"{severity} when > {threshold:g} for >= {duration}s"


def likely_bottleneck(rules: list[dict[str, Any]]) -> str:
    abnormal = {r["rule"].split()[0] for r in rules if r["status"] in {"Warning", "Critical"}}
    compute = bool({"R1", "P1"} & abnormal)
    storage = bool({"S2", "S3", "S4", "P2"} & abnormal)
    network = bool({"N1", "N2", "N3", "Q3", "P3"} & abnormal)
    count = sum(1 for flag in (compute, storage, network) if flag)
    if count >= 2:
        return "Mixed"
    if compute:
        return "Compute"
    if storage:
        return "Storage"
    if network:
        return "Network/Exchange"
    return "Unknown"


def render_report(
    results: list[dict[str, Any]],
    start: int,
    end: int,
    scope: str,
    data_source: str,
    report_format: str = "compact",
    include_queries: bool = False,
) -> str:
    if report_format == "full":
        return render_full_report(results, start, end, scope, data_source, include_queries)
    return render_compact_report(results, start, end, scope, data_source, include_queries)


def overall_status(results: list[dict[str, Any]]) -> str:
    statuses = [r["status"] for r in results if r["status"] != "Skipped"]
    if any(s == "Critical" for s in statuses):
        return "Critical"
    if any(s == "Warning" for s in statuses):
        return "Warning"
    if any(s == "Unknown" for s in statuses):
        return "Unknown"
    if statuses:
        return "Healthy"
    if results and all(r.get("status") == "Skipped" for r in results):
        return "Skipped"
    return "Unknown"


def actionable_rules(results: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (cluster, rule)
        for cluster in results
        for rule in cluster.get("rules", [])
        if rule.get("status") in {"Critical", "Warning"}
    ]


def domain_status(rules: list[dict[str, Any]], prefixes: tuple[str, ...]) -> str:
    selected = [rule for rule in rules if rule["rule"].split()[0].startswith(prefixes)]
    if not selected:
        return "Not checked"
    statuses = [rule.get("status") for rule in selected]
    if "Critical" in statuses:
        return "Critical"
    if "Warning" in statuses:
        return "Warning"
    if "Unknown" in statuses:
        return "Unknown"
    return "Healthy"


def compact_evidence(rule: dict[str, Any]) -> str:
    observed = str(rule.get("observed", ""))
    if len(observed) <= 120:
        return observed
    return observed[:117] + "..."


def rule_code(rule: dict[str, Any]) -> str:
    return str(rule.get("rule", "")).split()[0]


def clusters_with_rule(findings: list[tuple[dict[str, Any], dict[str, Any]]], codes: set[str]) -> list[str]:
    names = sorted({
        str(cluster.get("cluster_name") or cluster.get("cluster_id"))
        for cluster, rule in findings
        if rule_code(rule) in codes
    })
    return names


def short_list(values: list[str], limit: int = 5) -> str:
    if len(values) <= limit:
        return ", ".join(values)
    return ", ".join(values[:limit]) + f", ... (+{len(values) - limit})"


def recommended_actions(results: list[dict[str, Any]]) -> list[str]:
    overall = overall_status(results)
    findings = actionable_rules(results)
    unknown_rows = [(r, u) for r in results if r.get("status") != "Skipped" for u in r.get("unknowns", [])]

    if overall == "Skipped":
        return [
            "No TiFlash nodes were detected in this scope; do not run TiFlash-specific remediation.",
            "If TiFlash is expected, verify topology, scaling state, and metrics export before investigating workload symptoms.",
        ]

    if not findings:
        actions = ["No immediate TiFlash remediation is required from covered metrics."]
        if unknown_rows:
            actions.append("Resolve the Unknown metric coverage first, then rerun the affected rule before making a final health judgment.")
        return actions

    actions: list[str] = []
    finding_codes = {rule_code(rule) for _, rule in findings}

    if "M3" in finding_codes:
        clusters = short_list(clusters_with_rule(findings, {"M3"}))
        actions.append(
            f"For M3 clusters ({clusters}), treat the issue as MPP exchange backlog. Pull TiDB slow queries/execution summaries for the same window and identify TiFlash MPP SQL digests with large shuffle/exchange before changing infrastructure."
        )

    if "M1" in finding_codes:
        clusters = short_list(clusters_with_rule(findings, {"M1"}))
        actions.append(
            f"For M1 clusters ({clusters}), inspect long-running MPP tasks and their SQL digests; cancel or throttle stuck analytical queries if they are still active."
        )

    if "P3" in finding_codes:
        clusters = short_list(clusters_with_rule(findings, {"P3"}))
        actions.append(
            f"For P3 clusters ({clusters}), investigate pipeline await latency as scheduling/backpressure. Because P1/P2 are not the primary findings, start with query concurrency and upstream wait rather than CPU or disk tuning."
        )

    resource_codes = {"R1", "R2", "R3"} & finding_codes
    if resource_codes:
        clusters = short_list(clusters_with_rule(findings, resource_codes))
        actions.append(
            f"For resource warnings ({clusters}), compare affected TiFlash instances with peers and scale CPU/memory/storage only after confirming sustained imbalance in the inspected window."
        )

    if unknown_rows:
        scopes = short_list([f"{r.get('cluster_name')}/{u.get('scope')}" for r, u in unknown_rows], 4)
        actions.append(
            f"Resolve remaining Unknown coverage ({scopes}); do not use those specific rules as negative evidence until the metric selector or no-event histogram behavior is verified."
        )

    actions.append("Rerun with `--include-queries` only for reproduction or handoff; the priority is SQL attribution for the named clusters and affected TiFlash instances.")
    return actions[:7]


def render_compact_report(
    results: list[dict[str, Any]],
    start: int,
    end: int,
    scope: str,
    data_source: str,
    include_queries: bool,
) -> str:
    overall = overall_status(results)
    findings = actionable_rules(results)
    visible_results = [r for r in results if r.get("status") != "Skipped"]

    lines: list[str] = [
        "# TiFlash Health Inspection Report",
        "",
        "## Health Summary",
        f"Status: {overall}",
        f"Scope: {scope}",
        f"Time Range: {utc_ts(start)} ~ {utc_ts(end)}",
        f"Data Source: {data_source}",
        f"Clusters Checked: {len(results)}",
        f"Conclusion: {summary_sentence(overall, results)}",
        "",
        "## Actionable Findings",
        "| Severity | Rule | Evidence | Impact |",
        "|---|---|---|---|",
    ]
    if overall == "Skipped":
        lines.append("| Skipped | No TiFlash detected | All clusters in scope were classified as no TiFlash | TiFlash inspection is not applicable |")
    elif not findings:
        lines.append("| Healthy | None | No warning or critical rule fired | No immediate TiFlash risk found from covered metrics |")
    else:
        for cluster, rule in findings[:10]:
            lines.append(f"| {rule['status']} | {md_escape(cluster['cluster_name'])}: {md_escape(rule['rule'])} | {md_escape(compact_evidence(rule))} | Review affected TiFlash workload or resource domain |")

    lines.extend([
        "",
        "## Domain Summary",
        "| Cluster | Query | MPP | Resource | Storage IO | Network/Raft | Pipeline |",
        "|---|---|---|---|---|---|---|",
    ])
    if not visible_results:
        lines.append("| None | Not applicable | Not applicable | Not applicable | Not applicable | Not applicable | Not applicable |")
    for r in visible_results:
        rules = r.get("rules", [])
        network_raft = domain_status(rules, ("N", "A"))
        lines.append(
            f"| {md_escape(r['cluster_name'])} ({md_escape(r['cluster_id'])}) | "
            f"{domain_status(rules, ('Q',))} | {domain_status(rules, ('M',))} | "
            f"{domain_status(rules, ('R',))} | {domain_status(rules, ('S',))} | "
            f"{network_raft} | {domain_status(rules, ('P',))} |"
        )

    unknown_rows = [(r, u) for r in visible_results for u in r.get("unknowns", [])]
    skipped_results = [r for r in results if r.get("status") == "Skipped"]
    lines.extend(["", "## Unknowns"])
    if not unknown_rows:
        lines.append("None. All required discovered metrics were evaluated.")
    else:
        lines.extend(["| Scope | Missing Data | Effect |", "|---|---|---|"])
        for cluster, unknown in unknown_rows[:20]:
            lines.append(f"| {md_escape(cluster['cluster_name'])}/{md_escape(unknown['scope'])} | {md_escape(unknown['missing'])} | {md_escape(unknown['effect'])} |")

    lines.extend(["", "## Skipped Clusters"])
    if not skipped_results:
        lines.append("None.")
    else:
        lines.extend(["| Cluster | Reason |", "|---|---|"])
        for r in skipped_results[:20]:
            lines.append(f"| {md_escape(r['cluster_name'])} ({md_escape(r['cluster_id'])}) | {md_escape(r['top_finding'])} |")

    lines.extend([
        "",
        "## Supporting Signals",
    ])
    if not visible_results:
        lines.append("- No evaluated TiFlash clusters in scope.")
    for r in visible_results:
        weak = [rule for rule in r.get("rules", []) if rule.get("weak_signal")]
        no_activity = [rule for rule in r.get("rules", []) if str(rule.get("observed", "")).startswith("no observed")]
        if weak:
            for rule in weak[:5]:
                lines.append(f"- {md_escape(r['cluster_name'])}: {md_escape(rule['rule'])} is supporting evidence only ({md_escape(rule['observed'])}).")
        if no_activity:
            joined = ", ".join(rule["rule"].split()[0] for rule in no_activity[:8])
            lines.append(f"- {md_escape(r['cluster_name'])}: no activity observed for {joined}; treated as Healthy, not Unknown.")
        if not weak and not no_activity:
            lines.append(f"- {md_escape(r['cluster_name'])}: no weak or no-activity supporting signals.")

    lines.extend(["", "## Recommended Actions"])
    for index, action in enumerate(recommended_actions(results), 1):
        lines.append(f"{index}. {action}")

    if include_queries:
        lines.extend(["", "## Evidence Queries", "Data Source: Clinic Data Proxy"])
        for r in visible_results:
            for rule_id, query in r.get("queries", []):
                lines.extend(["", f"`{md_escape(r['cluster_name'])}/{rule_id}`", "```promql", query, "```"])

    return "\n".join(lines) + "\n"


def render_full_report(results: list[dict[str, Any]], start: int, end: int, scope: str, data_source: str, include_queries: bool = True) -> str:
    overall = overall_status(results)

    lines: list[str] = [
        "# TiFlash Health Inspection Report",
        "",
        "## Health Summary",
        f"Status: {overall}",
        f"Scope: {scope}",
        f"Time Range: {utc_ts(start)} ~ {utc_ts(end)}",
        f"Data Source: {data_source}",
        f"Clusters Checked: {len(results)}",
        f"Conclusion: {summary_sentence(overall, results)}",
        "",
        "## Top Risks",
        "| Severity | Rule | Evidence | Impact |",
        "|---|---|---|---|",
    ]
    top_risks = actionable_rules(results)
    if not top_risks:
        lines.append("| Healthy | None | No warning or critical rule fired | No immediate TiFlash risk found from covered metrics |")
    else:
        for cluster, rule in top_risks[:30]:
            lines.append(f"| {rule['status']} | {md_escape(cluster['cluster_name'])}: {md_escape(rule['rule'])} | {md_escape(rule['observed'])} | Review affected TiFlash workload or resource domain |")

    lines.extend([
        "",
        "## Unknowns",
        "| Scope | Missing Data | Attempted Discovery | Effect |",
        "|---|---|---|---|",
    ])
    unknown_rows = [(r, u) for r in results if r.get("status") != "Skipped" for u in r.get("unknowns", [])]
    if not unknown_rows:
        lines.append("| None | None | All required discovered metrics were evaluated | None |")
    else:
        for cluster, unknown in unknown_rows[:50]:
            lines.append(f"| {md_escape(cluster['cluster_name'])}/{md_escape(unknown['scope'])} | {md_escape(unknown['missing'])} | {md_escape(unknown['attempt'])} | {md_escape(unknown['effect'])} |")

    lines.extend([
        "",
        "## Skipped Clusters",
        "| Cluster | Reason |",
        "|---|---|",
    ])
    skipped_results = [r for r in results if r.get("status") == "Skipped"]
    if not skipped_results:
        lines.append("| None | None |")
    else:
        for r in skipped_results:
            lines.append(f"| {md_escape(r['cluster_name'])} ({md_escape(r['cluster_id'])}) | {md_escape(r['top_finding'])} |")

    lines.extend([
        "",
        "## Cluster Summary",
        "| Cluster | Status | Coverage | TiFlash | Tier Run | Likely Bottleneck | Top Finding |",
        "|---|---|---|---|---|---|---|",
    ])
    for r in results:
        tiflash = f"{len(r.get('tiflash_instances', []))} metric instances"
        if r["status"] == "Skipped":
            tiflash = "not detected"
        lines.append(f"| {md_escape(r['cluster_name'])} ({md_escape(r['cluster_id'])}) | {r['status']} | {r['coverage']} | {tiflash} | {r['tier_run']} | {r['likely_bottleneck']} | {md_escape(r['top_finding'])} |")

    for r in results:
        if r["status"] == "Healthy":
            continue
        lines.extend(render_cluster_section(r, include_queries))

    return "\n".join(lines) + "\n"


def summary_sentence(overall: str, results: list[dict[str, Any]]) -> str:
    counts = {state: sum(1 for r in results if r["status"] == state) for state in ["Critical", "Warning", "Unknown", "Healthy", "Skipped"]}
    unknown_rules = sum(len(r.get("unknowns", [])) for r in results if r.get("status") != "Skipped")
    return (
        f"{overall} overall; "
        f"clusters: critical={counts['Critical']}, warning={counts['Warning']}, unknown={counts['Unknown']}, "
        f"healthy={counts['Healthy']}, skipped={counts['Skipped']}; "
        f"unknown_rules={unknown_rules}."
    )


def render_cluster_section(r: dict[str, Any], include_queries: bool = True) -> list[str]:
    lines = [
        "",
        f"## Cluster: {r['cluster_name']} ({r['cluster_id']})",
        "",
        "### Metadata",
        "| Field | Value |",
        "|---|---|",
    ]
    for key, value in r["metadata"].items():
        lines.append(f"| {md_escape(key)} | {md_escape(value)} |")
    lines.append(f"| TiFlash Instances | {md_escape(', '.join(r.get('tiflash_instances', [])) or 'none detected')} |")
    lines.append(f"| Selector | `{md_escape(r.get('selector', ''))}` |")
    lines.append(f"| Namespace | `{md_escape(r.get('namespace', ''))}` |")

    sections = [
        ("Coverage", [rule for rule in r.get("rules", []) if rule["rule"].startswith("D")]),
        ("Query Health", [rule for rule in r.get("rules", []) if rule["rule"].split()[0].startswith("Q")]),
        ("MPP Task Health", [rule for rule in r.get("rules", []) if rule["rule"].split()[0].startswith("M")]),
        ("Resource Health", [rule for rule in r.get("rules", []) if rule["rule"].split()[0].startswith("R")]),
        ("Storage IO", [rule for rule in r.get("rules", []) if rule["rule"].split()[0].startswith("S")]),
        ("Network IO", [rule for rule in r.get("rules", []) if rule["rule"].split()[0].startswith("N")]),
        ("Raft And Replica Health", [rule for rule in r.get("rules", []) if rule["rule"].split()[0].startswith("A")]),
        ("Pipeline Model", [rule for rule in r.get("rules", []) if rule["rule"].split()[0].startswith("P")]),
    ]
    for title, rules in sections:
        if not rules:
            continue
        lines.extend(["", f"### {title}", "| Rule | Status | Observed | Threshold | Evidence |", "|---|---|---|---|---|"])
        for rule in rules:
            lines.append(f"| {md_escape(rule['rule'])} | {rule['status']} | {md_escape(rule['observed'])} | {md_escape(rule['threshold'])} | {rule['evidence']} |")

    lines.extend([
        "",
        "### Bottleneck Attribution",
        f"Likely Bottleneck: {r['likely_bottleneck']}",
        "Confidence: Low" if r["likely_bottleneck"] == "Unknown" else "Confidence: Medium",
        "",
        "Evidence:",
    ])
    abnormal = [rule for rule in r.get("rules", []) if rule.get("status") in {"Critical", "Warning"}]
    if abnormal:
        for rule in abnormal[:8]:
            lines.append(f"- {rule['rule']}: {rule['observed']}")
    else:
        lines.append("- No warning or critical finding from covered metrics.")

    if r.get("status") == "Skipped":
        lines.extend(["", "Skipped Reason:"])
        lines.append(f"- {md_escape(r['top_finding'])}")
    else:
        lines.extend(["", "Unknowns:"])
    if r.get("status") != "Skipped" and r.get("unknowns"):
        for unknown in r["unknowns"][:10]:
            lines.append(f"- {unknown['scope']}: {unknown['missing']} ({unknown['effect']})")
    elif r.get("status") != "Skipped":
        lines.append("- None.")

    if include_queries:
        lines.extend(["", "### Evidence Queries", "Data Source: Clinic Data Proxy", ""])
        for rule_id, query in r.get("queries", [])[:30]:
            lines.extend([f"`{rule_id}`", "```promql", query, "```"])

    lines.extend(["", "### Recommended Actions"])
    for action in recommended_actions([r]):
        lines.append(f"- {action}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster-id", action="append", help="Inspect a specific cluster id. Repeat to inspect selected clusters.")
    parser.add_argument("--search-id", default=DEFAULT_ORG_SEARCH_ID)
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--trend-hours", type=int, default=24)
    parser.add_argument("--start", type=int, help="Absolute Unix start timestamp. Defaults to end minus --window-minutes.")
    parser.add_argument("--end", type=int, help="Absolute Unix end timestamp. Defaults to current time.")
    parser.add_argument("--step", default="60")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--output", help="Markdown output path.")
    parser.add_argument("--format", choices=["compact", "full"], default="compact")
    parser.add_argument("--include-queries", action="store_true", help="Include PromQL evidence queries in the report.")
    parser.add_argument("--jobs", type=int, default=4, help="Concurrent cluster inspections in batch mode.")
    args = parser.parse_args()

    client = ClinicClient(Path(args.env_file))
    end = args.end if args.end is not None else int(time.time())
    start = args.start if args.start is not None else end - args.window_minutes * 60
    if start >= end:
        raise RuntimeError("--start must be earlier than --end")
    trend_start = end - args.trend_hours * 3600

    if args.cluster_id:
        selected_cluster_ids = list(dict.fromkeys(args.cluster_id))
        clusters = []
        missing_cluster_ids = []
        for cluster_id in selected_cluster_ids:
            matches = [item for item in client.search_clusters(cluster_id, True) if str(item.get("clusterID")) == cluster_id]
            if not matches:
                missing_cluster_ids.append(cluster_id)
                continue
            clusters.append(matches[0])
        if missing_cluster_ids:
            raise RuntimeError(f"cluster id(s) not found: {', '.join(missing_cluster_ids)}")
        scope = f"selected clusters {', '.join(selected_cluster_ids)}"
    else:
        clusters = client.search_clusters(args.search_id, True)
        scope = f"default org search id {args.search_id}"

    inactive_statuses = {"deleted", "deleting", "paused"}
    report_clusters = clusters if args.cluster_id else [item for item in clusters if cluster_status(item) not in inactive_statuses]
    if not report_clusters:
        raise RuntimeError("no active clusters discovered")

    results_by_index: list[dict[str, Any] | None] = [None] * len(report_clusters)
    inspect_items = []
    for index, item in enumerate(report_clusters):
        status = cluster_status(item)
        if status in inactive_statuses:
            results_by_index[index] = skipped_cluster_result(
                item,
                f"Cluster status is {status}",
                "Cluster status",
                "cluster status precheck",
            )
        else:
            inspect_items.append((index, item))

    def inspect_one(index: int, item: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        cluster_id = item.get("clusterID")
        print(f"[{index + 1}/{len(report_clusters)}] inspecting {cluster_id}", file=sys.stderr)
        worker_client = ClinicClient(Path(args.env_file))
        result = inspect_cluster(worker_client, item, start, end, trend_start, args.step)
        print(f"[{index + 1}/{len(report_clusters)}] finished {cluster_id}: {result.get('status')}", file=sys.stderr)
        return index, result

    worker_count = 1 if args.cluster_id and len(args.cluster_id) == 1 else min(len(inspect_items), max(1, args.jobs))
    if inspect_items:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(inspect_one, index, item) for index, item in inspect_items]
            for future in as_completed(futures):
                index, result = future.result()
                results_by_index[index] = result
    results = [result for result in results_by_index if result is not None]

    report = render_report(results, start, end, scope, "Clinic Data Proxy", args.format, args.include_queries)
    output = Path(args.output) if args.output else Path(f"tiflash-health-inspection-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md")
    output.write_text(report)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
