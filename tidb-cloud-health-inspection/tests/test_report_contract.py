import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "config" / "report-contract.v1.json"


class ReportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_plan_groups_are_disjoint_and_complete(self):
        logical = self.contract["plan_groups"]["logical"]
        full_stack = self.contract["plan_groups"]["full_stack"]
        self.assertEqual(logical, ["Starter", "Essential", "Premium"])
        self.assertEqual(full_stack, ["Dedicated", "BYOC"])
        self.assertFalse(set(logical) & set(full_stack))

    def test_customer_header_excludes_internal_audit_fields(self):
        customer_keys = {item["key"] for item in self.contract["customer_header"]}
        internal = set(self.contract["internal_audit_fields"])
        self.assertFalse(customer_keys & internal)
        for forbidden in ("registry_version", "checklist_signature", "selection", "check_id"):
            self.assertNotIn(forbidden, customer_keys)

    def test_p99_is_primary_for_distribution_metrics(self):
        metrics = self.contract["logical_health_metrics"]
        by_key = {item["key"]: item for item in metrics}
        for key in ("qps", "ru_consumption", "query_latency_p99", "query_latency_p999", "request_wait_p99"):
            self.assertEqual(by_key[key]["primary"], "time_window_p99")
            self.assertIn("p50", by_key[key]["supporting"])
            self.assertIn("p95", by_key[key]["supporting"])
            self.assertIn("p999", by_key[key]["supporting"])
            self.assertIn("max", by_key[key]["supporting"])

    def test_ru_and_na_rules_are_explicit(self):
        rules = self.contract["value_rules"]
        self.assertIn("exact counter increase", rules["ru_rcu"])
        self.assertIn("N/A", rules["na"])
        self.assertIn("Never", rules["na"])
        self.assertIn("request-latency P99 series", rules["percentile_semantics"])

    def test_customer_privacy_contract(self):
        rule = self.contract["privacy_rules"]["customer_report"]
        for phrase in ("checklist IDs", "registry metadata", "raw PromQL", "SQL text", "Top-N SQL"):
            self.assertIn(phrase, rule)
        logical = self.contract["privacy_rules"]["logical_scope"]
        for prohibited in ("nodes", "stores", "regions", "peers", "disks", "schedulers", "Raft", "compaction"):
            self.assertIn(prohibited, logical)

    def test_fixed_customer_labels_have_no_check_id(self):
        labels = self.contract["fixed_table_labels"]
        self.assertEqual(labels["core_health_metrics"], ["Metric", "Primary P99", "Supporting Distribution"])
        self.assertNotIn("Check ID", labels["finding"])
        serialized = json.dumps(labels)
        self.assertNotIn("Checklist", serialized)


if __name__ == "__main__":
    unittest.main()
