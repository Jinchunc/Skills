import json
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dedicated_golden as dg

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dedicated_golden.json"


class ShapeTests(unittest.TestCase):
    def test_result_shape_strips_values(self):
        results = [
            {"check_id": "TIKV-01", "status": "HEALTHY",
             "evidence": {"cpu_max_by_instance": {"db-tikv-0": 55.0}, "unit": "%"}},
            {"check_id": "SQL-99", "status": "HEALTHY", "evidence": {}},  # not a bound check
        ]
        shape = dg.result_shape(results, dg.BOUND_DEDICATED_CHECKS)
        self.assertIn("TIKV-01", shape)
        self.assertNotIn("SQL-99", shape)  # filtered to bound checks
        self.assertEqual(shape["TIKV-01"]["status"], "HEALTHY")
        # keys only — no raw instance names / values leak into the shape
        self.assertEqual(shape["TIKV-01"]["evidence_keys"], ["cpu_max_by_instance", "unit"])
        self.assertNotIn("db-tikv-0", json.dumps(shape, ensure_ascii=False))

    def test_compare_detects_hard_drift(self):
        base = {"TIKV-08": {"status": "WARNING", "reason": "r", "evidence_keys": ["a", "b"]}}
        # status change (hard)
        d = dg.compare_shapes(base, {"TIKV-08": {"status": "HEALTHY", "reason": "r", "evidence_keys": ["a", "b"]}})
        self.assertTrue(any("status" in x and not x.startswith("note:") for x in d))
        # evidence_keys change (hard)
        d = dg.compare_shapes(base, {"TIKV-08": {"status": "WARNING", "reason": "r", "evidence_keys": ["a"]}})
        self.assertTrue(any("evidence_keys" in x for x in d))
        # missing check (hard)
        d = dg.compare_shapes(base, {})
        self.assertIn("TIKV-08: MISSING in candidate", d)

    def test_compare_reason_is_soft_note(self):
        base = {"AVL-01": {"status": "HEALTHY", "reason": "x", "evidence_keys": []}}
        cand = {"AVL-01": {"status": "HEALTHY", "reason": "y", "evidence_keys": []}}
        diffs = dg.compare_shapes(base, cand)
        self.assertTrue(all(x.startswith("note:") for x in diffs))  # only a soft note

    def test_identical_shapes_have_no_diff(self):
        s = {"PD-03": {"status": "HEALTHY", "reason": None, "evidence_keys": ["v"]}}
        self.assertEqual(dg.compare_shapes(s, s), [])


@unittest.skipUnless(FIXTURE.exists(), "golden fixture not captured yet")
class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.shapes = json.loads(FIXTURE.read_text(encoding="utf-8"))["shapes"]

    def test_fixture_self_consistent(self):
        self.assertEqual(dg.compare_shapes(self.shapes, self.shapes), [])

    def test_fixture_covers_bound_checks(self):
        valid = {"HEALTHY", "NOTICE", "WARNING", "CRITICAL", "N/A"}
        # Every bound check present in the baseline carries a valid status + key list.
        for cid, sh in self.shapes.items():
            self.assertIn(cid, dg.BOUND_DEDICATED_CHECKS, f"{cid} not a bound dedicated check")
            self.assertIn(sh["status"], valid)
            self.assertIsInstance(sh["evidence_keys"], list)

    def test_fixture_no_raw_values(self):
        # The baseline must never carry raw metric values / instance names.
        blob = json.dumps(self.shapes, ensure_ascii=False)
        for leak in ("db-tikv", "cpu_max_by_instance\":{", "0.0,", "basic-tikv"):
            self.assertNotIn(leak, blob)


if __name__ == "__main__":
    unittest.main()
