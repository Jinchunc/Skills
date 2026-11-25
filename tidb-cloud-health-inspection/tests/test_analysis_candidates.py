"""The engine surfaces which checks warrant deeper interpretation."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from checklist_engine import evaluate
from checklist_registry import SelectionRequest, load_registry, select_checks

CAPS = frozenset({
    "component.scope", "component.overview", "component.sql",
    "component.resource_control", "component.logical_storage", "component.availability",
})


class AnalysisCandidateTests(unittest.TestCase):
    def selection(self):
        return select_checks(load_registry(), SelectionRequest("Premium", "health", "individual_cluster", None, CAPS))

    def test_warning_and_critical_become_candidates(self):
        payload = {
            "window": {"start": "2026-11-20T00:00:00Z", "end": "2026-11-21T00:00:00Z"},
            "check_results": [
                {"check_id": "SQL-02", "status": "WARNING", "evidence": {}, "reason": "latency_high"},
                {"check_id": "SQL-01", "status": "HEALTHY", "evidence": {}, "reason": "ok"},
            ],
        }
        normalized = evaluate(payload, self.selection())
        self.assertTrue(normalized["deep_dive_recommended"])
        candidates = {c["check_id"] for c in normalized["analysis_candidates"]}
        self.assertIn("SQL-02", candidates)
        self.assertNotIn("SQL-01", candidates)

    def test_all_healthy_has_no_candidates(self):
        payload = {
            "window": {"start": "2026-11-20T00:00:00Z", "end": "2026-11-21T00:00:00Z"},
            "check_results": [{"check_id": "SQL-01", "status": "HEALTHY", "evidence": {}, "reason": "ok"}],
        }
        normalized = evaluate(payload, self.selection())
        self.assertFalse(normalized["deep_dive_recommended"])
        self.assertEqual(normalized["analysis_candidates"], [])


if __name__ == "__main__":
    unittest.main()
