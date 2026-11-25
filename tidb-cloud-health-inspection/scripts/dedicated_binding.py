#!/usr/bin/env python3
"""Bind the Dedicated/BYOC collector's report to canonical check_results.

`dedicated_health_inspection.inspect_cluster` is a proven, monolithic full-stack
collector whose output is organized around findings and per-domain signals, not
canonical Check IDs. Rather than re-implement its (live-only, hard-to-verify)
PromQL and sustained-window logic as selection-driven adapters, this module maps
its already-computed structured signals to canonical `check_results` so the
Dedicated/BYOC path stops bypassing the registry/engine and becomes `bound`.

Scope of this increment: only the checks whose status can be derived from the
collector's *structured* `analysis` dict, `metrics` presence, and `unknown_checks`
list are bound here. Checks the collector does not yet expose structurally are
left unbound; the engine synthesizes explicit N/A for them. The authoritative
overall status remains the collector's own `verdict` (see health_inspection.py),
so this binding changes coverage and evidence, never the proven verdict.

Binding收口 (2026-11-25): all 23 bound dedicated checks are now collected natively
via `collection_adapters` recipes, guardrail-verified at zero hard diff against a
same-window monolith baseline. `health_inspection.collect_full_stack` merges
native-primary (native authoritative, this binding add-based as the gap/error
fallback). This module is therefore retained only as:
  * the SCOPE-01/04/08 source (not yet modeled as native recipes), and
  * the error-path / per-check fallback (the monolith still runs for the verdict
    floor + metadata, so its bound results cost nothing extra), and
  * the `verdict_to_status` passthrough used for the overall severity floor.
The 23 per-check derivations below are no longer the primary path; they stay as the
reversible fallback until the monolith (`dedicated_health_inspection.py`) is retired,
at which point this module collapses to SCOPE + verdict passthrough.
NOTE (honest caveat): CDC-01/02 active (changefeed-present) paths were verified only
by unit tests, not on a live changefeed-enabled cluster — see
references/dedicated-basic-report-checklist.md §3.
"""
from __future__ import annotations

from typing import Any

from inspection_core import load_rules

_RULES = load_rules()


def _threshold(rule_name: str, key: str, default: float) -> float:
    spec = _RULES.get(rule_name)
    if isinstance(spec, dict) and isinstance(spec.get(key), (int, float)):
        return float(spec[key])
    return default


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _peak(metrics: dict[str, Any], *keys: str) -> float | None:
    """Largest finite value across the given scalar peaks or per-instance maps."""
    best: float | None = None
    for key in keys:
        value = metrics.get(key)
        candidates: list[Any] = []
        if isinstance(value, dict):
            candidates = list(value.values())
        elif value is not None:
            candidates = [value]
        for candidate in candidates:
            number = _num(candidate)
            if number is not None and (best is None or number > best):
                best = number
    return best


VERDICT_TO_STATUS = {
    "healthy": "HEALTHY",
    "warning": "WARNING",
    "error": "CRITICAL",
    "needs review": "N/A",
    "unknown": "N/A",
}


def verdict_to_status(verdict: Any) -> str:
    return VERDICT_TO_STATUS.get(str(verdict or "").strip().lower(), "N/A")


def _result(check_id: str, status: str, reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"check_id": check_id, "status": status, "evidence": evidence or {}, "reason": reason}


def _unknown_has(report: dict[str, Any], *needles: str) -> bool:
    unknown = report.get("unknown_checks") or []
    return any(any(needle in str(item) for needle in needles) for item in unknown)


def bind_dedicated_results(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a dedicated collector report to canonical check_results (confident subset)."""
    analysis = report.get("analysis") or {}
    metrics = report.get("metrics") or {}
    results: list[dict[str, Any]] = []

    # ── Scope (full-stack is authorized for Dedicated/BYOC) ──
    scope_evidence = {"full_stack_authorized": True, "physical_infrastructure_queried": True}
    for check_id in ("SCOPE-01", "SCOPE-04", "SCOPE-08"):
        results.append(_result(check_id, "HEALTHY", "scope_resolved", scope_evidence))

    # ── Overview ──
    overview = {
        "provider": report.get("provider"),
        "region": report.get("region"),
        "version": report.get("version"),
        "status": report.get("status"),
    }
    md_present = any(value not in (None, "") for value in overview.values())
    results.append(
        _result("OVR-01", "HEALTHY" if md_present else "N/A", "metadata_resolved" if md_present else "metadata_unavailable", overview)
    )
    results.append(
        _result("OVR-02", "HEALTHY" if overview["status"] else "N/A", "product_state_resolved" if overview["status"] else "state_unavailable", {"status": overview["status"]})
    )

    # ── TiKV CPU (TIKV-01) ──
    cpu_max = metrics.get("cpu_max_by_instance")
    if analysis.get("cpu_sustained_high"):
        results.append(_result("TIKV-01", "WARNING", "cpu_sustained_high", {"cpu_sustained_high": analysis["cpu_sustained_high"], "cpu_max_by_instance": cpu_max}))
    elif cpu_max:
        results.append(_result("TIKV-01", "HEALTHY", "within_threshold", {"cpu_max_by_instance": cpu_max}))
    else:
        results.append(_result("TIKV-01", "N/A", "cpu_metric_unavailable"))

    # ── TiKV memory (TIKV-02) ──
    if analysis.get("memory_error_windows"):
        results.append(_result("TIKV-02", "CRITICAL", "memory_over_error_threshold", {"memory_error_windows": analysis["memory_error_windows"]}))
    elif analysis.get("memory_warn_peaks") or analysis.get("memory_leak_warning_nodes") or analysis.get("memory_trend_risers"):
        results.append(_result("TIKV-02", "WARNING", "memory_pressure_or_growth", {
            "memory_warn_peaks": analysis.get("memory_warn_peaks"),
            "memory_leak_warning_nodes": analysis.get("memory_leak_warning_nodes"),
            "memory_trend_risers": analysis.get("memory_trend_risers"),
        }))
    elif metrics.get("memory_max_by_instance"):
        results.append(_result("TIKV-02", "HEALTHY", "within_threshold", {"memory_max_by_instance": metrics.get("memory_max_by_instance")}))
    else:
        results.append(_result("TIKV-02", "N/A", "memory_metric_unavailable"))

    # ── TiKV gRPC (TIKV-03) ──
    if analysis.get("grpc_fail_spikes") or analysis.get("grpc_p99_sustained"):
        results.append(_result("TIKV-03", "WARNING", "grpc_fail_or_latency", {
            "grpc_fail_spikes": analysis.get("grpc_fail_spikes"),
            "grpc_p99_sustained": analysis.get("grpc_p99_sustained"),
        }))
    elif metrics.get("grpc_qps_avg_by_instance"):
        results.append(_result("TIKV-03", "HEALTHY", "within_threshold", {"grpc_qps_avg_by_instance": metrics.get("grpc_qps_avg_by_instance")}))
    else:
        results.append(_result("TIKV-03", "N/A", "grpc_metric_unavailable"))

    # ── TiKV compaction (TIKV-06) ──
    if analysis.get("compaction_pending_sustained") or analysis.get("compaction_pending_peak_breaches"):
        results.append(_result("TIKV-06", "WARNING", "compaction_pending_high", {
            "compaction_pending_sustained": analysis.get("compaction_pending_sustained"),
            "compaction_pending_peak_breaches": analysis.get("compaction_pending_peak_breaches"),
        }))
    elif _unknown_has(report, "compaction"):
        results.append(_result("TIKV-06", "N/A", "compaction_metric_unavailable"))
    else:
        results.append(_result("TIKV-06", "HEALTHY", "within_threshold"))

    # ── TiKV unified read pool (TIKV-07) ──
    if analysis.get("unified_read_hotspots"):
        results.append(_result("TIKV-07", "WARNING", "unified_read_pool_hotspot", {"unified_read_hotspots": analysis["unified_read_hotspots"]}))
    elif _unknown_has(report, "Unified Read Pool"):
        results.append(_result("TIKV-07", "N/A", "unified_read_pool_metric_unavailable"))
    else:
        results.append(_result("TIKV-07", "HEALTHY", "within_threshold"))

    # ── TiKV restart / availability restart (TIKV-10, AVL-02) ──
    restarts = metrics.get("restarts_by_instance") or {}
    if restarts:
        any_restart = any((value or 0) > 0 for value in restarts.values())
        restart_status = "WARNING" if any_restart else "HEALTHY"
        restart_reason = "restarts_detected" if any_restart else "no_restarts"
    else:
        restart_status, restart_reason = "N/A", "restart_metric_unavailable"
    results.append(_result("TIKV-10", restart_status, restart_reason, {"restarts_by_instance": restarts}))
    results.append(_result("AVL-02", restart_status, restart_reason, {"restarts_by_instance": restarts}))

    # ── Physical store usage (INF-03) ──
    if analysis.get("store_usage_fast_growth"):
        results.append(_result("INF-03", "WARNING", "store_usage_fast_growth", {"store_usage_fast_growth": analysis["store_usage_fast_growth"]}))
    elif metrics.get("store_usage_by_instance"):
        results.append(_result("INF-03", "HEALTHY", "within_threshold", {"store_usage_by_instance": metrics.get("store_usage_by_instance")}))
    else:
        results.append(_result("INF-03", "N/A", "store_usage_metric_unavailable"))

    # ── Region distribution (PD-03) — observation only ──
    region = metrics.get("region_by_instance") or {}
    if region:
        results.append(_result("PD-03", "HEALTHY", "region_distribution_observed", {
            "region_by_instance": region,
            "leader_by_instance": metrics.get("leader_by_instance"),
        }))
    else:
        results.append(_result("PD-03", "N/A", "region_metric_unavailable"))

    # ── TiKV write stability (TIKV-04) ← write-stall peak ──
    write_stall = _peak(metrics, "write_stall_peak")
    if write_stall is None:
        results.append(_result("TIKV-04", "N/A", "write_stall_metric_unavailable"))
    elif write_stall > 0:
        results.append(_result("TIKV-04", "WARNING", "write_stall_observed", {"write_stall_peak": write_stall}))
    else:
        results.append(_result("TIKV-04", "HEALTHY", "no_write_stall", {"write_stall_peak": write_stall}))

    # ── TiKV flow control (TIKV-05) ← scheduler throttle / discard ──
    discard = _peak(metrics, "scheduler_discard_ratio_peak")
    throttle = _peak(metrics, "scheduler_throttle_peak_seconds", "scheduler_throttle_total_seconds")
    if discard is not None and discard > 0:
        results.append(_result("TIKV-05", "WARNING", "scheduler_requests_discarded", {"scheduler_discard_ratio_peak": discard}))
    elif discard is not None or throttle is not None:
        results.append(_result("TIKV-05", "HEALTHY", "flow_control_within_range", {"scheduler_discard_ratio_peak": discard, "scheduler_throttle_peak_seconds": throttle}))
    else:
        results.append(_result("TIKV-05", "N/A", "flow_control_metric_unavailable"))

    # ── TiKV error signals (TIKV-08) ← error counters + error log count ──
    error_keys = ("critical_error_total", "channel_full_total", "server_report_failure_total", "grpc_fail_total", "error_log_count")
    error_values = {k: _num(metrics.get(k)) for k in error_keys if metrics.get(k) is not None}
    if not error_values:
        results.append(_result("TIKV-08", "N/A", "error_signal_metric_unavailable"))
    elif any((v or 0) > 0 for v in error_values.values()):
        results.append(_result("TIKV-08", "WARNING", "error_signals_present", error_values))
    else:
        results.append(_result("TIKV-08", "HEALTHY", "no_error_signals", error_values))

    # ── TiKV responsiveness (TIKV-09) ← slow score + raft log lag P99 ──
    slow_peak = _peak(metrics, "slow_score_peak", "slow_score_by_instance")
    lag_peak = _peak(metrics, "raft_log_lag_p99_peak", "raft_log_lag_p99_by_instance")
    slow_warn = _threshold("tikv_slow_score", "warning", 100.0)
    lag_warn = _threshold("tikv_raft_log_lag_p99", "warning", 5000.0)
    if slow_peak is None and lag_peak is None:
        results.append(_result("TIKV-09", "N/A", "responsiveness_metric_unavailable"))
    elif (slow_peak is not None and slow_peak >= slow_warn) or (lag_peak is not None and lag_peak >= lag_warn):
        results.append(_result("TIKV-09", "WARNING", "slow_score_or_raft_lag_high", {"slow_score_peak": slow_peak, "raft_log_lag_p99_peak": lag_peak}))
    else:
        results.append(_result("TIKV-09", "HEALTHY", "within_threshold", {"slow_score_peak": slow_peak, "raft_log_lag_p99_peak": lag_peak}))

    # ── Disk throughput / IOPS (INF-04) ← exceeded-IOPS peak ──
    iops_exceed = _peak(metrics, "iops_exceed_peak")
    iops_present = _peak(metrics, "disk_iops_peak", "node_iops_peak", "disk_throughput_peak_bytes_per_s")
    iops_warn = _threshold("inf_iops_exceeded", "warning", 1.0)
    if iops_exceed is not None and iops_exceed >= iops_warn:
        results.append(_result("INF-04", "WARNING", "exceeded_iops", {"iops_exceed_peak": iops_exceed}))
    elif iops_exceed is not None or iops_present is not None:
        results.append(_result("INF-04", "HEALTHY", "within_threshold", {"iops_exceed_peak": iops_exceed, "disk_iops_peak": metrics.get("disk_iops_peak")}))
    else:
        results.append(_result("INF-04", "N/A", "disk_io_metric_unavailable"))

    # ── Disk latency (INF-05) ← read/write latency peaks (observation) ──
    read_lat = _peak(metrics, "disk_read_latency_peak")
    write_lat = _peak(metrics, "disk_write_latency_peak")
    if read_lat is None and write_lat is None:
        results.append(_result("INF-05", "N/A", "disk_latency_metric_unavailable"))
    else:
        results.append(_result("INF-05", "HEALTHY", "disk_latency_observed", {"disk_read_latency_peak": read_lat, "disk_write_latency_peak": write_lat}))

    # ── TiDB compute node CPU (TIDB-01) ← CPU% vs GOMAXPROCS quota ──
    tidb_cpu = _peak(metrics, "tidb_cpu_max_by_instance")
    tidb_cpu_warn = _threshold("tidb_cpu_pct", "warning", 85.0)
    if tidb_cpu is None:
        results.append(_result("TIDB-01", "N/A", "tidb_cpu_metric_unavailable"))
    elif tidb_cpu >= tidb_cpu_warn:
        results.append(_result("TIDB-01", "WARNING", "tidb_cpu_high", {"tidb_cpu_max_by_instance": metrics.get("tidb_cpu_max_by_instance")}))
    else:
        results.append(_result("TIDB-01", "HEALTHY", "within_threshold", {"tidb_cpu_max_by_instance": metrics.get("tidb_cpu_max_by_instance")}))

    # ── §6 Reliability / HA / DR ─────────────────────────────
    # Availability / product status (AVL-01).
    product_status = str(report.get("status") or "").strip()
    if not product_status:
        results.append(_result("AVL-01", "N/A", "status_unavailable"))
    elif product_status.upper() in ("AVAILABLE", "NORMAL", "RUNNING", "HEALTHY", "ACTIVE"):
        results.append(_result("AVL-01", "HEALTHY", "product_available", {"status": product_status}))
    else:
        results.append(_result("AVL-01", "WARNING", "product_status_abnormal", {"status": product_status}))

    # Single-point / topology risk (AVL-08) ← TiKV instance count (≥3 = redundant).
    # Prefer the pod list; fall back to any per-instance metric map.
    pods = metrics.get("tikv_pods")
    tikv_count: float | None = None
    if isinstance(pods, (list, tuple, dict)) and len(pods) > 0:
        tikv_count = float(len(pods))
    else:
        for key in ("cpu_max_by_instance", "memory_max_by_instance", "region_by_instance", "store_usage_by_instance"):
            inst = metrics.get(key)
            if isinstance(inst, dict) and inst:
                tikv_count = float(len(inst))
                break
    if not tikv_count:
        results.append(_result("AVL-08", "N/A", "topology_metric_unavailable"))
    elif tikv_count < 3:
        results.append(_result("AVL-08", "WARNING", "single_point_risk", {"tikv_instance_count": tikv_count}))
    else:
        results.append(_result("AVL-08", "HEALTHY", "redundant_topology", {"tikv_instance_count": tikv_count}))

    # Changefeed inventory / state (CDC-01) ← ticdc_coordinator_changefeed_state.
    states = metrics.get("changefeed_states")
    if not isinstance(states, dict) or not states:
        results.append(_result("CDC-01", "N/A", "changefeed_metric_unavailable"))
    else:
        total = _num(states.get("Total")) or 0
        working = _num(states.get("Working")) or 0
        failed = (_num(states.get("Failed")) or 0) + (_num(states.get("Error")) or 0)
        stopped = _num(states.get("Stopped")) or 0
        if total == 0:
            results.append(_result("CDC-01", "HEALTHY", "no_changefeeds", {"changefeed_states": states}))
        elif failed > 0:
            results.append(_result("CDC-01", "WARNING", "changefeed_failed", {"changefeed_states": states}))
        elif working < total or stopped > 0:
            results.append(_result("CDC-01", "NOTICE", "changefeed_not_all_working", {"changefeed_states": states}))
        else:
            results.append(_result("CDC-01", "HEALTHY", "all_changefeeds_working", {"changefeed_states": states}))

    # Changefeed checkpoint / resolved-ts lag (CDC-02).
    lag = _peak(metrics, "cdc_checkpoint_lag_max", "cdc_resolved_lag_max")
    if lag is None:
        results.append(_result("CDC-02", "N/A", "changefeed_lag_metric_unavailable"))
    else:
        warn = _threshold("cdc_checkpoint_lag_s", "warning", 300.0)
        crit = _threshold("cdc_checkpoint_lag_s", "critical", 3600.0)
        cdc_status = "CRITICAL" if lag >= crit else ("WARNING" if lag >= warn else "HEALTHY")
        results.append(_result("CDC-02", cdc_status, "checkpoint_lag", {"cdc_checkpoint_lag_max": lag}))

    # ── TiDB compute node memory (TIDB-02) ← raw RSS peak (observation) ──
    # No verified product-exposed TiDB memory limit for a logical %, so RSS is
    # reported as observation, never scored.
    tidb_rss = _peak(metrics, "tidb_memory_max_by_instance")
    if tidb_rss is None:
        results.append(_result("TIDB-02", "N/A", "tidb_memory_metric_unavailable"))
    else:
        results.append(_result("TIDB-02", "HEALTHY", "tidb_rss_observed", {"tidb_memory_max_by_instance": metrics.get("tidb_memory_max_by_instance")}))

    return results
