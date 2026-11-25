import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ResultSchemaTests(unittest.TestCase):
    def test_normalized_and_gate_schemas_are_separate(self):
        normalized = json.loads((ROOT / "config" / "result.schema.json").read_text(encoding="utf-8"))
        gate = json.loads((ROOT / "config" / "gate.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(normalized["title"], "TiDB Cloud normalized health inspection")
        self.assertEqual(gate["title"], "TiDB Cloud inspection gate result")
        self.assertIn("check_results", normalized["required"])
        self.assertIn("missing_runnable_check_ids", normalized["required"])
        self.assertIn("evidence_bundle", gate["required"])
        self.assertNotIn("evidence_bundle", normalized["required"])


if __name__ == "__main__":
    unittest.main()
