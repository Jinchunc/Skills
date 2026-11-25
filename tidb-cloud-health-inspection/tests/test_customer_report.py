import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from customer_report import render_health_report, validate_customer_report


def stats(value):
    return {"samples": 10, "min": value, "avg": value, "p50": value, "p95": value, "p99": value, "p999": value, "max": value}


# Shared plans (Starter/Essential/Premium) run on a shared resource pool, so
# §6 可靠性 and §7 配置与功能特性 are omitted → 7 dynamically-numbered sections.
LOGICAL_SECTIONS_ZH = [
    "## 1. 执行摘要", "## 2. 基础信息与基线", "## 3. 工作负载健康", "## 4. 资源健康",
    "## 5. SQL 分析", "## 6. 发现与优先建议", "## 7. 巡检覆盖",
]

# Dedicated/BYOC own their infrastructure → full 9-section skeleton.
DEDICATED_SECTIONS_ZH = [
    "## 1. 执行摘要", "## 2. 基础信息与基线", "## 3. 工作负载健康", "## 4. 资源健康",
    "## 5. SQL 分析", "## 6. 可靠性", "## 7. 配置与功能特性", "## 8. 发现与优先建议", "## 9. 巡检覆盖",
]


class CustomerReportTests(unittest.TestCase):
    def payload(self):
        return {
            "cluster_id": "10260745576873721799",
            "cluster_name": "example",
            "plan": "Premium",
            "status": "NOTICE",
            "confidence": "MEDIUM",
            "metadata": {"provider": "aws", "region": "ap-south-1", "version": "v1"},
            "window": {"start": "2026-11-13T00:00:00+00:00", "end": "2026-11-20T00:00:00+00:00"},
            "check_results": [
                {"check_id": "SQL-01", "status": "HEALTHY", "evidence": {"statistics": stats(1)}},
                {"check_id": "SQL-02", "status": "NOTICE", "evidence": {"statistics_seconds": stats(0.01)}},
                {"check_id": "SQL-03", "status": "NOTICE", "evidence": {"statistics_seconds": stats(0.02)}},
                {"check_id": "SQL-05", "status": "HEALTHY", "evidence": {"estimated_error_count": 1, "window_average_ratio_percent": 0.001}},
                {"check_id": "RCU-01", "status": "HEALTHY", "evidence": {"statistics_ru_per_second": stats(100), "window_total_ru": 1000}},
                {"check_id": "RCU-05", "status": "HEALTHY", "evidence": {"statistics_seconds": stats(0.001)}},
            ],
        }

    def dedicated_payload(self):
        # Evidence intentionally carries internal identifiers to prove they never surface.
        return {
            "cluster_id": "C9",
            "cluster_name": "ded-demo",
            "plan": "Dedicated",
            "status": "WARNING",
            "confidence": "MEDIUM",
            "lang": "en",
            "metadata": {"provider": "aws", "region": "ap-south-1", "version": "v8.1.0"},
            "window": {"start": "2026-11-20T00:00:00+00:00", "end": "2026-11-21T00:00:00+00:00"},
            "check_results": [
                {"check_id": "TIKV-01", "status": "WARNING", "evidence": {"cpu_max_by_instance": {"basic-tikv-0": 95.0}}},
                {"check_id": "TIKV-02", "status": "HEALTHY", "evidence": {"memory_max_by_instance": {"basic-tikv-0": 50.0}}},
                {"check_id": "INF-03", "status": "N/A", "evidence": {}},
            ],
        }

    # ── skeleton: shared = 7 sections, dedicated = 9 (same ordering, dynamic numbering) ──
    def test_logical_uses_shared_skeleton(self):
        report = render_health_report(self.payload())
        for section in LOGICAL_SECTIONS_ZH:
            self.assertIn(section, report)
        # Shared resource pool → no reliability / config sections.
        self.assertNotIn("可靠性", report)
        self.assertNotIn("配置与功能特性", report)
        for marker in ("本周期", "上周期", "趋势", "SQL 分析"):
            self.assertIn(marker, report)

    def test_dedicated_uses_full_skeleton(self):
        payload = self.dedicated_payload()
        payload.pop("lang")  # Chinese default
        report = render_health_report(payload)
        for section in DEDICATED_SECTIONS_ZH:
            self.assertIn(section, report)

    def test_report_hides_internal_ids(self):
        report = render_health_report(self.payload())
        for internal in ("SQL-02", "SQL-03", "RCU-01", "RCU-05", "Checklist Signature", "Registry Version"):
            self.assertNotIn(internal, report)

    def test_owner_sql_topn_allowed_but_raw_text_withheld(self):
        payload = self.payload()
        payload["previous_period"] = {"SQL-01": {"check_id": "SQL-01", "evidence": {"statistics": stats(0.5)}}}
        payload["check_results"].append({
            "check_id": "SQLD-01", "status": "HEALTHY",
            "evidence": {"mode": "ru", "rows": [{"digest": "a1b2c3d4", "ru": 100, "sql_text": "SELECT sum(x) FROM t"}]}},
        )
        report = render_health_report(payload)  # validator must not raise
        self.assertIn("消耗 RU Top 10", report)
        self.assertIn("a1b2c3d4", report)            # owner-facing digest allowed
        self.assertNotIn("SELECT sum", report)       # raw SQL text is never rendered
        self.assertRegex(report, r"Max [+-]\d")       # previous-period trend computed

    def test_english_locale(self):
        payload = self.payload()
        payload["lang"] = "en"
        report = render_health_report(payload)
        self.assertIn("Workload Health", report)
        self.assertIn("Current period", report)
        self.assertNotIn("工作负载健康", report)

    def test_validator_rejects_internal_terms(self):
        with self.assertRaisesRegex(ValueError, "internal terminology"):
            validate_customer_report("Finding SQL-02")

    def test_validator_rejects_urls_and_promql(self):
        with self.assertRaisesRegex(ValueError, "URL"):
            validate_customer_report("See https://clinic.pingcap.com/grafana")
        with self.assertRaisesRegex(ValueError, "query/infrastructure"):
            validate_customer_report("value sum(rate(tikv_cpu[5m]))")

    def test_validator_rejects_any_internal_check_id(self):
        with self.assertRaisesRegex(ValueError, "check ID"):
            validate_customer_report("Area TIKV-06 is warning")

    def test_validator_allows_customer_cluster_name(self):
        validate_customer_report("# Report for PROD-01\n\nHealthy.")

    # ── dedicated specifics inside the unified skeleton ──
    def test_dedicated_resource_tables_and_safety(self):
        report = render_health_report(self.dedicated_payload())
        validate_customer_report(report)  # must not raise
        self.assertIn("Resource Health", report)          # §4
        self.assertIn("Storage node CPU", report)          # TIKV-01 (WARNING) in §4.2
        self.assertIn("Storage node memory", report)       # TIKV-02 (HEALTHY) in §4.2
        self.assertIn("WARNING", report)
        for leaked in ("basic-tikv-0", "TIKV-01", "cpu_max_by_instance", "http"):
            self.assertNotIn(leaked, report)

    def test_dedicated_unbound_sections_render_slots(self):
        report = render_health_report(self.dedicated_payload())
        # §6/§7 have no bound collectors yet → a pending/interpretation slot, not fabricated content.
        self.assertIn("interpretation pending", report)

    def test_interpretation_slots_injected_when_present(self):
        payload = self.payload()
        payload["interpretations"] = {
            "exec_summary": "总体平稳，高峰触及 RCU 上限。",
            "key_risks": "高峰 RCU 饱和是唯一实质风险。",
            "recommendations": "评估上调 RCU 或削峰。",
        }
        report = render_health_report(payload)
        self.assertIn("高峰触及 RCU 上限", report)
        self.assertIn("唯一实质风险", report)
        self.assertIn("评估上调 RCU 或削峰", report)
        self.assertNotIn("待分析解读", report)  # placeholder replaced

    def test_interpretation_falls_back_to_pending_placeholder(self):
        report = render_health_report(self.payload())  # no interpretations
        self.assertIn("待分析解读", report)  # honest pending slot, never fabricated

    def test_chart_ref_emitted_only_when_series_present(self):
        payload = self.payload()
        report = render_health_report(payload)
        self.assertNotIn("charts/qps.png", report)  # no series → no chart ref
        for r in payload["check_results"]:
            if r["check_id"] == "SQL-01":
                r["evidence"]["series"] = [[1.0, 10.0], [2.0, 20.0]]
        report2 = render_health_report(payload)
        self.assertIn("![", report2)
        self.assertIn("charts/qps.png", report2)
        validate_customer_report(report2)  # image ref must pass the privacy validator

    def test_slow_log_sampled_label(self):
        payload = self.payload()
        payload["check_results"].append({
            "check_id": "SQL-06", "status": "HEALTHY",
            "evidence": {"window_total": 50, "unique_digests": 12, "sampled": True, "sample_capped": True}})
        report = render_health_report(payload)
        self.assertIn("抽样", report)
        self.assertIn("≥50", report)  # capped sample shown as a lower bound

    def test_dedicated_component_labels_localized(self):
        payload = self.dedicated_payload()
        payload.pop("lang")  # Chinese default
        zh = render_health_report(payload)
        self.assertIn("存储节点 CPU", zh)       # TIKV-01 localized
        self.assertIn("存储节点内存", zh)        # TIKV-02 localized
        self.assertNotIn("Storage node CPU", zh)  # English not leaked into zh report
        en = render_health_report(self.dedicated_payload())  # lang=en
        self.assertIn("Storage node CPU", en)
        self.assertNotIn("存储节点 CPU", en)


if __name__ == "__main__":
    unittest.main()
