import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import handoff


class HandoffTests(unittest.TestCase):
    def test_component_routing_by_prefix(self):
        self.assertEqual(handoff.component_of("TIKV-08"), "TiKV")
        self.assertEqual(handoff.component_of("TIDB-01"), "TiDB")
        self.assertEqual(handoff.component_of("PD-03"), "PD")
        self.assertEqual(handoff.component_of("CDC-02"), "TiCDC")
        self.assertEqual(handoff.component_of("SQL-02"), "Workload")
        self.assertEqual(handoff.component_of("RCU-05"), "ResourceControl")
        self.assertEqual(handoff.component_of("ZZZ-9"), "Other")

    def test_build_handoff_contract(self):
        normalized = {
            "cluster_id": "C1", "cluster_name": "demo", "plan": "Dedicated",
            "window": {"start": "s", "end": "e"},
            "analysis_candidates": [
                {"check_id": "TIKV-08", "status": "WARNING", "reason": "error_signals_present"},
                {"check_id": "SQL-02", "status": "NOTICE", "reason": "latency"},
            ],
        }
        h = handoff.build_handoff(normalized)
        self.assertEqual(h["deep_analysis"], "delegated")
        self.assertEqual(h["cluster_id"], "C1")
        self.assertEqual(h["plan"], "Dedicated")
        self.assertEqual(len(h["items"]), 2)
        tikv = next(i for i in h["items"] if i["check_id"] == "TIKV-08")
        self.assertEqual(tikv["component"], "TiKV")
        self.assertEqual(tikv["severity"], "WARNING")
        self.assertEqual(tikv["signal"], "error_signals_present")

    def test_build_handoff_empty_when_all_healthy(self):
        h = handoff.build_handoff({"cluster_id": "C1", "analysis_candidates": []})
        self.assertEqual(h["items"], [])
        self.assertEqual(h["deep_analysis"], "delegated")

    def test_contract_carries_no_raw_values(self):
        # Items expose only routing fields, never evidence/metric values.
        h = handoff.build_handoff({
            "analysis_candidates": [{"check_id": "TIKV-01", "status": "WARNING", "reason": "r"}]})
        self.assertEqual(sorted(h["items"][0].keys()), ["check_id", "component", "severity", "signal"])


if __name__ == "__main__":
    unittest.main()
