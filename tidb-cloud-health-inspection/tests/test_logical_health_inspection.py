"""Logical-path coverage for the selection-driven collection layer.

The former LogicalHealthCollector has been retired; the tenant-safe logical path
now runs through the registry selection + collection recipes + adapters. These
tests pin the behavior that mattered: RU is attempted, and the logical path never
emits physical-infrastructure evidence.
"""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from checklist_registry import SelectionRequest, load_registry, select_checks
from collection_adapters import CollectContext, collect, load_recipes, load_rules
from inspection_core import resolve_window

LOGICAL_CAPS = frozenset(
    {
        "component.scope",
        "component.overview",
        "component.sql",
        "component.resource_control",
        "component.logical_storage",
        "component.availability",
    }
)


class FakeAPI:
    def query_range(self, cluster_id, query, start, end, step):
        value = "0.001"
        if "tidb_server_query_total" in query:
            value = "100"
        elif "tidb_ruv2_ru_total" in query:
            value = "200"
        return {"status": "success", "data": {"result": [{"values": [[1, value], [2, value]]}]}}

    def query_instant(self, cluster_id, query, time):
        value = "1000" if "tidb_ruv2_ru_total" in query else "999"
        return {"status": "success", "data": {"result": [{"value": [1, value]}]}}


def run_premium():
    registry = load_registry()
    selection = select_checks(
        registry, SelectionRequest("Premium", "health", "individual_cluster", None, LOGICAL_CAPS)
    )
    window = resolve_window("2026-11-19T00:00:00+00:00", "2026-11-20T00:00:00+00:00", None, 300)
    ctx = CollectContext(
        api=FakeAPI(), cluster_id="123", window=window, plan="Premium", metadata={"name": "example"}, rules=load_rules()
    )
    payload = collect(ctx, selection, load_recipes())
    return selection, payload


class LogicalCollectionTests(unittest.TestCase):
    def test_collects_ru(self):
        _, payload = run_premium()
        by_id = {item["check_id"]: item for item in payload["check_results"]}
        self.assertEqual(by_id["RCU-01"]["status"], "HEALTHY")
        self.assertIsNotNone(by_id["RCU-01"]["evidence"]["window_total_ru"])

    def test_every_runnable_check_has_a_recipe(self):
        selection, payload = run_premium()
        produced = {item["check_id"] for item in payload["check_results"]}
        self.assertEqual(set(selection["runnable_check_ids"]), produced)
        for item in payload["check_results"]:
            self.assertNotEqual(item["reason"], "no_collection_recipe")

    def test_logical_path_never_queries_physical_metrics(self):
        selection, _ = run_premium()
        recipes = load_recipes()
        prohibited = (
            "tikv_engine_size_bytes",
            "pd_scheduler_store_status",
            "node_disk_",
            "tikv_raftstore_",
            "tikv_storage_",
            "tikv_engine_compaction",
            "process_resident_memory_bytes",
            "process_cpu_seconds_total",
        )
        for check_id in selection["runnable_check_ids"]:
            params = recipes.get(check_id, {}).get("params", {})
            blob = " ".join(
                str(params.get(key, "")) for key in ("query", "numerator", "denominator", "rate_query", "window_total_query", "cross_check_query")
            )
            for token in prohibited:
                self.assertNotIn(token, blob, f"{check_id} must not query {token}")


if __name__ == "__main__":
    unittest.main()
