#!/usr/bin/env python3
"""Selection-driven collection layer for TiDB Cloud health inspection.

The registry (``checklist.v1.json`` + ``checklist_registry.py``) decides WHAT to
collect; this module decides HOW. A collector no longer owns a hard-coded metric
set: it iterates ``selection["runnable_check_ids"]`` and, for each one, looks up a
recipe in ``config/collection.v1.json`` and runs the named adapter.

Consequences:
- Prohibited (``X``) checks are never in ``runnable_check_ids`` and therefore are
  never collected — fail-closed at collection time, not after the fact.
- A runnable check with no recipe becomes ``N/A(no_collection_recipe)``; it never
  crashes the run and is never silently Healthy.
- One per-run query cache de-duplicates shared PromQL so declaring one recipe per
  check does not multiply API calls.

The generic adapters (``promql_distribution``, ``promql_ratio``,
``cluster_metadata``) and the logical named adapters (``ru_consumption``,
``scope_guard``, ``worst_of``, …) are functional. ``tikv_thread_pool_pressure``
remains a stub until the dedicated per-thread logic is ported into it.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from inspection_core import TimeWindow, distribution, load_rules, status_high

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPES = ROOT / "config" / "collection.v1.json"

CheckResult = dict[str, Any]
Adapter = Callable[["CollectContext", str, dict[str, Any]], CheckResult]

STATUS_RANK = {"HEALTHY": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3}

ADAPTERS: dict[str, Adapter] = {}
# Derived adapters run in a second pass and may read sibling results via
# ctx.collected (e.g. a worst-of summary over other checks).
DERIVED_ADAPTERS: set[str] = set()


def adapter(name: str, *, derived: bool = False) -> Callable[[Adapter], Adapter]:
    """Register an adapter under a stable name used by recipes."""

    def register(fn: Adapter) -> Adapter:
        if name in ADAPTERS:
            raise ValueError(f"duplicate adapter name: {name}")
        ADAPTERS[name] = fn
        if derived:
            DERIVED_ADAPTERS.add(name)
        return fn

    return register


# ── result helpers ───────────────────────────────────────────

def result(check_id: str, status: str, evidence: dict[str, Any], reason: str) -> CheckResult:
    return {"check_id": check_id, "status": status, "evidence": evidence, "reason": reason}


def na(check_id: str, reason: str, evidence: dict[str, Any] | None = None) -> CheckResult:
    return result(check_id, "N/A", evidence or {}, reason)


# ── rules ────────────────────────────────────────────────────
# `load_rules` lives in inspection_core (shared by this layer and the full-stack
# collector) and is re-exported here for existing imports.


def status_low(value: float, notice: float, warning: float, critical: float) -> str:
    """Lower is worse (e.g. headroom / available ratio). Bands descend."""
    if value > notice:
        return "HEALTHY"
    if value > warning:
        return "NOTICE"
    if value > critical:
        return "WARNING"
    return "CRITICAL"


def apply_rule(ctx: "CollectContext", rule_name: str, value: float) -> tuple[str, str]:
    """Map a scaled value to a status via a named rule. Fail-closed: an unknown or
    malformed rule yields N/A, never a fabricated status.

    'notice' defaults to 'warning' (no NOTICE band). 'critical' is optional:
    omit it to never escalate to CRITICAL. 'params' rules are not directly
    applicable here — their owning named adapter reads the knobs instead.
    """
    spec = (ctx.rules or {}).get(rule_name)
    if not isinstance(spec, dict):
        return "N/A", "rule_not_defined"
    kind = spec.get("kind", "high")
    if kind == "params":
        return "N/A", "rule_requires_named_adapter"
    if kind not in ("high", "low"):
        return "N/A", "unsupported_rule_kind"
    warning = spec.get("warning")
    if warning is None:
        return "N/A", "rule_malformed"
    notice = spec.get("notice", warning)
    critical = spec.get("critical")
    if kind == "high":
        crit = critical if critical is not None else math.inf
        return status_high(value, notice, warning, crit), "deterministic_threshold"
    crit = critical if critical is not None else -math.inf
    return status_low(value, notice, warning, crit), "deterministic_threshold"


# ── collection context with per-run query cache ──────────────

_CLUSTER_ID_MATCHER = re.compile(r'tidb_cluster_id\s*=\s*"[^"]*"')


def strip_cluster_id_filter(query: str) -> str:
    """Remove any ``tidb_cluster_id="..."`` label matcher from a PromQL query and
    tidy the resulting selector. Used as a safety fallback for clusters whose
    metrics do not carry the ``tidb_cluster_id`` label: the canonical Clinic API
    path is already scoped to one cluster, so an unscoped query returns only this
    cluster's data. Returns the query unchanged when no such matcher is present."""
    s = _CLUSTER_ID_MATCHER.sub("", query)
    s = re.sub(r",\s*,", ",", s)      # collapse double commas left behind
    s = re.sub(r"\{\s*,\s*", "{", s)  # leading comma inside a selector
    s = re.sub(r"\s*,\s*\}", "}", s)  # trailing comma inside a selector
    return s


def downsample_series(series: list[tuple[float, float]], max_points: int = 240) -> list[list[float]]:
    """Reduce a [(ts, value)] series to at most ``max_points`` evenly-spaced points
    for compact chart storage. Returns [[ts, value], ...] (JSON-friendly)."""
    n = len(series)
    if n <= max_points:
        return [[ts, v] for ts, v in series]
    stride = n / max_points
    out: list[list[float]] = []
    i = 0.0
    while int(i) < n:
        ts, v = series[int(i)]
        out.append([ts, v])
        i += stride
    return out


@dataclass
class CollectContext:
    api: Any  # clinic_api.ClinicAPI (duck-typed for offline tests)
    cluster_id: str
    window: TimeWindow
    plan: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    namespace: str | None = None
    rules: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    collected: dict[str, CheckResult] = field(default_factory=dict)
    # Runtime substitutions for queries that need cluster-resolved bits (e.g. the
    # TiKV disk device regex / k8s namespace for node-disk→pod joins).
    substitutions: dict[str, str] = field(default_factory=dict)
    _cache: dict[tuple[str, str], Any] = field(default_factory=dict)

    def expand(self, query: str) -> str:
        out = query.replace("{cluster_id}", self.cluster_id).replace(
            "{window}", self.window.promql_duration)
        if self.namespace:
            out = out.replace("{namespace}", self.namespace)
        for name, value in (self.substitutions or {}).items():
            out = out.replace("{" + name + "}", str(value))
        return out

    def _range_response(self, query: str) -> Any:
        key = ("range", query)
        if key not in self._cache:
            self._cache[key] = self.api.query_range(
                self.cluster_id, query, self.window.start_iso, self.window.end_iso, str(self.window.step_seconds)
            )
        return self._cache[key]

    def range_values(self, query: str) -> list[float]:
        expanded = self.expand(query)
        response = self._range_response(expanded)
        if not isinstance(response, dict) or response.get("status") != "success":
            self.errors[expanded] = str((response or {}).get("error") or "query_failed")
            return []
        values: list[float] = []
        for series in response.get("data", {}).get("result", []):
            for sample in series.get("values", []):
                try:
                    v = float(sample[1])
                except (IndexError, TypeError, ValueError):
                    continue
                if math.isfinite(v):
                    values.append(v)
        return values

    def range_values_resilient(self, query: str) -> tuple[list[float], bool]:
        """Range query that tolerates clusters whose metrics lack the
        ``tidb_cluster_id`` label. Tries the query as written first; if that is
        empty and the query carries a ``tidb_cluster_id`` matcher, retries with
        that matcher stripped (safe: the API path is already cluster-scoped).
        Returns (values, cluster_id_label_absent) — the flag is True only when the
        scoped query was empty and the unscoped fallback recovered data."""
        values = self.range_values(query)
        if values:
            return values, False
        unscoped = strip_cluster_id_filter(self.expand(query))
        if unscoped != self.expand(query):
            fallback = self.range_values(unscoped)
            if fallback:
                return fallback, True
        return [], False

    def _series_from_response(self, response: Any) -> list[tuple[float, float]]:
        """First result series of a range response as [(epoch_seconds, value)],
        finite samples only."""
        if not isinstance(response, dict) or response.get("status") != "success":
            return []
        results = response.get("data", {}).get("result", [])
        if not results:
            return []
        series: list[tuple[float, float]] = []
        for sample in results[0].get("values", []):
            try:
                ts, v = float(sample[0]), float(sample[1])
            except (IndexError, TypeError, ValueError):
                continue
            if math.isfinite(v):
                series.append((ts, v))
        return series

    def range_series_resilient(self, query: str) -> tuple[list[tuple[float, float]], str | None]:
        """Range query returning a time series ``[(ts, value)]`` with two tolerant
        fallbacks, tried only when the query as written is empty:
        1. histogram ``by (le)`` → ``by (vmrange)`` (VictoriaMetrics-backed clusters,
           e.g. Essential, bucket latency on the ``vmrange`` label, not ``le``);
        2. strip a ``tidb_cluster_id`` matcher the cluster's metrics don't carry.
        Returns (series, fallback) where fallback is None / "vmrange" / "unscoped"."""
        expanded = self.expand(query)
        series = self._series_from_response(self._range_response(expanded))
        if series:
            return series, None
        if "by (le)" in expanded:
            alt = expanded.replace("by (le)", "by (vmrange)")
            s = self._series_from_response(self._range_response(alt))
            if s:
                return s, "vmrange"
        unscoped = strip_cluster_id_filter(expanded)
        if unscoped != expanded:
            s = self._series_from_response(self._range_response(unscoped))
            if s:
                return s, "unscoped"
        return [], None

    def instant_scalar(self, query: str) -> float | None:
        expanded = self.expand(query)
        key = ("instant", expanded)
        if key not in self._cache:
            self._cache[key] = self.api.query_instant(self.cluster_id, expanded, self.window.end_iso)
        response = self._cache[key]
        if not isinstance(response, dict) or response.get("status") != "success":
            self.errors[expanded] = str((response or {}).get("error") or "query_failed")
            return None
        try:
            value = float(response["data"]["result"][0]["value"][1])
        except (IndexError, KeyError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def instant_vector(self, query: str, key_field: str = "instance") -> dict[str, float]:
        """{instance: value} from an instant vector query — the per-instance map the
        dedicated collector builds (vector_stats). Shares the instant-query cache."""
        expanded = self.expand(query)
        key = ("instant", expanded)
        if key not in self._cache:
            self._cache[key] = self.api.query_instant(self.cluster_id, expanded, self.window.end_iso)
        response = self._cache[key]
        if not isinstance(response, dict) or response.get("status") != "success":
            self.errors[expanded] = str((response or {}).get("error") or "query_failed")
            return {}
        out: dict[str, float] = {}
        for entry in response.get("data", {}).get("result", []):
            metric = entry.get("metric") or {}
            k = str(metric.get(key_field) or metric.get("pod") or metric.get("type") or "unknown")
            try:
                v = float((entry.get("value") or [None, None])[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(v):
                out[k] = v
        return out


# ── generic adapters (functional) ────────────────────────────

@adapter("promql_distribution")
def promql_distribution(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    query = params["query"]
    kind = params.get("kind", "range")
    scale = float(params.get("scale", 1.0))
    unit = params.get("unit")
    rule = params.get("rule", "none")

    if kind == "instant":
        scalar = ctx.instant_scalar(query)
        if scalar is None:
            return na(check_id, "empty_series", {"query": ctx.expand(query), "unit": unit})
        evidence = {"query": ctx.expand(query), "value": scalar, "unit": unit, "scale": scale}
        if rule == "none":
            return result(check_id, "HEALTHY", evidence, "observation_only")
        status, reason = apply_rule(ctx, rule, scalar * scale)
        evidence.update({"rule": rule, "rule_value": scalar * scale})
        return result(check_id, status, evidence, reason)

    series, fallback = ctx.range_series_resilient(query)
    stats = distribution([v for _, v in series])
    if not stats:
        return na(check_id, "empty_series", {"query": ctx.expand(query), "unit": unit})
    # evidence_key lets a recipe match a report-contract field name
    # (e.g. "statistics_seconds" for latency series). Default "statistics".
    stats_key = params.get("evidence_key", "statistics")
    evidence = {"query": ctx.expand(query), stats_key: stats, "unit": unit, "scale": scale}
    if fallback:
        # Record which tolerant fallback recovered the data (vmrange/unscoped).
        evidence["series_fallback"] = fallback
    if params.get("retain_series"):
        # Downsampled raw series kept for the report's trend charts.
        evidence["series"] = downsample_series(series)
    if rule == "none":
        return result(check_id, "HEALTHY", evidence, "observation_only")
    rule_stat = params.get("rule_statistic", "max")
    value = float(stats[rule_stat]) * scale
    status, reason = apply_rule(ctx, rule, value)
    evidence.update({"rule": rule, "rule_statistic": rule_stat, "rule_value": value})
    return result(check_id, status, evidence, reason)


@adapter("promql_ratio")
def promql_ratio(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    scale = float(params.get("scale", 100.0))
    unit = params.get("unit", "%")
    rule = params.get("rule", "none")
    num = distribution(ctx.range_values(params["numerator"]))
    den = distribution(ctx.range_values(params["denominator"]))
    if not num or not den or float(den["avg"]) <= 0:
        return na(check_id, "non_comparable_or_empty", {
            "numerator": ctx.expand(params["numerator"]),
            "denominator": ctx.expand(params["denominator"]),
        })
    fraction = float(num["avg"]) / float(den["avg"])
    ratio = fraction * scale
    evidence = {
        "numerator_statistics": num,
        "denominator_statistics": den,
        "window_average_ratio": fraction,  # unscaled fraction
        "window_average_ratio_percent": ratio,  # scaled (percent when scale=100)
        "unit": unit,
    }
    if params.get("emit_estimated_count"):
        evidence["estimated_error_count"] = float(num["avg"]) * ctx.window.duration_seconds
    if rule == "none":
        return result(check_id, "HEALTHY", evidence, "observation_only")
    status, reason = apply_rule(ctx, rule, ratio)
    evidence.update({"rule": rule, "rule_value": ratio})
    return result(check_id, status, evidence, reason)


@adapter("per_instance_vector")
def per_instance_vector(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Per-instance peak map from an instant vector query — the shape the dedicated
    monolith builds via vector_stats (e.g. ``{instance: value}`` under
    ``tidb_memory_max_by_instance``). Evidence is kept **minimal** (only the named
    map, plus rule fields when a threshold applies) to match the collector's output
    exactly — see references/dedicated-migration-plan.md.

    Params: ``query``, ``evidence_key``; optional ``rule`` (default none →
    observation), ``scale``, ``healthy_reason``, ``na_reason``, ``key_field``."""
    vec = ctx.instant_vector(params["query"], params.get("key_field", "instance"))
    key = params["evidence_key"]
    if not vec:
        return na(check_id, params.get("na_reason", "metric_unavailable"))
    evidence: dict[str, Any] = {key: vec}  # minimal, to match the collector's shape
    rule = params.get("rule", "none")
    if rule == "none":
        return result(check_id, "HEALTHY", evidence, params.get("healthy_reason", "observation_only"))
    peak = max(vec.values()) * float(params.get("scale", 1.0))
    status, reason = apply_rule(ctx, rule, peak)
    if status == "HEALTHY" and params.get("healthy_reason"):
        reason = params["healthy_reason"]
    return result(check_id, status, evidence, reason)


@adapter("multi_instance_vector")
def multi_instance_vector(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Several per-instance vectors → one evidence dict under the named keys
    (observation-only), matching multi-map collector shapes like PD-03's
    region + leader. ``metrics`` is a list of {evidence_key, query[, key_field]}.
    N/A when none of them resolve. No raw thresholds here — deep judgment is
    delegated (see references/dedicated-migration-plan.md)."""
    evidence: dict[str, Any] = {}
    for m in params.get("metrics", []):
        vec = ctx.instant_vector(m["query"], m.get("key_field", "instance"))
        if vec:
            evidence[m["evidence_key"]] = vec
    if not evidence:
        return na(check_id, params.get("na_reason", "metric_unavailable"))
    return result(check_id, "HEALTHY", evidence, params.get("healthy_reason", "observation_only"))


@adapter("multi_scalar_peak")
def multi_scalar_peak(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Several named scalar peaks — the max across each query's instant vector —
    into one evidence dict, matching the collector's multi-peak shapes (INF-04/05).
    Observation-only, or a threshold on one named value (``threshold_key`` + ``rule``).
    N/A when none of the metrics resolve. Keys are always present (value None when a
    single metric is empty) so the evidence shape is stable.

    Three coarse basic-signal modes (deep judgment → o11y):
      * ``warn_if_positive``: list of evidence keys — WARNING when any value > 0
        (present/absent signals: write stall, discarded requests, error counters).
      * ``thresholds``: list of {key, rule[, scale]} — WARNING/CRITICAL when any
        named value crosses its rule (OR semantics: slow score OR raft log lag).
      * ``threshold_key`` + ``rule``: a single threshold on one named value.
    ``warn_reason`` names the fired reason. All modes keep the full evidence dict."""
    evidence: dict[str, Any] = {}
    for m in params.get("metrics", []):
        vec = ctx.instant_vector(m["query"], m.get("key_field", "pod"))
        evidence[m["evidence_key"]] = max(vec.values()) if vec else None
    if all(v is None for v in evidence.values()):
        return na(check_id, params.get("na_reason", "metric_unavailable"))
    healthy_reason = params.get("healthy_reason", "observation_only")
    warn_reason = params.get("warn_reason", "signal_present")
    # Mode A — present/absent: WARN when any named value is positive.
    pos_keys = params.get("warn_if_positive")
    if pos_keys:
        fired = any((evidence.get(k) or 0) > 0 for k in pos_keys)
        return result(check_id, "WARNING" if fired else "HEALTHY", evidence,
                      warn_reason if fired else healthy_reason)
    # Mode B — several thresholds OR'd into the worst status.
    thresholds = params.get("thresholds")
    if thresholds:
        worst = "HEALTHY"
        for t in thresholds:
            val = evidence.get(t["key"])
            if val is None:
                continue
            status, _ = apply_rule(ctx, t["rule"], float(val) * float(t.get("scale", 1.0)))
            if STATUS_RANK.get(status, 0) > STATUS_RANK.get(worst, 0):
                worst = status
        return result(check_id, worst, evidence,
                      healthy_reason if worst == "HEALTHY" else warn_reason)
    # Mode C — a single threshold on one named value.
    rule = params.get("rule", "none")
    if rule != "none":
        trigger = evidence.get(params.get("threshold_key"))
        if trigger is not None:
            status, reason = apply_rule(ctx, rule, float(trigger) * float(params.get("scale", 1.0)))
            if status == "HEALTHY" and params.get("healthy_reason"):
                reason = params["healthy_reason"]
            return result(check_id, status, evidence, reason)
    return result(check_id, "HEALTHY", evidence, params.get("healthy_reason", "observation_only"))


@adapter("disk_iops_check")
def disk_iops_check(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """INF-04: TiKV disk IOPS peak + exceeded-IOPS peak, WARNING (coarse) when the
    exceeded-IOPS peak crosses the threshold. ``disk_iops_peak`` is a plain disk-join
    peak; ``iops_exceed_peak`` comes from the clinic exceeded-IOPS-by-pod join (an
    AWS-EBS client-side method, not PromQL). Deep throttle root-cause → o11y."""
    iops_vec = ctx.instant_vector(params["iops_query"], "pod")
    disk_iops_peak = max(iops_vec.values()) if iops_vec else None
    iops_exceed_peak = None
    try:
        resp = ctx.api.query_exceeded_iops_by_pod(
            ctx.cluster_id, "tikv", ctx.window.start_iso, ctx.window.end_iso,
            step="300", rate_window="2m")
        peaks: list[float] = []
        results = resp.get("data", {}).get("result", []) if isinstance(resp, dict) else []
        for entry in results:
            nums = []
            for v in entry.get("values") or []:
                try:
                    f = float(v[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if math.isfinite(f):
                    nums.append(f)
            if nums:
                peaks.append(max(nums))
        iops_exceed_peak = max(peaks) if peaks else None
    except Exception:
        iops_exceed_peak = None
    if disk_iops_peak is None and iops_exceed_peak is None:
        return na(check_id, params.get("na_reason", "disk_io_metric_unavailable"))
    evidence = {"disk_iops_peak": disk_iops_peak, "iops_exceed_peak": iops_exceed_peak}
    if iops_exceed_peak is not None:
        status, reason = apply_rule(ctx, params.get("rule", "inf_iops_exceeded"), iops_exceed_peak)
        if status == "HEALTHY" and params.get("healthy_reason"):
            reason = params["healthy_reason"]
        return result(check_id, status, evidence, reason)
    return result(check_id, "HEALTHY", evidence, params.get("healthy_reason", "within_threshold"))


@adapter("scalar_peak_probe")
def scalar_peak_probe(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """A presence + coarse-threshold probe whose HEALTHY shape carries NO evidence —
    matching the monolith's empty-evidence HEALTHY for TIKV-06 (compaction) and
    TIKV-07 (unified read pool), where the deep sustained/hotspot judgment is
    delegated to o11y. N/A when the metric is absent; HEALTHY (empty) when present and
    under the coarse rule (or rule=none); evidence {evidence_key: peak} only when the
    coarse rule fires (WARNING/CRITICAL)."""
    vec = ctx.instant_vector(params["query"], params.get("key_field", "instance"))
    if not vec:
        return na(check_id, params.get("na_reason", "metric_unavailable"))
    peak = max(vec.values())
    rule = params.get("rule", "none")
    if rule != "none":
        status, reason = apply_rule(ctx, rule, peak * float(params.get("scale", 1.0)))
        if status != "HEALTHY":
            return result(check_id, status, {params["evidence_key"]: peak},
                          params.get("warn_reason", reason))
    return result(check_id, "HEALTHY", {}, params.get("healthy_reason", "within_threshold"))


@adapter("product_status_check")
def product_status_check(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """AVL-01: product availability from cluster-metadata status. HEALTHY when the
    status is a known-good value, WARNING otherwise, N/A when absent. Status comes
    from ctx.metadata (cluster detail), not PromQL."""
    status_val = str((ctx.metadata or {}).get(params.get("field", "status")) or "").strip()
    if not status_val:
        return na(check_id, params.get("na_reason", "status_unavailable"))
    good = {str(s).upper() for s in params.get(
        "healthy_values", ["AVAILABLE", "NORMAL", "RUNNING", "HEALTHY", "ACTIVE"])}
    if status_val.upper() in good:
        return result(check_id, "HEALTHY", {"status": status_val},
                      params.get("healthy_reason", "product_available"))
    return result(check_id, "WARNING", {"status": status_val},
                  params.get("warn_reason", "product_status_abnormal"))


@adapter("instance_count_check")
def instance_count_check(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """AVL-08: topology redundancy from the count of instances in a per-instance
    vector. WARNING (single-point risk) below min_count, else HEALTHY; N/A when the
    vector is empty. Evidence is the instance count only."""
    vec = ctx.instant_vector(params["query"], params.get("key_field", "instance"))
    if not vec:
        return na(check_id, params.get("na_reason", "topology_metric_unavailable"))
    count = float(len(vec))
    evidence = {params.get("evidence_key", "tikv_instance_count"): count}
    if count < float(params.get("min_count", 3)):
        return result(check_id, "WARNING", evidence, params.get("warn_reason", "single_point_risk"))
    return result(check_id, "HEALTHY", evidence, params.get("healthy_reason", "redundant_topology"))


@adapter("metadata_fields")
def metadata_fields(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Flat evidence from named cluster-metadata fields — the OVR-01/02 shape
    ({provider, region, version, status} / {status}). HEALTHY when any field is
    present; N/A otherwise. Fields come from ctx.metadata (cluster detail)."""
    md = ctx.metadata or {}
    evidence = {name: md.get(name) for name in params.get("fields", [])}
    if any(value not in (None, "") for value in evidence.values()):
        return result(check_id, "HEALTHY", evidence, params.get("healthy_reason", "metadata_resolved"))
    return na(check_id, params.get("na_reason", "metadata_unavailable"))


@adapter("changefeed_state_check")
def changefeed_state_check(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """CDC-01: changefeed inventory / state from ``ticdc_coordinator_changefeed_state``
    (keyed by the ``state`` label, max per state). N/A when no changefeeds exist.
    Status mirrors the dedicated binding: no changefeeds / all working → HEALTHY,
    any failed or errored → WARNING, some not-working or stopped → NOTICE. Deep lag /
    root-cause is delegated to o11y. NOTE: only the N/A path is guardrail-verifiable on
    a cluster without changefeeds; the active paths are ported faithfully from the
    binding and exercised where changefeeds exist."""
    states = ctx.instant_vector(params["query"], "state")
    key = params.get("evidence_key", "changefeed_states")
    if not states:
        return na(check_id, params.get("na_reason", "changefeed_metric_unavailable"))

    def g(name: str) -> float:
        return float(states.get(name) or 0)

    total, working = g("Total"), g("Working")
    failed, stopped = g("Failed") + g("Error"), g("Stopped")
    evidence = {key: states}
    if total == 0:
        return result(check_id, "HEALTHY", evidence, "no_changefeeds")
    if failed > 0:
        return result(check_id, "WARNING", evidence, "changefeed_failed")
    if working < total or stopped > 0:
        return result(check_id, "NOTICE", evidence, "changefeed_not_all_working")
    return result(check_id, "HEALTHY", evidence, "all_changefeeds_working")


@adapter("combined_scalar_threshold")
def combined_scalar_threshold(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Peak across several scalar queries → one evidence value under ``evidence_key``,
    graded by a rule that may escalate to CRITICAL. Evidence is present on every
    non-N/A path; N/A when every query is empty. CDC-02 uses it for the worse of the
    changefeed checkpoint-ts lag and resolved-ts lag. Deep trend → o11y."""
    vals = [v for v in (ctx.instant_scalar(q) for q in params["queries"]) if v is not None]
    if not vals:
        return na(check_id, params.get("na_reason", "metric_unavailable"))
    peak = max(vals)
    key = params["evidence_key"]
    rule = params.get("rule", "none")
    if rule == "none":
        return result(check_id, "HEALTHY", {key: peak}, params.get("healthy_reason", "observation_only"))
    status, reason = apply_rule(ctx, rule, peak * float(params.get("scale", 1.0)))
    return result(check_id, status, {key: peak}, params.get("reason", reason))


def _build_tikv_loki_filter(namespace: str) -> str:
    """TiKV ERROR/FATAL log filter — mirrors the dedicated monolith's
    build_loki_filter(namespace) exactly (same include/exclude set) so the native
    error_log_count matches the collector's."""
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


def _loki_error_log_count(ctx: CollectContext, params: dict[str, Any]) -> int:
    """ERROR/FATAL TiKV log-line count over the window via Loki. Fail-closed → 0
    (matching the monolith's log_line_count on an error/empty response), so the key is
    always present and a Loki outage never fabricates a WARNING."""
    ns = ctx.namespace
    loki = getattr(ctx.api, "loki_query_range", None)
    if not ns or loki is None:
        return 0
    try:
        start_ns = int(ctx.window.start.timestamp() * 1_000_000_000)
        end_ns = int(ctx.window.end.timestamp() * 1_000_000_000)
        resp = loki(ctx.cluster_id, _build_tikv_loki_filter(ns), start_ns, end_ns,
                    int(params.get("loki_limit", 200)))
        if not isinstance(resp, dict):
            return 0
        total = 0
        for stream in ((resp.get("data") or {}).get("result") or []):
            total += len(stream.get("values") or [])
        return total
    except Exception:
        return 0


@adapter("tikv_error_signals")
def tikv_error_signals(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """TIKV-08: TiKV error signals — several PromQL counter totals over the window plus
    an ERROR/FATAL Loki log-line count (``error_log_count``, ported from the monolith's
    log scan). WARNING when any signal > 0, else HEALTHY. Evidence always carries every
    named counter key plus ``error_log_count`` (0 when a signal is absent or a query
    fails), matching the monolith's stable 5-key shape. Deep correlation → o11y.
    ``counters`` is a list of {evidence_key, query}."""
    evidence: dict[str, Any] = {}
    for m in params.get("counters", []):
        v = ctx.instant_scalar(m["query"])
        evidence[m["evidence_key"]] = float(v) if v is not None else 0.0
    evidence["error_log_count"] = _loki_error_log_count(ctx, params)
    fired = any((v or 0) > 0 for v in evidence.values())
    return result(check_id, "WARNING" if fired else "HEALTHY", evidence,
                  params.get("warn_reason", "error_signals_present") if fired
                  else params.get("healthy_reason", "no_error_signals"))


@adapter("cluster_metadata")
def cluster_metadata(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    fields = params.get("fields", [])
    md = ctx.metadata or {}
    picked = {name: md.get(name) for name in fields}
    if any(value not in (None, "") for value in picked.values()):
        return result(check_id, "HEALTHY", {"metadata": picked}, "metadata_resolved")
    return na(check_id, "metadata_unavailable", {"metadata": picked})


# ── scope / availability / storage adapters (logical) ────────

@adapter("scope_guard")
def scope_guard(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Tenant-safe scope assertion for Shared Plans: no physical infrastructure
    is queried (SCOPE-* checks)."""
    return result(
        check_id,
        "HEALTHY",
        {
            "cluster_id": ctx.cluster_id,
            "plan": ctx.plan or (ctx.metadata or {}).get("plan"),
            "logical_cluster_only": True,
            "physical_infrastructure_queried": False,
        },
        "scope_guard_passed",
    )


@adapter("connections_headroom")
def connections_headroom(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Connection count (Total). The count itself is a valid observation (HEALTHY);
    headroom-vs-limit is a separate judgment not made here (no configured-limit
    source). N/A only when the series is empty."""
    series, _ = ctx.range_series_resilient(params["query"])
    stats = distribution([v for _, v in series])
    evidence = {"query": ctx.expand(params["query"]), "statistics": stats, "unit": params.get("unit", "connections")}
    if not stats:
        return na(check_id, "connection_metric_empty", evidence)
    if params.get("retain_series") and series:
        evidence["series"] = downsample_series(series)
    return result(check_id, "HEALTHY", evidence, "observation_only")


@adapter("logical_storage_usage")
def logical_storage_usage(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Logical/database storage usage over the window: start, end, growth, growth %.

    Driven by a recipe-supplied logical-storage ``query`` (never physical store
    capacity). Fail-closed N/A when no query is configured or the series is empty.
    The same evidence serves LST-01 (usage), LST-02 (absolute increase) and
    LST-03 (growth rate); the per-run query cache de-duplicates the series.
    """
    query = params.get("query")
    if not query:
        return na(check_id, "logical_storage_metric_not_configured")
    values, label_absent = ctx.range_values_resilient(query)
    if not values:
        return na(check_id, "logical_storage_metric_empty", {"query": ctx.expand(query)})
    start, end = float(values[0]), float(values[-1])
    growth = end - start
    growth_percent = (growth / start * 100.0) if start else None
    unit = params.get("unit", "bytes")
    evidence = {
        "query": ctx.expand(query if not label_absent else strip_cluster_id_filter(ctx.expand(query))),
        "start": start,
        "end": end,
        "growth": growth,
        "growth_percent": growth_percent,
        "unit": unit,
        "metric_semantics": "product-exposed logical storage (not physical store capacity)",
    }
    if label_absent:
        # This cluster's metrics carry no tidb_cluster_id label; recovered via the
        # unscoped fallback (API path is already cluster-scoped).
        evidence["cluster_id_label_absent"] = True
    return result(check_id, "HEALTHY", evidence, "observation_only")


def _window_dates(window: TimeWindow) -> list[str]:
    """YYYYMMDD partitions spanned by the window (UTC), inclusive."""
    from datetime import timedelta

    dates: list[str] = []
    day = window.start.date()
    last = window.end.date()
    while day <= last:
        dates.append(day.strftime("%Y%m%d"))
        day += timedelta(days=1)
    return dates


@adapter("slow_log_count")
def slow_log_count(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Slow-log aggregate count over the window via Data Proxy SQL (SQL-06).

    Counts rows in ``clinic_data_proxy.slow_query_logs`` across the window's date
    partitions (one IN-clause query). Not business request volume. Fail-closed N/A
    when the source is unavailable. Validated live against a Premium cluster.
    """
    dates = _window_dates(ctx.window)
    if not dates:
        return na(check_id, "slow_log_no_window_dates")
    table = params.get("table", "clinic_data_proxy.slow_query_logs")
    in_list = ", ".join(f"'{d}'" for d in dates)
    # Total rows + distinct digests (unique slow SQL) in one query.
    sql = f"SELECT count(*) AS c, count(DISTINCT digest) AS u FROM {table} WHERE date IN ({in_list})"
    proxy_exc: str | None = None
    try:
        resp = ctx.api.query_sql(ctx.cluster_id, sql)
    except Exception as exc:  # source/network error → try NGM, never crash
        resp, proxy_exc = None, str(exc)
    rows = resp.get("rows") if isinstance(resp, dict) else None
    if rows and rows[0]:
        try:
            total = int(float(rows[0][0]))
        except (TypeError, ValueError, IndexError):
            return na(check_id, "slow_log_count_unparseable", {"response": str(resp)[:200]})
        unique = None
        try:
            unique = int(float(rows[0][1]))
        except (TypeError, ValueError, IndexError):
            unique = None
        duration = ctx.window.duration_seconds
        evidence = {
            "window_total": total,
            "unique_digests": unique,
            "per_second": (total / duration) if duration else None,
            "dates": dates,
            "unit": params.get("unit", "rows"),
            "source": table,
            "metric_semantics": "slow-log entry count + distinct digests; cross-check with QPS, not request volume",
        }
        return result(check_id, "HEALTHY", evidence, "observation_only")
    # Data Proxy unavailable (no slow_query_logs table on this plan, e.g. Essential).
    # Fall back to NGM bounded sampling — the same source the Top-N uses.
    resp_error = str(resp.get("error")) if (isinstance(resp, dict) and resp.get("error")) else None
    ngm = _slow_log_via_ngm(ctx, check_id, params)
    if ngm is not None:
        return ngm
    if proxy_exc:
        return na(check_id, "slow_log_source_error", {"error": proxy_exc, "dates": dates})
    if resp_error:
        return na(check_id, "slow_log_query_error", {"dates": dates, "response": str(resp)[:200]})
    return na(check_id, "slow_log_source_unavailable", {"dates": dates, "response": str(resp)[:200]})


def _slow_log_via_ngm(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult | None:
    """Slow-log volume via NGM bounded sampling for clusters without a Data Proxy
    ``slow_query_logs`` table (Essential/shared). NGM returns up to ``ngm_limit``
    of the slowest entries, so the count is a bounded sample (a lower bound when
    capped), not an exhaustive total — labelled as such. Returns None on failure."""
    begin = int(ctx.window.start.timestamp())
    end = int(ctx.window.end.timestamp())
    limit = int(params.get("ngm_limit", 2000))
    try:
        resp = ctx.api.ngm_slow_query_list(
            ctx.cluster_id, begin, end, limit=limit,
            fields="digest,query_time,timestamp", show_internal=False)
    except Exception:
        return None
    rows = _topn_rows(resp)
    if not rows:
        return None
    digests = {str(r.get("digest")) for r in rows if r.get("digest")}
    capped = len(rows) >= limit
    evidence = {
        "window_total": len(rows),
        "unique_digests": len(digests) or None,
        "sampled": True,
        "sample_capped": capped,
        "ngm_limit": limit,
        "unit": params.get("unit", "rows"),
        "source": "ngm_slow_query_list",
        "metric_semantics": "NGM bounded sample of slowest queries (not exhaustive); window_total is a lower bound when sample_capped",
    }
    return result(check_id, "HEALTHY", evidence, "observation_only")


def _topn_rows(payload: Any) -> list[dict[str, Any]]:
    """Defensively extract a list of ranked rows from an API payload whose exact
    envelope varies (items / data / list / result, or a bare list)."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "list", "result", "rows", "top"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                nested = _topn_rows(value)
                if nested:
                    return nested
    return []


def _aggregate_slow_by_digest(raw_rows: list[dict[str, Any]], mode: str, top: int) -> list[dict[str, Any]]:
    """Group individual slow-query rows by digest → per-digest count + total query
    time, ranked for slow_count (by count) or slow_duration (by total time)."""
    agg: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        digest = row.get("digest") or row.get("sql_digest")
        if not digest:
            continue
        query_time = 0.0
        try:
            query_time = float(row.get("query_time") or 0.0)
        except (TypeError, ValueError):
            query_time = 0.0
        bucket = agg.setdefault(str(digest), {"digest": str(digest), "count": 0, "total_query_time": 0.0})
        bucket["count"] += 1
        bucket["total_query_time"] += query_time
    key = "count" if mode == "slow_count" else "total_query_time"
    return sorted(agg.values(), key=lambda b: b[key], reverse=True)[:top]


@adapter("sql_topn")
def sql_topn(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Owner-facing SQL Top-N ranking for the shared-plan Health report.

    Audience is the cluster owner (the customer's own SQL), so the ranking — with
    its normalized digest/text — is allowed in the customer report. ``mode`` picks
    the source: ``ru`` (top_ru_summary), ``slow_count`` / ``slow_duration``
    (ngm_slow_query_list aggregated by digest). Ranking is diagnostic evidence, not
    a Health status, so a non-empty result is HEALTHY; an empty/absent source is
    N/A (never a fabricated ranking).
    """
    mode = params.get("mode")
    top = int(params.get("top", 10))
    start_epoch = int(ctx.window.start.timestamp())
    end_epoch = int(ctx.window.end.timestamp())
    try:
        if mode == "ru":
            payload = ctx.api.top_ru_summary(ctx.cluster_id, start_epoch, end_epoch, top)
            rows = _topn_rows(payload)
        elif mode in ("slow_count", "slow_duration"):
            # ngm_slow_query_list returns individual slow queries, not per-digest
            # aggregates, so aggregate client-side by digest: occurrence count for
            # slow_count, summed query_time for slow_duration (validated live).
            sample_limit = int(params.get("sample_limit", 1000))
            payload = ctx.api.ngm_slow_query_list(
                ctx.cluster_id, start_epoch, end_epoch, limit=sample_limit, order_by="query_time"
            )
            rows = _aggregate_slow_by_digest(_topn_rows(payload), mode, top)
        else:
            return na(check_id, "sql_topn_unknown_mode", {"mode": mode})
    except Exception as exc:  # source/network/shape error → N/A, never crash the run
        return na(check_id, "sql_topn_source_error", {"mode": mode, "error": str(exc)})
    if not rows:
        return na(check_id, "sql_topn_empty", {"mode": mode})
    evidence = {
        "mode": mode,
        "top": top,
        "rows": rows[:top],
        "audience": "owner",
        "metric_semantics": "owner-scoped SQL ranking; diagnostic, not a Health status",
    }
    return result(check_id, "HEALTHY", evidence, "owner_scoped_ranking")


@adapter("availability_status")
def availability_status(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Product availability/status. Fail-closed N/A until the product status
    source is bound."""
    return na(check_id, "availability_status_source_not_bound")


# ── derived adapters (second pass; read sibling results) ─────

@adapter("worst_of", derived=True)
def worst_of(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Summarize the worst status across named sibling checks (e.g. SQL-10 over
    SQL-02/03/05). N/A members are ignored; all-N/A yields N/A."""
    of = list(params.get("of", []))
    member_statuses = {cid: ctx.collected.get(cid, {}).get("status", "N/A") for cid in of}
    ranked = [s for s in member_statuses.values() if s in STATUS_RANK]
    worst = max(ranked, key=STATUS_RANK.__getitem__) if ranked else "N/A"
    return result(
        check_id,
        worst,
        {"summary_status": worst, "of": of, "member_statuses": member_statuses, "sql_top_n_collected": False},
        "deterministic_summary",
    )


# ── named adapters (fully ported) ────────────────────────────

@adapter("ru_consumption")
def ru_consumption(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """RU rate distribution + exact window total + legacy-metric cross-check (RCU-01)."""
    rate_query = params["rate_query"]
    total_query = params["window_total_query"]
    cross_query = params.get("cross_check_query")
    rate_series, _ = ctx.range_series_resilient(rate_query)
    total = ctx.instant_scalar(total_query)
    ru_source = "tidb_ruv2"
    # Fallback for clusters that don't emit the RU v2 counter (e.g. Essential):
    # resource_manager read+write RU units. The API path is already cluster-scoped.
    if not rate_series and params.get("fallback_rate_query"):
        fb_series, _ = ctx.range_series_resilient(params["fallback_rate_query"])
        if fb_series:
            rate_series, ru_source = fb_series, "resource_manager"
            rate_query = params["fallback_rate_query"]
            if total is None and params.get("fallback_total_query"):
                total = ctx.instant_scalar(params["fallback_total_query"])
                total_query = params["fallback_total_query"]
    rate = distribution([v for _, v in rate_series])
    cross = ctx.instant_scalar(cross_query) if (cross_query and ru_source == "tidb_ruv2") else None
    delta = None
    if total is not None and cross not in (None, 0.0):
        delta = abs(total - cross) / abs(cross) * 100
    evidence = {
        "query": ctx.expand(rate_query),
        "statistics_ru_per_second": rate,
        "window_total_query": ctx.expand(total_query),
        "window_total_ru": total,
        "cross_check_window_total_ru": cross,
        "cross_check_delta_percent": delta,
        "ru_metric_source": ru_source,
        "unit": params.get("unit", "RU/s"),
        "metric_semantics": ("tenant-scoped RU v2 consumption counter" if ru_source == "tidb_ruv2"
                             else "resource_manager RU units (read+write); used when the RU v2 counter is absent"),
    }
    if params.get("retain_series") and rate_series:
        evidence["series"] = downsample_series(rate_series)
    if rate is not None and total is not None:
        return result(check_id, "HEALTHY", evidence, "valid_window_evidence")
    if rate is not None and ru_source == "resource_manager":
        # Fallback source: rate present is a valid observation even without an exact
        # window total (the RU v2 cross-check does not apply to this metric).
        return result(check_id, "HEALTHY", evidence, "observation_only")
    return na(check_id, "ru_metric_discovery_incomplete", evidence)


# ── named adapters (stubs — port dedicated logic here) ───────

@adapter("tikv_thread_pool_pressure")
def tikv_thread_pool_pressure(ctx: CollectContext, check_id: str, params: dict[str, Any]) -> CheckResult:
    """Per-instance, per-thread-group TiKV CPU pressure.

    TODO(Phase 2): port the thread-pool grouping and sustained-window rule from
    dedicated_health_inspection.py into this adapter.
    """
    return na(check_id, "adapter_not_implemented", {"params": params})


# ── recipe loading, cross-validation, execution ──────────────

def load_recipes(path: str | Path = DEFAULT_RECIPES) -> dict[str, dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "1.0.0":
        raise ValueError("collection recipes: unsupported or missing schema_version")
    recipes = value.get("recipes")
    if not isinstance(recipes, dict):
        raise ValueError("collection recipes: 'recipes' must be an object")
    return recipes


def validate_recipes(
    registry: dict[str, Any],
    recipes: dict[str, dict[str, Any]],
    rules: dict[str, Any] | None = None,
) -> list[str]:
    """Cross-check the sources of truth. Returns a list of human-readable errors
    (empty when consistent). When ``rules`` is given, also checks that every rule
    a recipe references exists."""
    errors: list[str] = []
    registry_ids = {c["id"] for c in registry.get("checks", [])}
    implemented = {
        c["id"] for c in registry.get("checks", [])
        if (c.get("implementation") or {}).get("status") == "implemented"
    }
    for check_id, recipe in recipes.items():
        if check_id not in registry_ids:
            errors.append(f"recipe references unknown check_id: {check_id}")
        if recipe.get("adapter") not in ADAPTERS:
            errors.append(f"{check_id}: unknown adapter {recipe.get('adapter')!r}")
        rule_name = (recipe.get("params") or {}).get("rule")
        if rules is not None and rule_name and rule_name != "none" and rule_name not in rules:
            errors.append(f"{check_id}: rule {rule_name!r} not defined in rules registry")
        # Derived adapters (e.g. worst_of) must reference collected, non-derived members.
        if recipe.get("adapter") in DERIVED_ADAPTERS:
            for member in (recipe.get("params") or {}).get("of", []):
                if member not in recipes:
                    errors.append(f"{check_id}: derived member {member} has no recipe")
                elif recipes[member].get("adapter") in DERIVED_ADAPTERS:
                    errors.append(f"{check_id}: derived member {member} is itself derived (unsupported)")
    for check_id in sorted(implemented - set(recipes)):
        errors.append(f"check {check_id} is 'implemented' but has no collection recipe")
    return errors


def validate_rules(rules: dict[str, Any]) -> list[str]:
    """Offline validation of threshold rules: known kind, numeric bands, and band
    monotonicity (high: notice<=warning<=critical; low: notice>=warning>=critical)."""
    errors: list[str] = []
    for name, spec in (rules or {}).items():
        if not isinstance(spec, dict):
            errors.append(f"rule {name}: must be an object")
            continue
        kind = spec.get("kind", "high")
        if kind == "params":
            continue
        if kind not in ("high", "low"):
            errors.append(f"rule {name}: unknown kind {kind!r}")
            continue
        if "warning" not in spec:
            errors.append(f"rule {name}: 'warning' is required for a {kind} rule")
        for key in ("notice", "warning", "critical"):
            if key in spec and not isinstance(spec[key], (int, float)):
                errors.append(f"rule {name}: {key} must be numeric")
        present = [spec[k] for k in ("notice", "warning", "critical") if isinstance(spec.get(k), (int, float))]
        if len(present) >= 2:
            ascending = all(present[i] <= present[i + 1] for i in range(len(present) - 1))
            descending = all(present[i] >= present[i + 1] for i in range(len(present) - 1))
            if kind == "high" and not ascending:
                errors.append(f"rule {name}: high bands must be notice<=warning<=critical")
            if kind == "low" and not descending:
                errors.append(f"rule {name}: low bands must be notice>=warning>=critical")
    return errors


def _run_one(ctx: CollectContext, check_id: str, recipe: dict[str, Any]) -> CheckResult:
    fn = ADAPTERS.get(recipe["adapter"])
    if fn is None:
        return na(check_id, "adapter_not_registered", {"adapter": recipe["adapter"]})
    try:
        return fn(ctx, check_id, recipe.get("params", {}))
    except Exception as exc:  # fail-closed: never let one adapter abort the run
        return na(check_id, "adapter_error", {"error": str(exc)})


def collect(ctx: CollectContext, selection: dict[str, Any], recipes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Run recipes for exactly the registry-selected runnable checks.

    Two passes: ordinary adapters first, then derived adapters (which read sibling
    results via ctx.collected). Output preserves selection order.
    """
    order = list(selection.get("runnable_check_ids", []))
    ctx.collected = {}
    deferred: list[str] = []

    for check_id in order:
        recipe = recipes.get(check_id)
        if recipe is None:
            ctx.collected[check_id] = na(check_id, "no_collection_recipe")
            continue
        if recipe.get("adapter") in DERIVED_ADAPTERS:
            deferred.append(check_id)
            continue
        ctx.collected[check_id] = _run_one(ctx, check_id, recipe)

    for check_id in deferred:
        ctx.collected[check_id] = _run_one(ctx, check_id, recipes[check_id])

    results = [ctx.collected[check_id] for check_id in order]
    return {
        "cluster_id": ctx.cluster_id,
        "cluster_name": (ctx.metadata or {}).get("name") or ctx.cluster_id,
        "metadata": ctx.metadata,
        "plan": ctx.plan,
        "window": ctx.window.evidence(),
        "check_results": results,
        "query_errors": ctx.errors,
    }


def main() -> int:
    import argparse
    import sys

    from checklist_registry import load_registry

    parser = argparse.ArgumentParser(description="Validate collection recipes against the registry")
    parser.add_argument("command", choices=["validate"])
    parser.add_argument("--registry")
    parser.add_argument("--recipes", default=str(DEFAULT_RECIPES))
    args = parser.parse_args()

    registry = load_registry(args.registry) if args.registry else load_registry()
    recipes = load_recipes(args.recipes)
    rules = load_rules()
    errors = validate_recipes(registry, recipes, rules or None) + validate_rules(rules)
    if errors:
        for item in errors:
            print(f"ERROR: {item}", file=sys.stderr)
        return 2
    print(json.dumps(
        {"valid": True, "recipes": len(recipes), "rules": len(rules), "adapters": sorted(ADAPTERS)},
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
