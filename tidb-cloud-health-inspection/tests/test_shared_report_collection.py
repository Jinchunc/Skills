"""Coverage for the shared-plan report additions: owner-facing SQL Top-N,
logical-storage and slow-log adapters, and the two-window comparison engine."""
import argparse
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from collection_adapters import CollectContext, logical_storage_usage, slow_log_count, sql_topn
from inspection_core import resolve_window


def _ctx(api):
    window = resolve_window("2026-11-19T00:00:00+00:00", "2026-11-20T00:00:00+00:00", None, 300)
    return CollectContext(api=api, cluster_id="c1", window=window, plan="Premium", metadata={}, rules={})


class _TopNAPI:
    def top_ru_summary(self, cid, start, end, top, **kw):
        return {"items": [{"digest": "aa11", "ru": 500}, {"digest": "bb22", "ru": 100}]}

    def ngm_slow_query_list(self, cid, begin, end, limit=100, order_by="query_time", **kw):
        return [{"digest": "cc33", "count": 9, "query_time": 1.2}]


class _RangeAPI:
    def __init__(self, values, instant="174461"):
        self.values = values
        self.instant = instant

    def query_range(self, cid, q, s, e, step):
        return {"status": "success", "data": {"result": [{"values": [[i, str(v)] for i, v in enumerate(self.values)]}]}}

    def query_instant(self, cid, q, t):
        return {"status": "success", "data": {"result": [{"value": [1, self.instant]}]}}


class SQLTopNAdapterTests(unittest.TestCase):
    def test_ru_topn_rows_owner_scoped(self):
        r = sql_topn(_ctx(_TopNAPI()), "SQLD-01", {"mode": "ru", "top": 10})
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(len(r["evidence"]["rows"]), 2)
        self.assertEqual(r["evidence"]["audience"], "owner")

    def test_slow_modes(self):
        for mode in ("slow_count", "slow_duration"):
            r = sql_topn(_ctx(_TopNAPI()), "SQLD-02", {"mode": mode})
            self.assertEqual(r["status"], "HEALTHY")

    def test_empty_source_is_na(self):
        class Empty:
            def top_ru_summary(self, *a, **k):
                return {"items": []}

        r = sql_topn(_ctx(Empty()), "SQLD-01", {"mode": "ru"})
        self.assertEqual((r["status"], r["reason"]), ("N/A", "sql_topn_empty"))

    def test_unknown_mode_is_na(self):
        r = sql_topn(_ctx(_TopNAPI()), "SQLD-01", {"mode": "bogus"})
        self.assertEqual(r["status"], "N/A")

    def test_source_error_degrades_not_crashes(self):
        class Boom:
            def top_ru_summary(self, *a, **k):
                raise RuntimeError("network down")

        r = sql_topn(_ctx(Boom()), "SQLD-01", {"mode": "ru"})
        self.assertEqual((r["status"], r["reason"]), ("N/A", "sql_topn_source_error"))


class StorageAndSlowLogTests(unittest.TestCase):
    def test_storage_start_end_growth(self):
        r = logical_storage_usage(_ctx(_RangeAPI([100, 120, 150])), "LST-01", {"query": "logical_bytes", "unit": "GiB"})
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["evidence"]["start"], 100.0)
        self.assertEqual(r["evidence"]["end"], 150.0)
        self.assertEqual(r["evidence"]["growth"], 50.0)

    def test_storage_no_query_is_na(self):
        r = logical_storage_usage(_ctx(_RangeAPI([])), "LST-01", {})
        self.assertEqual((r["status"], r["reason"]), ("N/A", "logical_storage_metric_not_configured"))

    def test_slow_log_count_from_sql(self):
        class SqlAPI:
            def query_sql(self, cid, sql, timeout=120):
                assert "slow_query_logs" in sql and "date IN" in sql
                return {"columns": ["c"], "rows": [["4343"]]}

        r = slow_log_count(_ctx(SqlAPI()), "SQL-06", {})
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["evidence"]["window_total"], 4343)

    def test_slow_log_source_error_is_na(self):
        class Boom:
            def query_sql(self, *a, **k):
                raise RuntimeError("data proxy down")

        r = slow_log_count(_ctx(Boom()), "SQL-06", {})
        self.assertEqual((r["status"], r["reason"]), ("N/A", "slow_log_source_error"))

    def test_slow_log_ngm_fallback_when_data_proxy_absent(self):
        # Essential/shared: Data Proxy has no slow_query_logs table (error response);
        # fall back to NGM bounded sampling, labelled as sampled.
        class NoProxyNGM:
            def query_sql(self, cid, sql, timeout=120):
                return {"error": "FailedPrecondition: data source not configured"}

            def ngm_slow_query_list(self, cid, begin, end, limit=100, **kw):
                return [{"digest": "a", "query_time": 2.0}, {"digest": "a", "query_time": 1.5},
                        {"digest": "b", "query_time": 1.1}]

        r = slow_log_count(_ctx(NoProxyNGM()), "SQL-06", {})
        self.assertEqual(r["status"], "HEALTHY")
        self.assertTrue(r["evidence"]["sampled"])
        self.assertEqual(r["evidence"]["window_total"], 3)
        self.assertEqual(r["evidence"]["unique_digests"], 2)
        self.assertEqual(r["evidence"]["source"], "ngm_slow_query_list")

    def test_slow_log_ngm_capped_marks_lower_bound(self):
        class Capped:
            def query_sql(self, *a, **k):
                raise RuntimeError("no table")

            def ngm_slow_query_list(self, cid, begin, end, limit=100, **kw):
                return [{"digest": str(i), "query_time": 1.0} for i in range(limit)]

        r = slow_log_count(_ctx(Capped()), "SQL-06", {"ngm_limit": 50})
        self.assertEqual(r["status"], "HEALTHY")
        self.assertTrue(r["evidence"]["sample_capped"])
        self.assertEqual(r["evidence"]["window_total"], 50)


class _CompareAPI:
    """query_range returns a different QPS level depending on the window so the
    previous-period trend is non-trivial."""
    def query_range(self, cid, q, start, end, step):
        value = "10" if start >= "2026-11-19" else "5"
        if "tidb_server_query_total" in q:
            return {"status": "success", "data": {"result": [{"values": [[1, value], [2, value]]}]}}
        return {"status": "success", "data": {"result": [{"values": [[1, "0.001"]]}]}}

    def query_instant(self, cid, q, t):
        return {"status": "success", "data": {"result": [{"value": [1, "1000"]}]}}


class ComparisonEngineTests(unittest.TestCase):
    def test_collect_logical_attaches_previous_period_and_lang(self):
        from health_inspection import collect_logical

        args = argparse.Namespace(
            start="2026-11-19T00:00:00+00:00", end="2026-11-20T00:00:00+00:00",
            relative_window=None, step_seconds=300, registry=None, profile="health", lang="zh",
        )
        raw, normalized, report = collect_logical(_CompareAPI(), {"name": "demo"}, "123", "Premium", args)
        self.assertEqual(normalized["lang"], "zh")
        self.assertIn("previous_period", normalized)
        self.assertIn("工作负载健康", report)
        # QPS doubled vs the previous window → a positive trend is rendered.
        self.assertRegex(report, r"Max \+100\.00%")


if __name__ == "__main__":
    unittest.main()
