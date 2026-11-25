#!/usr/bin/env python3
"""Publish a rendered health report to Feishu/Lark via the global `lark-cli`,
driven by config/delivery.v1.json so the destination is configurable and the skill
stays portable across environments.

What is configurable (config/delivery.v1.json):
  - the root parent (Wiki node or Drive folder) — token comes from an env var
    (parent.token_env, default FEISHU_PARENT_TOKEN) or parent.token; nothing
    environment-specific is committed;
  - whether to create a per-org container under the root (structure.group_by_org);
  - the org-container and per-inspection document titles (templates).

Publishing uses the user's own lark-cli identity (`--as user`); markdown import
embeds the report's `@./…/charts/*.png` locally, so text + tables + charts go up in
one call. Nothing here stores credentials — lark-cli holds them in the OS keychain.

Usage:
  publish_report.py --report-dir output/<dir> [--org <org>] [--dry-run]
                    [--config config/delivery.v1.json] [--title <override>]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from customer_report import validate_customer_report  # noqa: E402

DEFAULT_CONFIG = "config/delivery.v1.json"
_CHART_REF_RE = re.compile(r"\]\(charts/")


def _run(cmd: list[str], stdin: str | None = None) -> dict:
    """Run a lark-cli command and parse its JSON stdout (raises on error)."""
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}… failed: {(proc.stderr or proc.stdout)[:400]}")
    out = (proc.stdout or "").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out}


def _load_config(path: str) -> dict:
    """Load the feishu delivery block from the config file. A missing file yields {}
    so the write path can be declared purely at invocation (CLI overrides below)."""
    p = Path(path)
    if not p.exists():
        return {}
    cfg = json.loads(p.read_text(encoding="utf-8"))
    return cfg.get("feishu") or {}


def _apply_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    """Overlay invocation-time CLI flags onto the delivery config so the Feishu write
    path (destination token/type, grouping, title) can be declared at call time
    without editing the config file. Only flags that were given take effect; the rest
    fall back to the config. ``--parent-token`` wins over both the config token and the
    env var. Returns a copy — the loaded config is never mutated."""
    cfg = json.loads(json.dumps(cfg))  # deep copy
    parent = cfg.setdefault("parent", {})
    structure = cfg.setdefault("structure", {})
    if args.parent_token is not None:
        parent["token"] = args.parent_token
    if args.parent_type is not None:
        parent["type"] = args.parent_type
    if args.token_env is not None:
        parent["token_env"] = args.token_env
    if args.doc_title_template is not None:
        structure["doc_title_template"] = args.doc_title_template
    if args.group_by_org is not None:
        structure["group_by_org"] = args.group_by_org
    if args.identity is not None:
        cfg["identity"] = args.identity
    return cfg


def _resolve_parent_token(parent: dict) -> str:
    token = (parent.get("token") or "").strip()
    if token:
        return token
    env_name = parent.get("token_env") or "FEISHU_PARENT_TOKEN"
    token = (os.environ.get(env_name) or "").strip()
    if not token:
        raise RuntimeError(
            f"no parent token: set '{env_name}' in this environment or fill "
            f"feishu.parent.token in the delivery config")
    return token


def _report_meta(report_dir: Path) -> dict:
    norm = json.loads((report_dir / "normalized.json").read_text(encoding="utf-8"))
    window = norm.get("window") or {}
    end = str(window.get("end") or "")
    return {
        "cluster_name": norm.get("cluster_name") or norm.get("cluster_id") or "cluster",
        "cluster_id": norm.get("cluster_id") or "",
        "plan": norm.get("plan") or "",
        "date": end[:10] if len(end) >= 10 else "",
    }


def _rewrite_chart_paths(markdown: str, report_dir: Path) -> str:
    """charts/x.png → @./<cwd-relative report_dir>/charts/x.png for lark-cli
    (local images must be referenced relative to the current working directory)."""
    rel = os.path.relpath(report_dir, Path.cwd())
    prefix = f"](@./{Path(rel).as_posix()}/charts/"
    return _CHART_REF_RE.sub(prefix, markdown)


def _find_wiki_child(parent_token: str, title: str, identity: str) -> str | None:
    """Return the node_token of a direct child of parent_token titled `title`."""
    res = _run(["lark-cli", "wiki", "+node-list", "--as", identity,
                "--parent-node-token", parent_token, "--page-all"])
    data = res.get("data") or res
    for item in data.get("items") or []:
        if item.get("title") == title:
            return item.get("node_token")
    return None


def _ensure_org_node(parent_token: str, parent_type: str, org_title: str,
                     identity: str, dry_run: bool) -> str:
    """Find-or-create a per-org container under the root, returning its token
    (idempotent for wiki: an existing child with the same title is reused).
    Wiki → a child node titled org_title; Drive → a subfolder named org_title."""
    if dry_run:
        return f"<org-node:{org_title}>"
    if parent_type == "wiki":
        existing = _find_wiki_child(parent_token, org_title, identity)
        if existing:
            return existing
        node = _run(["lark-cli", "wiki", "+node-create", "--as", identity,
                     "--parent-node-token", parent_token, "--title", org_title,
                     "--obj-type", "docx"])
        data = node.get("data") or node
        return (data.get("node") or {}).get("node_token") or data.get("node_token")
    folder = _run(["lark-cli", "drive", "+create-folder", "--as", identity,
                   "--folder-token", parent_token, "--name", org_title])
    data = folder.get("data") or folder
    return data.get("token")


def publish(report_dir: Path, cfg: dict, org: str | None, title_override: str | None,
            dry_run: bool) -> dict:
    parent = cfg.get("parent") or {}
    structure = cfg.get("structure") or {}
    identity = cfg.get("identity") or "user"
    parent_type = parent.get("type") or "wiki"
    root_token = _resolve_parent_token(parent)
    meta = _report_meta(report_dir)

    title = title_override or structure.get("doc_title_template", "{cluster_name} {date}").format(
        cluster_name=meta["cluster_name"], date=meta["date"], plan=meta["plan"],
        cluster_id=meta["cluster_id"], org=org or "")

    target_token = root_token
    org_title = None
    if structure.get("group_by_org") and org:
        org_title = structure.get("org_title_template", "{org}").format(org=org)
        target_token = _ensure_org_node(root_token, parent_type, org_title, identity, dry_run)

    report_md = report_dir / "health-report.md"
    raw_md = report_md.read_text(encoding="utf-8")
    # Defense-in-depth: re-run the privacy gate on exactly what we are about to
    # publish (the file may have been hand-edited or come from a non-standard run).
    validate_customer_report(raw_md)
    content = _rewrite_chart_paths(raw_md, report_dir)
    create_cmd = ["lark-cli", "docs", "+create", "--as", identity,
                  "--doc-format", "markdown", "--title", title,
                  "--parent-token", str(target_token), "--content", "-"]

    plan = {
        "report_dir": str(report_dir), "parent_type": parent_type,
        "root_token": root_token, "org": org, "org_container": org_title,
        "target_token": str(target_token), "doc_title": title,
        "charts": len(_CHART_REF_RE.findall(raw_md)),
        "command": " ".join(create_cmd[:-1]) + " - < (rewritten markdown)",
    }
    if dry_run:
        plan["dry_run"] = True
        return plan
    res = _run(create_cmd, stdin=content)
    data = res.get("data") or res
    plan["url"] = data.get("url")
    plan["document_id"] = data.get("document_id") or data.get("document", {}).get("document_id")
    return plan


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-dir", required=True)
    ap.add_argument("--org")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--title")
    ap.add_argument("--dry-run", action="store_true")
    # Invocation-time Feishu write-path overrides (take precedence over --config).
    ap.add_argument("--parent-token",
                    help="Feishu destination token (Wiki node or Drive folder). Declares the "
                         "write path at call time; wins over the config token and the env var.")
    ap.add_argument("--parent-type", choices=["wiki", "folder"],
                    help="Destination type (wiki node or drive folder); overrides config.")
    ap.add_argument("--token-env",
                    help="Name of the env var holding the parent token; overrides config.")
    ap.add_argument("--doc-title-template",
                    help="Document title template (fields: {cluster_name} {date} {plan} "
                         "{cluster_id} {org}); overrides config.")
    ap.add_argument("--group-by-org", action=argparse.BooleanOptionalAction, default=None,
                    help="Create a per-org container under the parent (or --no-group-by-org); overrides config.")
    ap.add_argument("--identity", choices=["user", "bot"],
                    help="lark-cli identity; overrides config (default: user).")
    args = ap.parse_args(argv)
    cfg = _apply_overrides(_load_config(args.config), args)
    try:
        result = publish(Path(args.report_dir), cfg, args.org, args.title, args.dry_run)
    except (RuntimeError, FileNotFoundError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
