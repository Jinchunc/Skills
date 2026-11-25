import json
import os
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
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


class WritePathOverrideTests(unittest.TestCase):
    """Declaring the Feishu write path at invocation (CLI overrides over the config)."""

    def _args(self, **kw):
        base = dict(parent_token=None, parent_type=None, token_env=None,
                    doc_title_template=None, group_by_org=None, identity=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_parent_token_override_wins_over_env_and_config(self):
        os.environ["FEISHU_PARENT_TOKEN"] = "env_tok"
        try:
            cfg = pr._apply_overrides(CFG, self._args(parent_token="cli_tok", parent_type="folder"))
            self.assertEqual(cfg["parent"]["token"], "cli_tok")
            self.assertEqual(cfg["parent"]["type"], "folder")
            # _resolve_parent_token prefers the (now CLI-set) token over the env var.
            self.assertEqual(pr._resolve_parent_token(cfg["parent"]), "cli_tok")
        finally:
            os.environ.pop("FEISHU_PARENT_TOKEN", None)

    def test_overrides_do_not_mutate_input_config(self):
        before = json.dumps(CFG, sort_keys=True)
        pr._apply_overrides(CFG, self._args(parent_token="x", group_by_org=False,
                                            doc_title_template="{cluster_name}", identity="bot"))
        self.assertEqual(json.dumps(CFG, sort_keys=True), before)  # original untouched

    def test_no_overrides_is_identity(self):
        cfg = pr._apply_overrides(CFG, self._args())
        self.assertEqual(cfg["parent"]["token_env"], "FEISHU_PARENT_TOKEN")
        self.assertTrue(cfg["structure"]["group_by_org"])

    def test_pure_cli_without_config_file(self):
        # Missing config file → {}; the write path comes entirely from the CLI.
        cfg = pr._apply_overrides(pr._load_config("/nonexistent/delivery.json"),
                                  self._args(parent_token="only_cli", parent_type="wiki"))
        self.assertEqual(cfg["parent"]["token"], "only_cli")
        self.assertEqual(pr._resolve_parent_token(cfg["parent"]), "only_cli")

    def test_group_by_org_can_be_disabled_at_invocation(self):
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d) / "rep"
            _make_report(rd)
            cfg = pr._apply_overrides(CFG, self._args(parent_token="t", group_by_org=False))
            plan = pr.publish(rd, cfg, org="bitunix", title_override=None, dry_run=True)
            self.assertIsNone(plan["org_container"])  # grouping off despite an org


if __name__ == "__main__":
    unittest.main()
