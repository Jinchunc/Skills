"""Unit tests for the selection-driven collection layer."""
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import collection_adapters as ca
from checklist_registry import SelectionRequest, load_registry, select_checks
from inspection_core import TimeWindow


def rng(values):
    return {"status": "success", "data": {"result": [{"metric": {}, "values": [[0, str(v)] for v in values]}]}}


def scalar(value):
    return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, str(value)]}]}}


def vector(by_instance, label="instance"):
    return {"status": "success", "data": {"result": [
        {"metric": {label: k}, "value": [0, str(v)]} for k, v in by_instance.items()]}}


class _VectorAPI:
    def __init__(self, inst):
        self.inst = inst

    def query_instant(self, cid, q, t):
        return vector(self.inst) if self.inst else {"status": "success", "data": {"result": []}}

    def query_range(self, *a, **k):
        return {"status": "success", "data": {"result": []}}


class RecordingAPI:
    """Substring-matching fake that records every query for cache assertions."""

    def __init__(self, ranges=None, instants=None):
        self.ranges = ranges or {}
        self.instants = instants or {}
        self.range_calls = []
        self.instant_calls = []

    def query_range(self, cluster_id, query, start, end, step):
        self.range_calls.append(query)
        for key, vals in self.ranges.items():
            if key in query:
                return rng(vals)
        return {"status": "success", "data": {"result": []}}

    def query_instant(self, cluster_id, query, time):
        self.instant_calls.append(query)
        for key, val in self.instants.items():
            if key in query:
                return scalar(val)
        return {"status": "success", "data": {"result": []}}


def make_ctx(api=None, rules=None, metadata=None, plan="Premium"):
    window = TimeWindow(
        datetime(2026, 11, 20, tzinfo=timezone.utc),
        datetime(2026, 11, 21, tzinfo=timezone.utc),
        300,
    )
    return ca.CollectContext(api=api, cluster_id="C1", window=window, plan=plan, metadata=metadata or {}, rules=rules or {})


class ApplyRuleTests(unittest.TestCase):
    RULES = {
        "hi": {"kind": "high", "notice": 50, "warning": 200, "critical": 1000},
        "warnonly": {"kind": "high", "warning": 100},
        "notice_default": {"kind": "high", "warning": 100, "critical": 200},
        "lo": {"kind": "low", "notice": 30, "warning": 10, "critical": 5},
        "p": {"kind": "params", "x": 1},
        "bad": {"kind": "high"},
    }

    def ctx(self):
        return make_ctx(rules=self.RULES)

    def test_high_bands(self):
        ctx = self.ctx()
        self.assertEqual(ca.apply_rule(ctx, "hi", 10)[0], "HEALTHY")
        self.assertEqual(ca.apply_rule(ctx, "hi", 60)[0], "NOTICE")
        self.assertEqual(ca.apply_rule(ctx, "hi", 210)[0], "WARNING")
        self.assertEqual(ca.apply_rule(ctx, "hi", 2000)[0], "CRITICAL")

    def test_critical_optional_is_warn_only(self):
        ctx = self.ctx()
        self.assertEqual(ca.apply_rule(ctx, "warnonly", 50)[0], "HEALTHY")
        self.assertEqual(ca.apply_rule(ctx, "warnonly", 150)[0], "WARNING")
        self.assertEqual(ca.apply_rule(ctx, "warnonly", 10**9)[0], "WARNING")

    def test_notice_defaults_to_warning(self):
        ctx = self.ctx()
        self.assertEqual(ca.apply_rule(ctx, "notice_default", 50)[0], "HEALTHY")
        self.assertEqual(ca.apply_rule(ctx, "notice_default", 150)[0], "WARNING")
        self.assertEqual(ca.apply_rule(ctx, "notice_default", 250)[0], "CRITICAL")

    def test_low_bands(self):
        ctx = self.ctx()
        self.assertEqual(ca.apply_rule(ctx, "lo", 40)[0], "HEALTHY")
        self.assertEqual(ca.apply_rule(ctx, "lo", 20)[0], "NOTICE")
        self.assertEqual(ca.apply_rule(ctx, "lo", 8)[0], "WARNING")
        self.assertEqual(ca.apply_rule(ctx, "lo", 1)[0], "CRITICAL")

    def test_failclosed_variants(self):
        ctx = self.ctx()
        self.assertEqual(ca.apply_rule(ctx, "p", 1), ("N/A", "rule_requires_named_adapter"))
        self.assertEqual(ca.apply_rule(ctx, "bad", 1), ("N/A", "rule_malformed"))
        self.assertEqual(ca.apply_rule(ctx, "missing", 1), ("N/A", "rule_not_defined"))


class PromqlDistributionTests(unittest.TestCase):
    def test_range_with_rule_and_evidence_key(self):
        api = RecordingAPI(ranges={"dur": [0.03, 0.21, 0.18]})
        ctx = make_ctx(api=api, rules={"r": {"kind": "high", "notice": 50, "warning": 200, "critical": 1000}})
        out = ca.promql_distribution(
            ctx, "SQL-02",
            {"query": "dur", "scale": 1000, "rule_statistic": "max", "rule": "r", "evidence_key": "statistics_seconds"},
        )
        self.assertEqual(out["status"], "WARNING")
        self.assertIn("statistics_seconds", out["evidence"])
        self.assertAlmostEqual(out["evidence"]["rule_value"], 210.0)

    def test_observation_only_when_rule_none(self):
        api = RecordingAPI(ranges={"qps": [1, 2, 3]})
        out = ca.promql_distribution(make_ctx(api=api), "SQL-01", {"query": "qps", "rule": "none"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "observation_only"))

    def test_empty_series_is_na(self):
        out = ca.promql_distribution(make_ctx(api=RecordingAPI()), "SQL-01", {"query": "nothing", "rule": "none"})
        self.assertEqual((out["status"], out["reason"]), ("N/A", "empty_series"))

    def test_instant_kind(self):
        api = RecordingAPI(instants={"scal": 7})
        out = ca.promql_distribution(make_ctx(api=api), "X", {"query": "scal", "kind": "instant", "rule": "none"})
        self.assertEqual(out["status"], "HEALTHY")
        self.assertEqual(out["evidence"]["value"], 7.0)


class PromqlRatioTests(unittest.TestCase):
    def test_ratio_percent_and_estimated_count(self):
        api = RecordingAPI(ranges={"errors": [1, 2, 1], "total": [100, 120, 110]})
        ctx = make_ctx(api=api, rules={"e": {"kind": "high", "notice": 0.1, "warning": 1.0, "critical": 2.0}})
        out = ca.promql_ratio(
            ctx, "SQL-05",
            {"numerator": "errors", "denominator": "total", "scale": 100, "rule": "e", "emit_estimated_count": True},
        )
        self.assertEqual(out["status"], "WARNING")
        self.assertAlmostEqual(out["evidence"]["window_average_ratio_percent"], 1.2121, places=3)
        self.assertIn("estimated_error_count", out["evidence"])

    def test_zero_denominator_is_na(self):
        api = RecordingAPI(ranges={"errors": [1, 1], "total": [0, 0]})
        out = ca.promql_ratio(make_ctx(api=api), "SQL-05", {"numerator": "errors", "denominator": "total"})
        self.assertEqual((out["status"], out["reason"]), ("N/A", "non_comparable_or_empty"))


class SimpleAdapterTests(unittest.TestCase):
    def test_cluster_metadata(self):
        ok = ca.cluster_metadata(make_ctx(metadata={"provider": "aws"}), "OVR-01", {"fields": ["provider", "region"]})
        self.assertEqual(ok["status"], "HEALTHY")
        empty = ca.cluster_metadata(make_ctx(metadata={}), "OVR-01", {"fields": ["provider"]})
        self.assertEqual((empty["status"], empty["reason"]), ("N/A", "metadata_unavailable"))

    def test_scope_guard_declares_no_physical(self):
        out = ca.scope_guard(make_ctx(), "SCOPE-01", {})
        self.assertEqual(out["status"], "HEALTHY")
        self.assertFalse(out["evidence"]["physical_infrastructure_queried"])
        self.assertTrue(out["evidence"]["logical_cluster_only"])

    def test_connections_headroom_healthy_with_stats(self):
        api = RecordingAPI(ranges={"connections": [20, 35, 28]})
        out = ca.connections_headroom(make_ctx(api=api), "SQL-07", {"query": "sum(connections)"})
        # Observation-only signal: present series → HEALTHY with stats, not N/A.
        self.assertEqual(out["status"], "HEALTHY")
        self.assertIsNotNone(out["evidence"]["statistics"])

    def test_connections_headroom_na_when_empty(self):
        api = RecordingAPI(ranges={})
        out = ca.connections_headroom(make_ctx(api=api), "SQL-07", {"query": "sum(connections)"})
        self.assertEqual((out["status"], out["reason"]), ("N/A", "connection_metric_empty"))

    def test_ru_consumption_resource_manager_fallback(self):
        # RU v2 counter absent (Essential) → resource_manager read+write fallback.
        api = RecordingAPI(
            ranges={"resource_manager_resource_unit_read": [100.0, 120.0, 110.0]},
            instants={"increase(resource_manager_resource_unit_read": 5000.0},
        )
        params = {
            "rate_query": 'sum(rate(tidb_ruv2_ru_total{tidb_cluster_id="{cluster_id}"}[5m]))',
            "window_total_query": 'sum(increase(tidb_ruv2_ru_total{tidb_cluster_id="{cluster_id}"}[{window}]))',
            "fallback_rate_query": 'sum(rate(resource_manager_resource_unit_read_request_unit_sum[5m])) + sum(rate(resource_manager_resource_unit_write_request_unit_sum[5m]))',
            "fallback_total_query": 'sum(increase(resource_manager_resource_unit_read_request_unit_sum[{window}])) + sum(increase(resource_manager_resource_unit_write_request_unit_sum[{window}]))',
            "unit": "RU/s", "rule": "none", "retain_series": True,
        }
        out = ca.ru_consumption(make_ctx(api=api), "RCU-01", params)
        self.assertEqual(out["status"], "HEALTHY")
        self.assertEqual(out["evidence"]["ru_metric_source"], "resource_manager")
        self.assertIn("series", out["evidence"])
        self.assertEqual(out["evidence"]["window_total_ru"], 5000.0)

    def test_strip_cluster_id_filter(self):
        # Matcher in the middle, at the start, and alone.
        self.assertEqual(
            ca.strip_cluster_id_filter('x{tidb_cluster_id="C1", type="used"}'), 'x{type="used"}')
        self.assertEqual(
            ca.strip_cluster_id_filter('x{type="used", tidb_cluster_id="C1"}'), 'x{type="used"}')
        self.assertEqual(ca.strip_cluster_id_filter('x{tidb_cluster_id="C1"}'), 'x{}')
        self.assertEqual(ca.strip_cluster_id_filter('x{type="used"}'), 'x{type="used"}')  # unchanged

    def test_logical_storage_fallback_when_cluster_id_label_absent(self):
        # Scoped query (with tidb_cluster_id) returns nothing; the unscoped fallback
        # — matched here only by the label-free selector — recovers the series.
        api = RecordingAPI(ranges={'tikv_store_size_bytes{type="used"}': [100.0, 110.0]})
        q = 'sum(tikv_store_size_bytes{tidb_cluster_id="{cluster_id}", type="used"})'
        out = ca.logical_storage_usage(make_ctx(api=api), "LST-01", {"query": q})
        self.assertEqual(out["status"], "HEALTHY")
        self.assertTrue(out["evidence"]["cluster_id_label_absent"])
        self.assertEqual((out["evidence"]["start"], out["evidence"]["end"]), (100.0, 110.0))
        # Scoped tried first, then the unscoped fallback.
        self.assertTrue(any("tidb_cluster_id" in c for c in api.range_calls))
        self.assertTrue(any("tidb_cluster_id" not in c for c in api.range_calls))

    def test_logical_storage_no_fallback_when_scoped_has_data(self):
        api = RecordingAPI(ranges={"tikv_store_size_bytes": [200.0, 220.0]})
        q = 'sum(tikv_store_size_bytes{tidb_cluster_id="{cluster_id}", type="used"})'
        out = ca.logical_storage_usage(make_ctx(api=api), "LST-01", {"query": q})
        self.assertEqual(out["status"], "HEALTHY")
        self.assertNotIn("cluster_id_label_absent", out["evidence"])  # no fallback used
        self.assertEqual(len(api.range_calls), 1)  # only the scoped query ran

    def test_latency_vmrange_fallback(self):
        # by(le) empty (VictoriaMetrics-backed cluster) → by(vmrange) recovers data.
        api = RecordingAPI(ranges={"by (vmrange)": [0.01, 0.02, 0.03]})
        q = "histogram_quantile(0.99, sum(rate(x_bucket[5m])) by (le))"
        out = ca.promql_distribution(make_ctx(api=api), "SQL-02",
                                     {"query": q, "kind": "range", "unit": "s"})
        self.assertEqual(out["status"], "HEALTHY")
        self.assertEqual(out["evidence"]["series_fallback"], "vmrange")
        self.assertTrue(any("by (vmrange)" in c for c in api.range_calls))

    def test_promql_distribution_retains_series_when_flagged(self):
        api = RecordingAPI(ranges={"sum(rate(q[5m]))": [1.0, 2.0, 3.0]})
        out = ca.promql_distribution(make_ctx(api=api), "SQL-01",
                                     {"query": "sum(rate(q[5m]))", "kind": "range", "retain_series": True})
        self.assertIn("series", out["evidence"])
        self.assertEqual(len(out["evidence"]["series"]), 3)
        self.assertEqual(out["evidence"]["series"][0][1], 1.0)  # [ts, value]

    def test_promql_distribution_no_series_without_flag(self):
        api = RecordingAPI(ranges={"sum(rate(q[5m]))": [1.0, 2.0]})
        out = ca.promql_distribution(make_ctx(api=api), "SQL-01",
                                     {"query": "sum(rate(q[5m]))", "kind": "range"})
        self.assertNotIn("series", out["evidence"])

    def test_instant_vector_parses_by_instance(self):
        ctx = make_ctx(api=_VectorAPI({"tidb-0": 10.0, "tidb-1": 20.0}))
        self.assertEqual(ctx.instant_vector("q"), {"tidb-0": 10.0, "tidb-1": 20.0})
        self.assertEqual(make_ctx(api=_VectorAPI({})).instant_vector("q"), {})

    def test_per_instance_vector_observation_minimal_evidence(self):
        ctx = make_ctx(api=_VectorAPI({"tidb-0": 6.1e9}))
        out = ca.per_instance_vector(ctx, "TIDB-02", {
            "query": "q", "evidence_key": "tidb_memory_max_by_instance",
            "rule": "none", "healthy_reason": "tidb_rss_observed",
            "na_reason": "tidb_memory_metric_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "tidb_rss_observed"))
        # minimal evidence — ONLY the named map (matches the dedicated collector shape)
        self.assertEqual(sorted(out["evidence"].keys()), ["tidb_memory_max_by_instance"])

    def test_per_instance_vector_na_when_empty(self):
        ctx = make_ctx(api=_VectorAPI({}))
        out = ca.per_instance_vector(ctx, "TIDB-02", {
            "query": "q", "evidence_key": "tidb_memory_max_by_instance",
            "na_reason": "tidb_memory_metric_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("N/A", "tidb_memory_metric_unavailable"))

    def test_per_instance_vector_threshold_minimal_evidence(self):
        rules = {"tidb_cpu_pct": {"kind": "high", "warning": 85}}
        # peak 90 ≥ 85 → WARNING; evidence stays minimal (no rule/rule_value keys)
        ctx = make_ctx(api=_VectorAPI({"tidb-0": 90.0, "tidb-1": 40.0}), rules=rules)
        out = ca.per_instance_vector(ctx, "TIDB-01", {
            "query": "q", "evidence_key": "tidb_cpu_max_by_instance",
            "rule": "tidb_cpu_pct", "healthy_reason": "within_threshold",
            "na_reason": "tidb_cpu_metric_unavailable"})
        self.assertEqual(out["status"], "WARNING")
        self.assertEqual(sorted(out["evidence"].keys()), ["tidb_cpu_max_by_instance"])
        # below threshold → HEALTHY with the configured healthy_reason
        ctx2 = make_ctx(api=_VectorAPI({"tidb-0": 40.0}), rules=rules)
        out2 = ca.per_instance_vector(ctx2, "TIDB-01", {
            "query": "q", "evidence_key": "tidb_cpu_max_by_instance",
            "rule": "tidb_cpu_pct", "healthy_reason": "within_threshold"})
        self.assertEqual((out2["status"], out2["reason"]), ("HEALTHY", "within_threshold"))

    def test_multi_instance_vector_combines_maps(self):
        class MultiAPI:
            def query_instant(self, cid, q, t):
                if "leader" in q:
                    return vector({"tikv-0": 5.0})
                if "region" in q:
                    return vector({"tikv-0": 50.0})
                return {"status": "success", "data": {"result": []}}
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.multi_instance_vector(make_ctx(api=MultiAPI()), "PD-03", {
            "metrics": [
                {"evidence_key": "region_by_instance", "query": "...region..."},
                {"evidence_key": "leader_by_instance", "query": "...leader..."}],
            "healthy_reason": "region_distribution_observed",
            "na_reason": "region_metric_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "region_distribution_observed"))
        self.assertEqual(sorted(out["evidence"].keys()), ["leader_by_instance", "region_by_instance"])

    def test_multi_instance_vector_na_when_none(self):
        out = ca.multi_instance_vector(make_ctx(api=_VectorAPI({})), "PD-03", {
            "metrics": [{"evidence_key": "region_by_instance", "query": "q"}],
            "na_reason": "region_metric_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("N/A", "region_metric_unavailable"))

    def test_multi_scalar_peak_takes_max_per_metric(self):
        class API:
            def query_instant(self, cid, q, t):
                if "read" in q:
                    return vector({"tikv-0": 0.01, "tikv-1": 0.04}, label="pod")
                if "write" in q:
                    return vector({"tikv-0": 0.02}, label="pod")
                return {"status": "success", "data": {"result": []}}
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.multi_scalar_peak(make_ctx(api=API()), "INF-05", {
            "metrics": [
                {"evidence_key": "disk_read_latency_peak", "query": "...read..."},
                {"evidence_key": "disk_write_latency_peak", "query": "...write..."}],
            "healthy_reason": "disk_latency_observed", "na_reason": "disk_latency_metric_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "disk_latency_observed"))
        self.assertEqual(out["evidence"]["disk_read_latency_peak"], 0.04)  # max across pods
        self.assertEqual(sorted(out["evidence"].keys()), ["disk_read_latency_peak", "disk_write_latency_peak"])

    def test_multi_scalar_peak_warn_if_positive_fires(self):
        # TIKV-04 write-stall shape: WARN when the named peak is > 0.
        class API:
            def query_instant(self, cid, q, t):
                return vector({"i": 3.0}, label="instance")
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.multi_scalar_peak(make_ctx(api=API()), "TIKV-04", {
            "metrics": [{"evidence_key": "write_stall_peak", "query": "stall"}],
            "warn_if_positive": ["write_stall_peak"],
            "warn_reason": "write_stall_observed", "healthy_reason": "no_write_stall",
            "na_reason": "write_stall_metric_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("WARNING", "write_stall_observed"))
        self.assertEqual(out["evidence"], {"write_stall_peak": 3.0})

    def test_multi_scalar_peak_warn_if_positive_healthy_at_zero(self):
        class API:
            def query_instant(self, cid, q, t):
                return vector({"i": 0.0}, label="instance")
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.multi_scalar_peak(make_ctx(api=API()), "TIKV-04", {
            "metrics": [{"evidence_key": "write_stall_peak", "query": "stall"}],
            "warn_if_positive": ["write_stall_peak"],
            "warn_reason": "write_stall_observed", "healthy_reason": "no_write_stall"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "no_write_stall"))

    def test_multi_scalar_peak_thresholds_or_fires_on_either(self):
        # TIKV-09 shape: WARN when slow score OR raft lag crosses its rule.
        rules = {"tikv_slow_score": {"kind": "high", "warning": 100},
                 "tikv_raft_log_lag_p99": {"kind": "high", "warning": 5000}}
        class API:
            def query_instant(self, cid, q, t):
                if "slow" in q:
                    return vector({"i": 40.0}, label="instance")  # below 100
                return vector({"i": 6000.0}, label="instance")     # lag above 5000
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.multi_scalar_peak(make_ctx(api=API(), rules=rules), "TIKV-09", {
            "metrics": [{"evidence_key": "slow_score_peak", "query": "slow"},
                        {"evidence_key": "raft_log_lag_p99_peak", "query": "lag"}],
            "thresholds": [{"key": "slow_score_peak", "rule": "tikv_slow_score"},
                           {"key": "raft_log_lag_p99_peak", "rule": "tikv_raft_log_lag_p99"}],
            "warn_reason": "slow_score_or_raft_lag_high", "healthy_reason": "within_threshold"})
        self.assertEqual((out["status"], out["reason"]), ("WARNING", "slow_score_or_raft_lag_high"))
        self.assertEqual(sorted(out["evidence"].keys()), ["raft_log_lag_p99_peak", "slow_score_peak"])

    def test_multi_scalar_peak_thresholds_healthy_when_both_below(self):
        rules = {"tikv_slow_score": {"kind": "high", "warning": 100},
                 "tikv_raft_log_lag_p99": {"kind": "high", "warning": 5000}}
        class API:
            def query_instant(self, cid, q, t):
                if "slow" in q:
                    return vector({"i": 40.0}, label="instance")
                return vector({"i": 120.0}, label="instance")
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.multi_scalar_peak(make_ctx(api=API(), rules=rules), "TIKV-09", {
            "metrics": [{"evidence_key": "slow_score_peak", "query": "slow"},
                        {"evidence_key": "raft_log_lag_p99_peak", "query": "lag"}],
            "thresholds": [{"key": "slow_score_peak", "rule": "tikv_slow_score"},
                           {"key": "raft_log_lag_p99_peak", "rule": "tikv_raft_log_lag_p99"}],
            "warn_reason": "slow_score_or_raft_lag_high", "healthy_reason": "within_threshold"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "within_threshold"))

    def test_disk_iops_check_threshold_and_exceed(self):
        class API:
            def query_instant(self, cid, q, t):
                return vector({"tikv-0": 1200.0, "tikv-1": 800.0}, label="pod")
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
            def query_exceeded_iops_by_pod(self, cid, comp, s, e, step="300", rate_window="2m"):
                return {"status": "success", "data": {"result": [
                    {"metric": {"pod": "tikv-0"}, "values": [[0, "0.0"], [1, "2.5"]]}]}}
        out = ca.disk_iops_check(make_ctx(api=API(), rules={"inf_iops_exceeded": {"kind": "high", "warning": 1.0}}),
                                 "INF-04", {"iops_query": "iops", "rule": "inf_iops_exceeded",
                                            "healthy_reason": "within_threshold"})
        self.assertEqual(out["status"], "WARNING")  # exceed peak 2.5 ≥ 1.0
        self.assertEqual(out["evidence"]["disk_iops_peak"], 1200.0)
        self.assertEqual(out["evidence"]["iops_exceed_peak"], 2.5)

    def test_scalar_peak_probe_healthy_has_empty_evidence(self):
        # TIKV-06/07 shape: HEALTHY carries NO evidence (deep judgment → o11y).
        rules = {"tikv_compaction_pending_peak": {"kind": "high", "warning": 549755813888}}
        class API:
            def query_instant(self, cid, q, t):
                return vector({"i": 1000.0}, label="instance")  # far below 512 GiB
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.scalar_peak_probe(make_ctx(api=API(), rules=rules), "TIKV-06", {
            "query": "compaction", "evidence_key": "compaction_pending_peak",
            "rule": "tikv_compaction_pending_peak", "warn_reason": "compaction_pending_high",
            "healthy_reason": "within_threshold", "na_reason": "compaction_metric_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "within_threshold"))
        self.assertEqual(out["evidence"], {})

    def test_scalar_peak_probe_warns_with_evidence_on_breach(self):
        rules = {"tikv_compaction_pending_peak": {"kind": "high", "warning": 500.0}}
        class API:
            def query_instant(self, cid, q, t):
                return vector({"i": 800.0}, label="instance")
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.scalar_peak_probe(make_ctx(api=API(), rules=rules), "TIKV-06", {
            "query": "compaction", "evidence_key": "compaction_pending_peak",
            "rule": "tikv_compaction_pending_peak", "warn_reason": "compaction_pending_high",
            "healthy_reason": "within_threshold"})
        self.assertEqual((out["status"], out["reason"]), ("WARNING", "compaction_pending_high"))
        self.assertEqual(out["evidence"], {"compaction_pending_peak": 800.0})

    def test_scalar_peak_probe_na_when_absent(self):
        class API:
            def query_instant(self, cid, q, t):
                return {"status": "success", "data": {"result": []}}
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.scalar_peak_probe(make_ctx(api=API()), "TIKV-07", {
            "query": "readpool", "evidence_key": "unified_read_running_tasks_peak",
            "rule": "none", "na_reason": "unified_read_pool_metric_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("N/A", "unified_read_pool_metric_unavailable"))

    def test_product_status_check_healthy_warn_na(self):
        healthy = ca.product_status_check(make_ctx(metadata={"status": "AVAILABLE"}), "AVL-01",
            {"field": "status", "healthy_reason": "product_available", "warn_reason": "product_status_abnormal"})
        self.assertEqual((healthy["status"], healthy["reason"], healthy["evidence"]),
                         ("HEALTHY", "product_available", {"status": "AVAILABLE"}))
        warn = ca.product_status_check(make_ctx(metadata={"status": "PAUSED"}), "AVL-01",
            {"field": "status", "warn_reason": "product_status_abnormal"})
        self.assertEqual((warn["status"], warn["reason"]), ("WARNING", "product_status_abnormal"))
        na_ = ca.product_status_check(make_ctx(metadata={}), "AVL-01",
            {"field": "status", "na_reason": "status_unavailable"})
        self.assertEqual((na_["status"], na_["reason"]), ("N/A", "status_unavailable"))

    def test_instance_count_check_healthy_and_single_point(self):
        class API3:
            def query_instant(self, cid, q, t):
                return vector({"i-0": 8.0, "i-1": 8.0, "i-2": 8.0}, label="instance")
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out = ca.instance_count_check(make_ctx(api=API3()), "AVL-08",
            {"query": "q", "evidence_key": "tikv_instance_count", "min_count": 3,
             "healthy_reason": "redundant_topology", "warn_reason": "single_point_risk"})
        self.assertEqual((out["status"], out["reason"], out["evidence"]),
                         ("HEALTHY", "redundant_topology", {"tikv_instance_count": 3.0}))
        class API1:
            def query_instant(self, cid, q, t):
                return vector({"i-0": 8.0}, label="instance")
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
        out2 = ca.instance_count_check(make_ctx(api=API1()), "AVL-08",
            {"query": "q", "evidence_key": "tikv_instance_count", "min_count": 3,
             "healthy_reason": "redundant_topology", "warn_reason": "single_point_risk"})
        self.assertEqual((out2["status"], out2["reason"]), ("WARNING", "single_point_risk"))

    def test_disk_iops_check_healthy_when_below(self):
        class API:
            def query_instant(self, cid, q, t):
                return vector({"tikv-0": 500.0}, label="pod")
            def query_range(self, *a, **k):
                return {"status": "success", "data": {"result": []}}
            def query_exceeded_iops_by_pod(self, *a, **k):
                return {"status": "success", "data": {"result": [
                    {"metric": {"pod": "tikv-0"}, "values": [[0, "0.0"]]}]}}
        out = ca.disk_iops_check(make_ctx(api=API(), rules={"inf_iops_exceeded": {"kind": "high", "warning": 1.0}}),
                                 "INF-04", {"iops_query": "iops", "rule": "inf_iops_exceeded",
                                            "healthy_reason": "within_threshold"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "within_threshold"))
        self.assertEqual(sorted(out["evidence"].keys()), ["disk_iops_peak", "iops_exceed_peak"])

    def test_metadata_fields_flat_evidence(self):
        ctx = make_ctx(metadata={"provider": "aws", "region": "us-east-1",
                                 "version": "v8.5.4", "status": "active"})
        out = ca.metadata_fields(ctx, "OVR-01", {
            "fields": ["provider", "region", "version", "status"],
            "healthy_reason": "metadata_resolved", "na_reason": "metadata_unavailable"})
        self.assertEqual((out["status"], out["reason"]), ("HEALTHY", "metadata_resolved"))
        # flat keys, not nested under "metadata"
        self.assertEqual(sorted(out["evidence"].keys()), ["provider", "region", "status", "version"])
        na = ca.metadata_fields(make_ctx(metadata={}), "OVR-02", {
            "fields": ["status"], "na_reason": "state_unavailable"})
        self.assertEqual((na["status"], na["reason"]), ("N/A", "state_unavailable"))

    def test_expand_substitutions(self):
        ctx = make_ctx()
        ctx.namespace = "tidbC1"
        ctx.substitutions = {"tikv_disk_device_regex": "nvme.*"}
        q = ctx.expand('x{namespace="{namespace}", device=~"{tikv_disk_device_regex}"}')
        self.assertEqual(q, 'x{namespace="tidbC1", device=~"nvme.*"}')

    def test_downsample_series_caps_points(self):
        src = [(float(i), float(i)) for i in range(1000)]
        out = ca.downsample_series(src, max_points=100)
        self.assertLessEqual(len(out), 100)
        self.assertEqual(out[0], [0.0, 0.0])

    def test_ru_consumption_present_and_missing(self):
        api = RecordingAPI(
            ranges={"rate(tidb_ruv2_ru_total": [5, 7, 6]},
            instants={"increase(tidb_ruv2_ru_total": 123456.0, "resource_unit_request_unit_v2_sum": 123000.0},
        )
        params = {
            "rate_query": "sum(rate(tidb_ruv2_ru_total{tidb_cluster_id=\"{cluster_id}\"}[5m]))",
            "window_total_query": "sum(increase(tidb_ruv2_ru_total{tidb_cluster_id=\"{cluster_id}\"}[{window}]))",
            "cross_check_query": "sum(increase(resource_manager_resource_unit_request_unit_v2_sum{tidb_cluster_id=\"{cluster_id}\"}[{window}]))",
        }
        out = ca.ru_consumption(make_ctx(api=api), "RCU-01", params)
        self.assertEqual(out["status"], "HEALTHY")
        self.assertEqual(out["evidence"]["window_total_ru"], 123456.0)
        self.assertIsNotNone(out["evidence"]["cross_check_delta_percent"])

        missing = ca.ru_consumption(make_ctx(api=RecordingAPI(ranges={"rate(tidb_ruv2_ru_total": [5]})), "RCU-01", params)
        self.assertEqual((missing["status"], missing["reason"]), ("N/A", "ru_metric_discovery_incomplete"))


class CollectTests(unittest.TestCase):
    def test_order_missing_error_and_derived(self):
        api = RecordingAPI(ranges={"qps": [1, 2, 3], "dur": [0.03, 0.21, 0.18]})
        ctx = make_ctx(api=api, rules={"r": {"kind": "high", "notice": 50, "warning": 200, "critical": 1000}})
        recipes = {
            "SQL-01": {"adapter": "promql_distribution", "params": {"query": "qps", "rule": "none"}},
            "SQL-02": {"adapter": "promql_distribution",
                        "params": {"query": "dur", "scale": 1000, "rule_statistic": "max", "rule": "r"}},
            "SQL-10": {"adapter": "worst_of", "params": {"of": ["SQL-01", "SQL-02"]}},
            "BAD": {"adapter": "promql_distribution", "params": {}},  # missing query -> adapter_error
        }
        selection = {"runnable_check_ids": ["SQL-01", "SQL-02", "SQL-10", "BAD", "GHOST"]}
        out = ca.collect(ctx, selection, recipes)
        ids = [r["check_id"] for r in out["check_results"]]
        self.assertEqual(ids, ["SQL-01", "SQL-02", "SQL-10", "BAD", "GHOST"])  # selection order preserved
        by_id = {r["check_id"]: r for r in out["check_results"]}
        self.assertEqual(by_id["SQL-10"]["status"], "WARNING")  # derived worst-of ran after siblings
        self.assertEqual(by_id["BAD"]["reason"], "adapter_error")
        self.assertEqual(by_id["GHOST"]["reason"], "no_collection_recipe")

    def test_query_cache_dedups(self):
        api = RecordingAPI(ranges={"shared": [1, 2, 3]})
        recipes = {
            "A": {"adapter": "promql_distribution", "params": {"query": "shared", "rule": "none"}},
            "B": {"adapter": "promql_distribution", "params": {"query": "shared", "rule": "none"}},
        }
        ca.collect(make_ctx(api=api), {"runnable_check_ids": ["A", "B"]}, recipes)
        self.assertEqual(len(api.range_calls), 1)  # one distinct query, called once

    def test_prohibited_checks_are_never_collected(self):
        api = RecordingAPI(ranges={"qps": [1, 2, 3]})
        recipes = {
            "SQL-01": {"adapter": "promql_distribution", "params": {"query": "qps", "rule": "none"}},
            "TIKV-01": {"adapter": "tikv_thread_pool_pressure", "params": {}},
        }
        out = ca.collect(make_ctx(api=api), {"runnable_check_ids": ["SQL-01"]}, recipes)
        produced = {r["check_id"] for r in out["check_results"]}
        self.assertEqual(produced, {"SQL-01"})  # TIKV-01 not in runnable -> never touched

    def test_worst_of_all_na(self):
        recipes = {
            "A": {"adapter": "logical_storage_usage", "params": {}},
            "S": {"adapter": "worst_of", "params": {"of": ["A"]}},
        }
        out = ca.collect(make_ctx(), {"runnable_check_ids": ["A", "S"]}, recipes)
        by_id = {r["check_id"]: r for r in out["check_results"]}
        self.assertEqual(by_id["S"]["status"], "N/A")


class InstantFiniteTests(unittest.TestCase):
    def test_instant_scalar_rejects_nan(self):
        ctx = make_ctx(api=RecordingAPI(instants={"q": float("nan")}))
        self.assertIsNone(ctx.instant_scalar("q"))

    def test_instant_scalar_rejects_inf(self):
        ctx = make_ctx(api=RecordingAPI(instants={"q": float("inf")}))
        self.assertIsNone(ctx.instant_scalar("q"))

    def test_ru_consumption_nan_total_is_na(self):
        api = RecordingAPI(ranges={"rate(": [5, 6]}, instants={"increase(": float("nan")})
        params = {"rate_query": "sum(rate(x[5m]))", "window_total_query": "sum(increase(x[1d]))"}
        out = ca.ru_consumption(make_ctx(api=api), "RCU-01", params)
        self.assertEqual((out["status"], out["reason"]), ("N/A", "ru_metric_discovery_incomplete"))


class ValidateRecipesTests(unittest.TestCase):
    def test_detects_inconsistencies(self):
        registry = {"checks": [
            {"id": "SQL-01", "implementation": {"status": "implemented"}},
            {"id": "LST-01", "implementation": {"status": "implemented"}},
        ]}
        recipes = {
            "SQL-01": {"adapter": "promql_distribution", "params": {"rule": "missing_rule"}},
            "ZZ-99": {"adapter": "promql_distribution", "params": {}},
            "BADAD": {"adapter": "nope", "params": {}},
        }
        errors = ca.validate_recipes(registry, recipes, rules={})
        joined = " | ".join(errors)
        self.assertIn("unknown check_id: ZZ-99", joined)
        self.assertIn("unknown adapter", joined)
        self.assertIn("rule 'missing_rule' not defined", joined)
        self.assertIn("LST-01 is 'implemented' but has no collection recipe", joined)

    def test_shipped_config_is_consistent(self):
        registry = load_registry()
        errors = ca.validate_recipes(registry, ca.load_recipes(), ca.load_rules())
        self.assertEqual(errors, [])


class ValidateRulesTests(unittest.TestCase):
    def test_detects_bad_rules(self):
        errors = ca.validate_rules({
            "a": {"kind": "high", "notice": 200, "warning": 100, "critical": 50},
            "b": {"kind": "low", "notice": 5, "warning": 10},
            "c": {"kind": "high"},
            "d": {"kind": "high", "warning": "x"},
            "e": {"kind": "weird"},
        })
        joined = " | ".join(errors)
        self.assertIn("a: high bands", joined)
        self.assertIn("b: low bands", joined)
        self.assertIn("c: 'warning' is required", joined)
        self.assertIn("d: warning must be numeric", joined)
        self.assertIn("e: unknown kind", joined)

    def test_shipped_rules_are_valid(self):
        self.assertEqual(ca.validate_rules(ca.load_rules()), [])


class DerivedMemberValidationTests(unittest.TestCase):
    def test_worst_of_members_must_be_present_and_nonderived(self):
        registry = {"checks": [{"id": "SQL-10", "implementation": {"status": "planned"}},
                               {"id": "SQL-02", "implementation": {"status": "planned"}},
                               {"id": "SQL-11", "implementation": {"status": "planned"}}]}
        recipes = {
            "SQL-10": {"adapter": "worst_of", "params": {"of": ["SQL-02", "GHOST-01", "SQL-11"]}},
            "SQL-02": {"adapter": "promql_distribution", "params": {"query": "x", "rule": "none"}},
            "SQL-11": {"adapter": "worst_of", "params": {"of": []}},
        }
        joined = " | ".join(ca.validate_recipes(registry, recipes, rules={}))
        self.assertIn("derived member GHOST-01 has no recipe", joined)
        self.assertIn("derived member SQL-11 is itself derived", joined)


class RegistryIntegrationTests(unittest.TestCase):
    def test_premium_runnable_all_have_recipes(self):
        registry = load_registry()
        caps = frozenset({
            "component.scope", "component.overview", "component.sql",
            "component.resource_control", "component.logical_storage", "component.availability",
        })
        selection = select_checks(registry, SelectionRequest("Premium", "health", "individual_cluster", None, caps))
        recipes = ca.load_recipes()
        missing = [c for c in selection["runnable_check_ids"] if c not in recipes]
        self.assertEqual(missing, [])


class ChangefeedStateCheckTests(unittest.TestCase):
    """CDC-01: changefeed inventory / state (ticdc_coordinator_changefeed_state)."""

    def _ctx(self, states):
        api = _VectorAPI(states)
        # states are keyed by the `state` label, not `instance`.
        api.query_instant = lambda cid, q, t: vector(states, label="state") if states else {
            "status": "success", "data": {"result": []}}
        return make_ctx(api=api)

    def test_na_when_no_changefeeds(self):
        r = ca.changefeed_state_check(self._ctx({}), "CDC-01",
                                      {"query": "ticdc_coordinator_changefeed_state",
                                       "na_reason": "changefeed_metric_unavailable"})
        self.assertEqual(r["status"], "N/A")
        self.assertEqual(r["reason"], "changefeed_metric_unavailable")
        self.assertEqual(r.get("evidence", {}), {})

    def test_no_changefeeds_total_zero_healthy(self):
        r = ca.changefeed_state_check(self._ctx({"Working": 0, "Total": 0}), "CDC-01",
                                      {"query": "q"})
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["reason"], "no_changefeeds")
        self.assertIn("changefeed_states", r["evidence"])

    def test_all_working_healthy(self):
        r = ca.changefeed_state_check(self._ctx({"Total": 3, "Working": 3}), "CDC-01", {"query": "q"})
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["reason"], "all_changefeeds_working")

    def test_failed_warns(self):
        r = ca.changefeed_state_check(self._ctx({"Total": 3, "Working": 2, "Failed": 1}), "CDC-01", {"query": "q"})
        self.assertEqual(r["status"], "WARNING")
        self.assertEqual(r["reason"], "changefeed_failed")

    def test_not_all_working_notice(self):
        r = ca.changefeed_state_check(self._ctx({"Total": 3, "Working": 2, "Stopped": 1}), "CDC-01", {"query": "q"})
        self.assertEqual(r["status"], "NOTICE")
        self.assertEqual(r["reason"], "changefeed_not_all_working")


class CombinedScalarThresholdTests(unittest.TestCase):
    """CDC-02: worse of checkpoint-ts lag and resolved-ts lag, graded."""

    RULES = {"cdc_checkpoint_lag_s": {"kind": "high", "warning": 300, "critical": 3600}}

    def _ctx(self, checkpoint, resolved):
        instants = {}
        if checkpoint is not None:
            instants["checkpoint_ts_lag"] = checkpoint
        if resolved is not None:
            instants["resolved_ts_lag"] = resolved
        return make_ctx(api=RecordingAPI(instants=instants), rules=self.RULES)

    def _params(self):
        return {"queries": ["max(ticdc_dispatchermanager_checkpoint_ts_lag)",
                            "max(ticdc_dispatchermanager_resolved_ts_lag)"],
                "evidence_key": "cdc_checkpoint_lag_max",
                "rule": "cdc_checkpoint_lag_s", "reason": "checkpoint_lag",
                "na_reason": "changefeed_lag_metric_unavailable"}

    def test_na_when_both_empty(self):
        r = ca.combined_scalar_threshold(self._ctx(None, None), "CDC-02", self._params())
        self.assertEqual(r["status"], "N/A")
        self.assertEqual(r["reason"], "changefeed_lag_metric_unavailable")
        self.assertEqual(r.get("evidence", {}), {})

    def test_healthy_below_warn(self):
        r = ca.combined_scalar_threshold(self._ctx(10.0, 20.0), "CDC-02", self._params())
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["evidence"]["cdc_checkpoint_lag_max"], 20.0)

    def test_warn_on_worse_of_two(self):
        r = ca.combined_scalar_threshold(self._ctx(10.0, 500.0), "CDC-02", self._params())
        self.assertEqual(r["status"], "WARNING")
        self.assertEqual(r["evidence"]["cdc_checkpoint_lag_max"], 500.0)

    def test_critical_escalation(self):
        r = ca.combined_scalar_threshold(self._ctx(4000.0, 10.0), "CDC-02", self._params())
        self.assertEqual(r["status"], "CRITICAL")
        self.assertEqual(r["reason"], "checkpoint_lag")


class _ErrorSignalAPI:
    """Fake for TIKV-08: counter instants by substring + a loki stub."""

    def __init__(self, counters, loki_lines=0, loki_fail=False):
        self.counters = counters
        self.loki_lines = loki_lines
        self.loki_fail = loki_fail

    def query_instant(self, cid, q, t):
        for key, val in self.counters.items():
            if key in q:
                return scalar(val)
        return {"status": "success", "data": {"result": []}}

    def query_range(self, *a, **k):
        return {"status": "success", "data": {"result": []}}

    def loki_query_range(self, cid, query, start_ns, end_ns, limit):
        if self.loki_fail:
            return {"error": "HTTP 500: boom"}
        return {"data": {"result": [
            {"stream": {}, "values": [["0", "line"]] * self.loki_lines}]}} if self.loki_lines else {
            "data": {"result": []}}


class TikvErrorSignalsTests(unittest.TestCase):
    """TIKV-08: four PromQL counter totals + Loki error-log count, 5 stable keys."""

    COUNTERS = [
        {"evidence_key": "critical_error_total", "query": "tikv_critical_error_total"},
        {"evidence_key": "channel_full_total", "query": "tikv_channel_full_total"},
        {"evidence_key": "server_report_failure_total", "query": "tikv_server_report_failure_msg_total"},
        {"evidence_key": "grpc_fail_total", "query": "tikv_grpc_msg_fail_total"},
    ]
    EXPECTED_KEYS = {"critical_error_total", "channel_full_total",
                     "server_report_failure_total", "grpc_fail_total", "error_log_count"}

    def _ctx(self, api):
        ctx = make_ctx(api=api)
        ctx.namespace = "tidbC1"
        return ctx

    def _params(self):
        return {"counters": self.COUNTERS, "warn_reason": "error_signals_present",
                "healthy_reason": "no_error_signals"}

    def test_healthy_all_zero_has_five_keys(self):
        api = _ErrorSignalAPI({}, loki_lines=0)  # all counters empty, no logs
        r = ca.tikv_error_signals(self._ctx(api), "TIKV-08", self._params())
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["reason"], "no_error_signals")
        self.assertEqual(set(r["evidence"].keys()), self.EXPECTED_KEYS)
        self.assertTrue(all(v == 0 for v in r["evidence"].values()))

    def test_warn_on_positive_counter(self):
        api = _ErrorSignalAPI({"tikv_grpc_msg_fail_total": 7}, loki_lines=0)
        r = ca.tikv_error_signals(self._ctx(api), "TIKV-08", self._params())
        self.assertEqual(r["status"], "WARNING")
        self.assertEqual(r["reason"], "error_signals_present")
        self.assertEqual(set(r["evidence"].keys()), self.EXPECTED_KEYS)
        self.assertEqual(r["evidence"]["grpc_fail_total"], 7.0)

    def test_warn_on_loki_logs_only(self):
        api = _ErrorSignalAPI({}, loki_lines=12)
        r = ca.tikv_error_signals(self._ctx(api), "TIKV-08", self._params())
        self.assertEqual(r["status"], "WARNING")
        self.assertEqual(r["evidence"]["error_log_count"], 12)

    def test_loki_failure_is_zero_not_warning(self):
        api = _ErrorSignalAPI({}, loki_fail=True)
        r = ca.tikv_error_signals(self._ctx(api), "TIKV-08", self._params())
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["evidence"]["error_log_count"], 0)
        self.assertEqual(set(r["evidence"].keys()), self.EXPECTED_KEYS)

    def test_no_loki_method_still_five_keys(self):
        # API without loki_query_range → error_log_count defaults to 0, key present.
        r = ca.tikv_error_signals(self._ctx(RecordingAPI()), "TIKV-08", self._params())
        self.assertEqual(set(r["evidence"].keys()), self.EXPECTED_KEYS)
        self.assertEqual(r["evidence"]["error_log_count"], 0)


if __name__ == "__main__":
    unittest.main()
