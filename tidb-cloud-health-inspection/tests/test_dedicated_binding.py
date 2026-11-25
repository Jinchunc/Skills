"""Tests for the Dedicated/BYOC report -> canonical check_results binding.

The monolith (dedicated_health_inspection.py) was retired 2026-11-25; collect_full_stack
is now native. dedicated_binding is kept as a reference oracle for the CDC parity +
golden shape guardrails, so these tests still pin its behavior."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dedicated_binding as db

STATUS_RANK = {"HEALTHY": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3}


def by_id(results):
    return {r["check_id"]: r for r in results}


def worst(results):
    ranked = [r["status"] for r in results if r["status"] in STATUS_RANK]
    return max(ranked, key=STATUS_RANK.__getitem__) if ranked else "N/A"


def healthy_report(**overrides):
    report = {
        "cluster_name": "demo",
        "provider": "aws",
        "region": "us-east-1",
        "version": "v8.1.0",
        "status": "AVAILABLE",
        "verdict": "Healthy",
        "unknown_checks": [],
        "analysis": {},
        "metrics": {
            "cpu_max_by_instance": {"tikv-0": 40.0},
            "memory_max_by_instance": {"tikv-0": 55.0},
            "grpc_qps_avg_by_instance": {"tikv-0": 1000.0},
            "store_usage_by_instance": {"tikv-0": 50.0},
            "region_by_instance": {"tikv-0": 100.0},
            "leader_by_instance": {"tikv-0": 33.0},
            "restarts_by_instance": {"tikv-0": 0.0},
        },
    }
    report.update(overrides)
    return report


class VerdictMappingTests(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(db.verdict_to_status("Healthy"), "HEALTHY")
        self.assertEqual(db.verdict_to_status("Warning"), "WARNING")
        self.assertEqual(db.verdict_to_status("Error"), "CRITICAL")
        self.assertEqual(db.verdict_to_status("Needs Review"), "N/A")
        self.assertEqual(db.verdict_to_status(None), "N/A")


class BindingTests(unittest.TestCase):
    def test_healthy_report_binds_healthy(self):
        results = by_id(db.bind_dedicated_results(healthy_report()))
        for check_id in ("TIKV-01", "TIKV-02", "TIKV-03", "TIKV-06", "TIKV-07", "TIKV-10", "AVL-02", "INF-03", "PD-03", "OVR-01", "OVR-02", "SCOPE-01"):
            self.assertEqual(results[check_id]["status"], "HEALTHY", check_id)
        self.assertTrue(results["SCOPE-01"]["evidence"]["physical_infrastructure_queried"])

    def test_cpu_sustained_is_warning(self):
        results = by_id(db.bind_dedicated_results(healthy_report(analysis={"cpu_sustained_high": [{"instance": "tikv-0"}]})))
        self.assertEqual(results["TIKV-01"]["status"], "WARNING")

    def test_memory_error_is_critical(self):
        results = by_id(db.bind_dedicated_results(healthy_report(analysis={"memory_error_windows": [{"instance": "tikv-0"}]})))
        self.assertEqual(results["TIKV-02"]["status"], "CRITICAL")

    def test_memory_growth_is_warning(self):
        results = by_id(db.bind_dedicated_results(healthy_report(analysis={"memory_leak_warning_nodes": ["tikv-0"]})))
        self.assertEqual(results["TIKV-02"]["status"], "WARNING")

    def test_grpc_and_compaction_and_urp(self):
        results = by_id(db.bind_dedicated_results(healthy_report(analysis={
            "grpc_fail_spikes": [1], "compaction_pending_sustained": [1], "unified_read_hotspots": [1],
        })))
        self.assertEqual(results["TIKV-03"]["status"], "WARNING")
        self.assertEqual(results["TIKV-06"]["status"], "WARNING")
        self.assertEqual(results["TIKV-07"]["status"], "WARNING")

    def test_restart_detected_is_warning(self):
        results = by_id(db.bind_dedicated_results(healthy_report(metrics={"restarts_by_instance": {"tikv-0": 2.0}})))
        self.assertEqual(results["TIKV-10"]["status"], "WARNING")
        self.assertEqual(results["AVL-02"]["status"], "WARNING")

    def test_missing_metrics_are_na(self):
        report = healthy_report(metrics={}, unknown_checks=["TiKV max CPU: timeout", "TiKV memory: timeout"])
        results = by_id(db.bind_dedicated_results(report))
        for check_id in ("TIKV-01", "TIKV-02", "TIKV-03", "INF-03", "PD-03"):
            self.assertEqual(results[check_id]["status"], "N/A", check_id)

    def test_extended_bindings_warn_on_signals(self):
        report = healthy_report(metrics={
            **healthy_report()["metrics"],
            "write_stall_peak": 0,
            "scheduler_discard_ratio_peak": 0.2,
            "error_log_count": 3,
            "slow_score_peak": 150,
            "raft_log_lag_p99_peak": 1000,
            "iops_exceed_peak": 2.0,
            "disk_read_latency_peak": 0.01,
        })
        results = by_id(db.bind_dedicated_results(report))
        self.assertEqual(results["TIKV-04"]["status"], "HEALTHY")   # no write stall
        self.assertEqual(results["TIKV-05"]["status"], "WARNING")   # requests discarded
        self.assertEqual(results["TIKV-08"]["status"], "WARNING")   # error log > 0
        self.assertEqual(results["TIKV-09"]["status"], "WARNING")   # slow score >= 100
        self.assertEqual(results["INF-04"]["status"], "WARNING")    # exceeded IOPS >= 1.0
        self.assertEqual(results["INF-05"]["status"], "HEALTHY")    # latency observed

    def test_extended_bindings_na_without_metrics(self):
        results = by_id(db.bind_dedicated_results(healthy_report(metrics={})))
        for check_id in ("TIKV-04", "TIKV-05", "TIKV-08", "TIKV-09", "INF-04", "INF-05"):
            self.assertEqual(results[check_id]["status"], "N/A", check_id)

    def test_responsiveness_threshold_from_rules(self):
        base = healthy_report()["metrics"]
        below = by_id(db.bind_dedicated_results(healthy_report(metrics={**base, "slow_score_peak": 50})))
        self.assertEqual(below["TIKV-09"]["status"], "HEALTHY")
        at = by_id(db.bind_dedicated_results(healthy_report(metrics={**base, "slow_score_peak": 100})))
        self.assertEqual(at["TIKV-09"]["status"], "WARNING")

    def test_tidb_resource_binding(self):
        base = healthy_report()["metrics"]
        high = by_id(db.bind_dedicated_results(healthy_report(metrics={
            **base, "tidb_cpu_max_by_instance": {"tidb-0": 90.0}, "tidb_memory_max_by_instance": {"tidb-0": 3.0e9}})))
        self.assertEqual(high["TIDB-01"]["status"], "WARNING")   # >= tidb_cpu_pct warning (85)
        self.assertEqual(high["TIDB-02"]["status"], "HEALTHY")   # RSS observation
        low = by_id(db.bind_dedicated_results(healthy_report(metrics={**base, "tidb_cpu_max_by_instance": {"tidb-0": 40.0}})))
        self.assertEqual(low["TIDB-01"]["status"], "HEALTHY")

    def test_tidb_resource_na_without_metrics(self):
        results = by_id(db.bind_dedicated_results(healthy_report(metrics={})))
        self.assertEqual(results["TIDB-01"]["status"], "N/A")
        self.assertEqual(results["TIDB-02"]["status"], "N/A")

    def test_reliability_bindings_healthy(self):
        report = healthy_report(status="active", metrics={
            **healthy_report()["metrics"],
            "cpu_max_by_instance": {"tikv-0": 40.0, "tikv-1": 41.0, "tikv-2": 42.0},  # 3 instances → redundant
            "changefeed_states": {"Total": 51, "Working": 51},
            "cdc_checkpoint_lag_max": 2.0,
        })
        results = by_id(db.bind_dedicated_results(report))
        self.assertEqual(results["AVL-01"]["status"], "HEALTHY")       # product status active
        self.assertEqual(results["AVL-08"]["status"], "HEALTHY")       # >=3 instances (from cpu map)
        self.assertEqual(results["CDC-01"]["status"], "HEALTHY")       # all working
        self.assertEqual(results["CDC-02"]["status"], "HEALTHY")       # lag < 300s

    def test_reliability_bindings_degraded(self):
        report = healthy_report(status="recovering", metrics={
            "cpu_max_by_instance": {"tikv-0": 40.0},  # single instance → single-point
            "changefeed_states": {"Total": 3, "Working": 2, "Failed": 1},
            "cdc_checkpoint_lag_max": 5000.0,
        })
        results = by_id(db.bind_dedicated_results(report))
        self.assertEqual(results["AVL-01"]["status"], "WARNING")       # status not available-ish
        self.assertEqual(results["AVL-08"]["status"], "WARNING")       # single instance
        self.assertEqual(results["CDC-01"]["status"], "WARNING")       # a changefeed failed
        self.assertEqual(results["CDC-02"]["status"], "CRITICAL")      # lag >= 3600s

    def test_reliability_na_without_metrics(self):
        results = by_id(db.bind_dedicated_results(healthy_report(status="", metrics={})))
        for cid in ("AVL-01", "AVL-08", "CDC-01", "CDC-02"):
            self.assertEqual(results[cid]["status"], "N/A", cid)

    def test_worst_matches_verdict_floor(self):
        # When the collector flags warnings, the bound subset reaches at least WARNING.
        report = healthy_report(verdict="Warning", analysis={"cpu_sustained_high": [1]})
        self.assertEqual(worst(db.bind_dedicated_results(report)), db.verdict_to_status(report["verdict"]))


if __name__ == "__main__":
    unittest.main()
