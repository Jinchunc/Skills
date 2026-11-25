"""Tests for organization-wide screening (list, aggregate, and the org runner loop)."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import health_inspection as hi


class OrgHelperTests(unittest.TestCase):
    def test_normalize_cluster_ids(self):
        self.assertEqual(hi.normalize_cluster_ids(["a", "b,c", "a"]), ["a", "b", "c"])
        self.assertEqual(hi.normalize_cluster_ids(None), [])

    def test_list_org_cluster_ids_tolerant(self):
        class API:
            def list_clusters(self, org_id, **kw):
                return {"items": [{"clusterID": "1"}, {"cluster_id": "2"}, {"id": "3"}, {"nope": "x"}, "junk"]}
        self.assertEqual(hi.list_org_cluster_ids(API(), "org"), ["1", "2", "3"])

    def test_list_org_cluster_ids_raises_on_error(self):
        class API:
            def list_clusters(self, org_id, **kw):
                return {"error": "forbidden"}
        with self.assertRaisesRegex(RuntimeError, "cluster listing failed"):
            hi.list_org_cluster_ids(API(), "org")

    def test_aggregate_report_orders_and_counts(self):
        results = [
            {"cluster_id": "a", "cluster_name": "a", "plan": "Premium", "status": "HEALTHY", "confidence": "HIGH"},
            {"cluster_id": "b", "cluster_name": "b", "plan": "Dedicated", "status": "CRITICAL", "confidence": "HIGH"},
        ]
        failures = [{"cluster_id": "c", "error": "boom"}]
        report = hi.aggregate_org_report("org1", results, failures)
        self.assertIn("Clusters inspected: **2**", report)
        self.assertIn("Failed to inspect: **1**", report)
        # CRITICAL listed before HEALTHY (most severe first)
        self.assertLess(report.index("CRITICAL"), report.index("HEALTHY"))
        self.assertIn("boom", report)


class OrgRunnerTests(unittest.TestCase):
    def setUp(self):
        self._api = hi.ClinicAPI
        self._inspect = hi.inspect_one
        hi.ClinicAPI = lambda env_file=None: object()

        def fake_inspect(api, cluster_id, args, output_dir):
            if cluster_id == "B":
                raise RuntimeError("discovery failed")
            return {"cluster_id": cluster_id, "cluster_name": cluster_id, "plan": "Premium",
                    "status": "WARNING" if cluster_id == "A" else "HEALTHY", "confidence": "MEDIUM"}

        hi.inspect_one = fake_inspect

    def tearDown(self):
        hi.ClinicAPI = self._api
        hi.inspect_one = self._inspect

    def test_org_loop_isolates_failures_and_aggregates(self):
        with tempfile.TemporaryDirectory() as d:
            args = SimpleNamespace(
                org_id=None, cluster_id=["A", "B", "C"], output_dir=d,
                profile="health", start=None, end=None, relative_window="1d",
                step_seconds=300, expected_plan=None, env_file=".env", registry=None,
            )
            rc = hi.run_inspect_org(args)
            self.assertEqual(rc, 0)
            summary = json.loads((Path(d) / "org-summary.json").read_text())
            self.assertEqual(summary["clusters_inspected"], 2)
            self.assertEqual(summary["clusters_failed"], 1)
            self.assertTrue((Path(d) / "org-report.md").exists())

    def test_org_requires_target(self):
        with tempfile.TemporaryDirectory() as d:
            args = SimpleNamespace(
                org_id=None, cluster_id=None, output_dir=d,
                profile="health", start=None, end=None, relative_window="1d",
                step_seconds=300, expected_plan=None, env_file=".env", registry=None,
            )
            self.assertEqual(hi.run_inspect_org(args), 1)
            self.assertTrue((Path(d) / "failure.json").exists())


if __name__ == "__main__":
    unittest.main()
