"""The dedicated collector's thresholds must come from config/rules.v1.json, not
hardcoded constants (single source of truth; no drift)."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dedicated_health_inspection as d
from inspection_core import load_rules


class DedicatedThresholdSourceTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()

    def test_thresholds_are_sourced_from_rules(self):
        r = self.rules
        self.assertEqual(d.CPU_SUSTAIN_WARN_PCT, r["tikv_cpu_sustained"]["sustain_warn_pct"])
        self.assertEqual(d.MEMORY_WARN_PCT, r["tikv_memory_pct"]["warning"])
        self.assertEqual(d.MEMORY_ERROR_PCT, r["tikv_memory_pct"]["critical"])
        self.assertEqual(d.GRPC_FAIL_30M_WARN_THRESHOLD, r["tikv_grpc"]["fail_30m_warn"])
        self.assertEqual(d.GRPC_P99_WARN_SECONDS, r["tikv_grpc"]["p99_warn_s"])
        self.assertEqual(d.MEMORY_LEAK_WARN_DELTA_PCT, r["tikv_memory_leak"]["warn_delta_pct"])
        self.assertEqual(d.MEMORY_LEAK_BASELINE_WINDOW, r["tikv_memory_leak"]["baseline_window"])
        self.assertEqual(d.UNIFIED_READ_HOT_PCT, r["tikv_unified_read_pool"]["hot_pct"])
        self.assertEqual(d.UNIFIED_READ_PEER_LOW_PCT, r["tikv_unified_read_pool"]["peer_low_pct"])
        self.assertEqual(d.STORE_USAGE_WARN_PCT, r["store_capacity_usage_pct"]["warning"])
        self.assertEqual(d.STORE_USAGE_RISE_WARN_PCT, r["store_capacity_usage_pct"]["rise_warn_pct"])
        self.assertEqual(d.COMPACTION_PENDING_SUSTAIN_WARN_BYTES, r["tikv_compaction_pending"]["sustain_warn_bytes"])
        self.assertEqual(d.COMPACTION_PENDING_REFERENCE_BYTES, r["tikv_compaction_pending"]["reference_bytes"])
        self.assertEqual(d.COMPACTION_PENDING_PEAK_WARN_BYTES, r["tikv_compaction_pending"]["peak_warn_bytes"])
        self.assertEqual(d.SLOW_SCORE_ALERT_VALUE, r["tikv_slow_score"]["warning"])
        self.assertEqual(d.RAFT_LOG_LAG_P99_ALERT, r["tikv_raft_log_lag_p99"]["warning"])
        self.assertEqual(d.IOPS_EXCEED_WARN_THRESHOLD, r["inf_iops_exceeded"]["warning"])

    def test_values_match_historical_numbers(self):
        # The move must not have changed behavior.
        self.assertEqual(d.CPU_SUSTAIN_WARN_PCT, 85.0)
        self.assertEqual(d.MEMORY_ERROR_PCT, 90)
        self.assertEqual(d.COMPACTION_PENDING_SUSTAIN_WARN_BYTES, 200 * 1024 ** 3)
        self.assertEqual(d.COMPACTION_PENDING_PEAK_WARN_BYTES, 512 * 1024 ** 3)
        self.assertEqual(d.RAFT_LOG_LAG_P99_ALERT, 5000)


if __name__ == "__main__":
    unittest.main()
