from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from checklist_engine import evaluate  # noqa: E402
from checklist_registry import SelectionRequest, load_registry, select_checks  # noqa: E402


class ChecklistEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry()

    @staticmethod
    def payload(check_results=None):
        return {
            "cluster_id": "test-cluster",
            "window": {
                "timezone": "UTC",
                "start": "2026-11-20T00:00:00+00:00",
                "end": "2026-11-20T01:00:00+00:00",
                "step": "5m",
                "aggregation": "window",
            },
            "check_results": check_results or [],
        }

    def test_missing_results_are_explicit_na(self) -> None:
        selection = select_checks(
            self.registry,
            SelectionRequest("Premium", "health", "individual_cluster"),
        )
        result = evaluate(self.payload(), selection)
        self.assertEqual("N/A", result["status"])
        self.assertEqual("LOW", result["confidence"])
        self.assertFalse(result["checklist_complete"])
        self.assertEqual(set(selection["selected_check_ids"]), set(result["na_check_ids"]))
        self.assertEqual(set(selection["runnable_check_ids"]), set(result["missing_runnable_check_ids"]))

    def test_planned_na_does_not_make_runtime_coverage_incomplete(self) -> None:
        selection = select_checks(
            self.registry,
            SelectionRequest("Premium", "health", "individual_cluster"),
        )
        reported = [
            {"check_id": check_id, "status": "HEALTHY", "evidence": {}}
            for check_id in selection["runnable_check_ids"]
        ]
        result = evaluate(self.payload(reported), selection)
        self.assertTrue(result["checklist_complete"])
        self.assertEqual([], result["missing_runnable_check_ids"])
        self.assertTrue(result["na_check_ids"])

    def test_reported_result_is_bound_to_selected_check(self) -> None:
        selection = select_checks(
            self.registry,
            SelectionRequest("Premium", "health", "individual_cluster"),
        )
        result = evaluate(
            self.payload([{"check_id": "SQL-01", "status": "WARNING", "evidence": {"p99_ms": 800}}]),
            selection,
        )
        self.assertEqual("WARNING", result["status"])
        self.assertEqual("MEDIUM", result["confidence"])
        self.assertIn("SQL-01", result["attempted_check_ids"])

    def test_prohibited_physical_result_fails_closed(self) -> None:
        selection = select_checks(
            self.registry,
            SelectionRequest("Premium", "health", "individual_cluster"),
        )
        with self.assertRaises(ValueError):
            evaluate(self.payload([{"check_id": "TIKV-01", "status": "HEALTHY"}]), selection)

    def test_window_is_mandatory(self) -> None:
        selection = select_checks(
            self.registry,
            SelectionRequest("Dedicated", "health", "individual_cluster"),
        )
        with self.assertRaises(ValueError):
            evaluate({"check_results": []}, selection)


if __name__ == "__main__":
    unittest.main()
