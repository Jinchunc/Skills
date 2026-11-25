"""Regression: a quiet (low-traffic) logical cluster must pass validation as N/A,
not hard-fail the whole inspection (Fix 2)."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import health_inspection as hi
from customer_report import render_health_report


def stats(value):
    return {"samples": 5, "min": value, "avg": value, "p50": value, "p95": value, "p99": value, "p999": value, "max": value}


class QuietClusterValidationTests(unittest.TestCase):
    def normalized(self):
        return {
            "plan": "Premium",
            "status": "HEALTHY",
            "confidence": "MEDIUM",
            "cluster_id": "C1",
            "cluster_name": "quiet",
            "metadata": {"provider": "aws", "region": "ap-south-1", "version": "v8.1.0"},
            "window": {"start": "2026-11-20T00:00:00+00:00", "end": "2026-11-21T00:00:00+00:00"},
            "check_results": [
                # Empty latency histogram -> legitimate N/A with no statistics_seconds.
                {"check_id": "SQL-02", "status": "N/A", "evidence": {"query": "q", "unit": "s"}, "reason": "empty_series"},
                {"check_id": "RCU-01", "status": "HEALTHY",
                 "evidence": {"statistics_ru_per_second": stats(1.0), "window_total_ru": 10.0}},
                {"check_id": "SCOPE-01", "status": "HEALTHY", "evidence": {"physical_infrastructure_queried": False}},
            ],
        }

    def test_quiet_cluster_passes_and_reports_na(self):
        normalized = self.normalized()
        raw = {"plan": "Premium"}
        report = render_health_report(normalized)
        gate = {"status": normalized["status"]}
        validation = hi.validate_artifacts(raw, normalized, report, gate, "Premium")
        self.assertTrue(validation["passed"], validation["checks"])
        self.assertTrue(validation["checks"]["sql_latency_attempted"])


if __name__ == "__main__":
    unittest.main()
