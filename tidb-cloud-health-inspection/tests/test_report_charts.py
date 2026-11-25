import unittest
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import report_charts


class ReportChartsTests(unittest.TestCase):
    def _payload(self):
        series = [[1700000000 + i * 300, 100.0 + i] for i in range(20)]
        prev = [[1700000000 - 7 * 86400 + i * 300, 90.0 + i] for i in range(20)]
        return {
            "window": {"start": "2026-11-16T00:00:00+00:00"},
            "previous_window": {"start": "2026-11-09T00:00:00+00:00"},
            "check_results": [
                {"check_id": "SQL-01", "evidence": {"series": series}},
                {"check_id": "SQL-03", "evidence": {"series": series}},  # P999 (log-y)
            ],
            "previous_period": {"SQL-01": {"check_id": "SQL-01", "evidence": {"series": prev}}},
        }

    @unittest.skipUnless(report_charts.charts_available(), "matplotlib not installed")
    def test_generate_charts_writes_png(self):
        with tempfile.TemporaryDirectory() as d:
            produced = report_charts.generate_charts(self._payload(), Path(d) / "charts", lang="zh")
            self.assertIn("qps", produced)
            self.assertEqual(produced["qps"], "charts/qps.png")
            self.assertTrue((Path(d) / "charts" / "qps.png").exists())
            self.assertTrue((Path(d) / "charts" / "p999.png").exists())
            # No series for QPS... wait, SQL-02 absent → no p99 chart.
            self.assertNotIn("p99", produced)

    def test_no_crash_without_series(self):
        # Empty payload → no charts, no exception, even if matplotlib is present.
        produced = report_charts.generate_charts({"check_results": []}, "/tmp/nope_charts")
        self.assertEqual(produced, {})


if __name__ == "__main__":
    unittest.main()
