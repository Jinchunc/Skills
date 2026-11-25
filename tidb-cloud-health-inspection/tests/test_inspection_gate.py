#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "inspection_gate.py"


class InspectionGateTest(unittest.TestCase):
    def run_gate(self, payload, *extra):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            cmd = [
                sys.executable,
                str(GATE),
                "--input",
                str(source),
                "--plan",
                "Dedicated",
                "--state-dir",
                str(root / "state"),
                "--artifact-dir",
                str(root / "artifacts"),
                *extra,
            ]
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
            result = json.loads(proc.stdout) if proc.stdout else None
            return proc, result

    def test_healthy_does_not_fire(self):
        proc, result = self.run_gate({"cluster_id": "c1", "verdict": "Healthy", "findings": []})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(result["fire"])
        self.assertEqual(result["notification_owner"], "none")

    def test_warning_is_deterministic_by_default(self):
        payload = {"cluster_id": "c1", "verdict": "Warning", "findings": [{"severity": "Warning", "title": "CPU"}]}
        proc, result = self.run_gate(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(result["fire"])
        self.assertEqual(result["notification_owner"], "python")

    def test_warning_can_require_agent(self):
        payload = {"cluster_id": "c1", "verdict": "Warning", "findings": [{"severity": "Warning", "title": "CPU"}]}
        proc, result = self.run_gate(payload, "--llm-for-warning")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["fire"])
        self.assertEqual(result["notification_owner"], "agent")

    def test_deterministic_critical_stays_in_python(self):
        payload = {"cluster_id": "c1", "verdict": "Error", "findings": [{"severity": "Error", "title": "Disk"}]}
        proc, result = self.run_gate(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(result["fire"])
        self.assertEqual(result["status"], "CRITICAL")
        self.assertEqual(result["notification_owner"], "python")

    def test_explicit_analysis_request_requires_agent(self):
        payload = {
            "cluster_id": "c1",
            "verdict": "Error",
            "analysis_required": True,
            "analysis_reason": "conflicting_evidence",
            "findings": [{"severity": "Error", "title": "Disk"}],
        }
        proc, result = self.run_gate(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(result["fire"])
        self.assertEqual(result["notification_owner"], "agent")
        self.assertEqual(result["analysis_reasons"], ["conflicting_evidence"])

    def test_collector_error_is_not_healthy(self):
        proc, result = self.run_gate({"error": "permission denied"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNone(result)
        failure = json.loads(proc.stderr)
        self.assertEqual(failure["status"], "N/A")

    def test_check_result_severity_above_verdict_raises_status(self):
        # Regression: the gate used to read only the top-level verdict and ignore
        # check_results, so a per-check severity (e.g. a tenant SQL check going
        # CRITICAL) above a lower collector verdict was dropped → gate_consistent False.
        payload = {
            "cluster_id": "c1",
            "verdict": "Warning",
            "status": "CRITICAL",
            "findings": [{"severity": "Error", "title": "SQL"}],
            "check_results": [
                {"check_id": "TIKV-08", "status": "WARNING", "reason": "x"},
                {"check_id": "SQL-02", "status": "CRITICAL", "reason": "y"},
                {"check_id": "CDC-01", "status": "N/A", "reason": "z"},
                {"check_id": "OVR-01", "status": "HEALTHY", "reason": "w"},
            ],
        }
        proc, result = self.run_gate(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Gate now derives CRITICAL from check_results, matching normalized["status"].
        self.assertEqual(result["status"], "CRITICAL")

    def test_na_check_results_do_not_inflate_status(self):
        # N/A checks are excluded (like checklist_engine.evaluate): a HEALTHY verdict
        # with only HEALTHY/N/A checks stays HEALTHY, not N/A.
        payload = {
            "cluster_id": "c1",
            "verdict": "Healthy",
            "findings": [],
            "check_results": [
                {"check_id": "OVR-01", "status": "HEALTHY", "reason": "w"},
                {"check_id": "CDC-01", "status": "N/A", "reason": "z"},
            ],
        }
        proc, result = self.run_gate(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "HEALTHY")


if __name__ == "__main__":
    unittest.main()
