import json
import os
import unittest
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import publish_report as pr


def _make_report(d: Path):
    (d / "charts").mkdir(parents=True, exist_ok=True)
    (d / "normalized.json").write_text(json.dumps({
        "cluster_id": "C1", "cluster_name": "demo-cluster", "plan": "Essential",
        "window": {"start": "2026-11-17T00:00:00+00:00", "end": "2026-11-24T00:00:00+00:00"},
    }), encoding="utf-8")
    (d / "health-report.md").write_text(
        "# R\n\n![QPS](charts/qps.png)\n\n![RCU](charts/rcu.png)\n", encoding="utf-8")


CFG = {
    "identity": "user", "brand": "feishu",
    "parent": {"type": "wiki", "token": "", "token_env": "FEISHU_PARENT_TOKEN"},
    "structure": {"group_by_org": True, "org_title_template": "{org}",
                  "doc_title_template": "{cluster_name} 巡检报告 {date}"},
}


class PublishReportTests(unittest.TestCase):
    def test_dry_run_plan_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d) / "rep"
            _make_report(rd)
            os.environ["FEISHU_PARENT_TOKEN"] = "wiki_root_tok"
            try:
                plan = pr.publish(rd, CFG, org="bitunix", title_override=None, dry_run=True)
            finally:
                os.environ.pop("FEISHU_PARENT_TOKEN", None)
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["root_token"], "wiki_root_tok")
            self.assertEqual(plan["doc_title"], "demo-cluster 巡检报告 2026-11-24")
            self.assertEqual(plan["org_container"], "bitunix")
            self.assertEqual(plan["charts"], 2)

    def test_title_override(self):
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d) / "rep"
            _make_report(rd)
            os.environ["FEISHU_PARENT_TOKEN"] = "t"
            try:
                plan = pr.publish(rd, CFG, org=None, title_override="自定义标题", dry_run=True)
            finally:
                os.environ.pop("FEISHU_PARENT_TOKEN", None)
            self.assertEqual(plan["doc_title"], "自定义标题")
            self.assertIsNone(plan["org_container"])  # no org → no grouping

    def test_missing_token_raises(self):
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d) / "rep"
            _make_report(rd)
            os.environ.pop("FEISHU_PARENT_TOKEN", None)
            with self.assertRaisesRegex(RuntimeError, "no parent token"):
                pr.publish(rd, CFG, org=None, title_override=None, dry_run=True)

    def test_chart_path_rewrite_is_cwd_relative(self):
        md = "![a](charts/qps.png) and ![b](charts/rcu.png)"
        out = pr._rewrite_chart_paths(md, Path.cwd() / "output" / "run")
        self.assertIn("](@./output/run/charts/qps.png)", out)
        self.assertIn("](@./output/run/charts/rcu.png)", out)


if __name__ == "__main__":
    unittest.main()
