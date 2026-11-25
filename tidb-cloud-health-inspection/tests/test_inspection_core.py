import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from inspection_core import distribution, normalize_plan, resolve_window


class InspectionCoreTests(unittest.TestCase):
    def test_distribution_has_p99_first_supporting_percentiles(self):
        result = distribution(range(1, 1001))
        self.assertEqual(result["percentile_method"], "linear_r7")
        self.assertAlmostEqual(result["p50"], 500.5)
        self.assertAlmostEqual(result["p95"], 950.05)
        self.assertAlmostEqual(result["p99"], 990.01)
        self.assertAlmostEqual(result["p999"], 999.001)
        self.assertEqual(result["max"], 1000)

    def test_relative_window_is_utc_and_exact(self):
        window = resolve_window(
            None,
            "2026-11-20T09:00:00+00:00",
            "7d",
            300,
            now=datetime(2026, 11, 20, 9, tzinfo=timezone.utc),
        )
        self.assertEqual(window.duration_seconds, 7 * 86400)
        self.assertEqual(window.promql_duration, "7d")

    def test_plan_normalization(self):
        self.assertEqual(normalize_plan({"deployTypeV2": "premium"}), "Premium")
        self.assertEqual(normalize_plan({"deployType": "serverless"}), "Starter")
        self.assertEqual(normalize_plan({"deployTypeV2": "mystery"}), "Unknown")
        # Real deploy types seen live: versioned plan → its plan; pool record → Unknown.
        self.assertEqual(normalize_plan({"deployTypeV2": "essential v2"}), "Essential")
        self.assertEqual(normalize_plan({"deployTypeV2": "premium v2"}), "Premium")
        self.assertEqual(normalize_plan({"deployTypeV2": "premium resource pool"}), "Unknown")


if __name__ == "__main__":
    unittest.main()
