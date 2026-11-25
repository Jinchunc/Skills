#!/usr/bin/env python3
"""Single deterministic entry point for TiDB Cloud health inspection."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from checklist_engine import evaluate
from checklist_registry import SelectionRequest, load_registry, select_checks
from clinic_api import ClinicAPI
from collection_adapters import CollectContext, collect, load_recipes, load_rules
from customer_report import render_health_report, validate_customer_report
from handoff import build_handoff
import report_charts
from inspection_core import LOGICAL_PLANS, TimeWindow, normalize_plan, resolve_window

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def plan_capabilities(registry: dict[str, Any], plan: str) -> frozenset[str]:
    """Capability aliases granted to a Plan — which component families it may probe,
    plus any per-check grants (e.g. ``check.lst-03``: the runner always collects an
    equal-duration previous window, so LST-03's comparable-window gate is satisfied).
    Read from checklist.v1.json's ``plan_capabilities`` map: data-driven, so adjusting
    a Plan's probe scope needs no code change. Unknown/missing Plan → empty (deny)."""
    caps = (registry.get("plan_capabilities") or {}).get(plan) or []
    return frozenset(caps)


def _fetch_cluster_detail(api: ClinicAPI, cluster_id: str) -> dict[str, Any]:
    """Product-exposed RCU Min/Max from the Clinic cluster-detail endpoint (matches
    the portal). Resolves the Clinic org via search, then reads minRCU/maxRCU.
    Fail-closed: {} on any error.

    NOTE: the same detail carries dataSizeUsed/dataSizeLimit, but for this cluster
    they read 0 / 50 GiB — inconsistent with the real logical size (hundreds of
    GiB), so they are NOT used for the storage row. Logical storage still needs the
    keyspace-metric aggregation decision (see live-validation-checklist A4)."""
    out: dict[str, Any] = {}
    try:
        search = api.search_cluster_by_id(str(cluster_id), show_deleted=True)
        items = search.get("items") if isinstance(search, dict) else search
        item = next((x for x in (items or []) if str(x.get("clusterID")) == str(cluster_id)), None)
        org = item.get("orgID") if isinstance(item, dict) else None
        if not org:
            return out
        detail = api.legacy_get_cluster(str(org), str(cluster_id))
        if isinstance(detail, dict) and not detail.get("error"):
            for src, dst in (("minRCU", "rcu_min"), ("maxRCU", "rcu_max")):
                value = detail.get(src)
                if isinstance(value, (int, float)):
                    out[dst] = value
            # §7 authorized feature inventory (OVR-06).
            gates = detail.get("featureGates")
            if isinstance(gates, dict) and gates:
                out["feature_gates"] = gates
    except Exception:
        pass
    return out


def _fetch_stats_unhealthy(api: ClinicAPI, cluster_id: str, window: TimeWindow) -> int | None:
    """Count of tables with statistics health in [0,50) (needs ANALYZE), from the
    tidb_statistics_stats_healthy gauge. Max across TiDB instances (conservative).
    None on any error."""
    try:
        resp = api.query_instant(
            str(cluster_id), 'max(tidb_statistics_stats_healthy{type="[0,50)"})', window.end_iso
        )
        return int(float(resp["data"]["result"][0]["value"][1]))
    except Exception:
        return None


def collect_logical(
    api: ClinicAPI,
    metadata: dict[str, Any],
    cluster_id: str,
    plan: str,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    window = resolve_window(args.start, args.end, args.relative_window, args.step_seconds)
    registry = load_registry(args.registry) if args.registry else load_registry()
    selection = select_checks(
        registry,
        SelectionRequest(
            plan=plan,
            profile=args.profile,
            target_scope="individual_cluster",
            component=None,
            capabilities=plan_capabilities(registry, plan),
        ),
    )
    recipes = load_recipes()
    rules = load_rules()

    def _collect(win: TimeWindow) -> dict[str, Any]:
        ctx = CollectContext(
            api=api,
            cluster_id=str(cluster_id),
            window=win,
            plan=plan,
            metadata=metadata,
            rules=rules,
        )
        return collect(ctx, selection, recipes)

    raw = _collect(window)

    # Collect the equal-duration window immediately preceding the current one so
    # the report can show 本周期 / 上周期 / 趋势. A failed/empty previous window
    # yields trend N/A in the renderer, never a fabricated comparison.
    duration = window.end - window.start
    previous_window = TimeWindow(window.start - duration, window.start, window.step_seconds)
    try:
        previous_raw = _collect(previous_window)
    except Exception:  # fail-closed: no previous evidence → trend N/A
        previous_raw = None

    normalized = evaluate(raw, selection)
    normalized["lang"] = getattr(args, "lang", "zh")

    # Product-exposed cluster facts for the 基础信息 / 集群用量 rows: RCU Min/Max and
    # logical storage from the Clinic cluster-detail API, plus the stats-health count
    # from tidb_statistics_stats_healthy. All per-logical-cluster and product/tenant
    # scoped (not the shared resource pool). Fail-closed → the row renders N/A.
    detail = _fetch_cluster_detail(api, cluster_id)
    unhealthy = _fetch_stats_unhealthy(api, cluster_id, window)
    if unhealthy is not None:
        detail["stats_unhealthy_count"] = unhealthy
    if detail:
        metadata_out = dict(normalized.get("metadata") or {})
        metadata_out.update(detail)
        normalized["metadata"] = metadata_out

    if previous_raw is not None:
        normalized["previous_period"] = {
            item["check_id"]: item for item in previous_raw.get("check_results", [])
        }
        normalized["previous_window"] = previous_raw.get("window")
    report = render_health_report(normalized)
    return raw, normalized, report


_SEVERITY_RANK = {"N/A": -1, "HEALTHY": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3}


def worse_status(a: str, b: str) -> str:
    """Return the more severe of two statuses (N/A is least severe)."""
    return a if _SEVERITY_RANK.get(a, -1) >= _SEVERITY_RANK.get(b, -1) else b


def _fetch_plan_cache_hit_pct(api: ClinicAPI, cluster_id: str, window: TimeWindow) -> float | None:
    """§5.4 execution-plan health: TiDB plan-cache hit rate (%) at the window end, via
    the Clinic metrics API. Fail-closed → None (metadata row renders N/A). TiCDC
    changefeed health (CDC-01/02) is now collected natively by the recipe layer."""
    try:
        resp = api.query_instant(
            str(cluster_id),
            "100 * sum(rate(tidb_server_plan_cache_total[5m])) / "
            "(sum(rate(tidb_server_plan_cache_total[5m])) + sum(rate(tidb_server_plan_cache_miss_total[5m])))",
            window.end_iso,
        )
        res = (resp or {}).get("data", {}).get("result", []) if isinstance(resp, dict) else []
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


def _collect_workload(api: ClinicAPI, cluster_id: str, args: argparse.Namespace,
                      check_ids: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """Collect the tenant SQL workload (RED) metrics for §3 via the shared
    collection layer, over the current + previous equal-duration window. Reused by
    the dedicated path so its §3 matches the logical report. Fail-closed.
    Returns (current results, previous results keyed by check_id, previous window)."""
    window = resolve_window(args.start, args.end, args.relative_window, args.step_seconds)
    recipes = load_recipes()
    rules = load_rules()
    selection = {"runnable_check_ids": list(check_ids)}

    def _run(win: TimeWindow) -> dict[str, Any]:
        ctx = CollectContext(api=api, cluster_id=str(cluster_id), window=win, plan="", metadata={}, rules=rules)
        return collect(ctx, selection, recipes)

    current = _run(window)
    duration = window.end - window.start
    previous_window = TimeWindow(window.start - duration, window.start, window.step_seconds)
    try:
        previous = _run(previous_window)
    except Exception:
        previous = None
    cur_results = current.get("check_results", [])
    prev_keyed = {r["check_id"]: r for r in (previous.get("check_results", []) if previous else [])}
    prev_window = previous.get("window") if previous else None
    return cur_results, prev_keyed, prev_window


# Dedicated checks migrated from the monolith to native recipes/adapters (phase 2
# of references/dedicated-migration-plan.md). Their native result overrides the
# monolith's bound result (guardrail-verified to the same shape). Reversible:
# shrink this set to roll back a check to the monolith.
def native_dedicated_checks() -> tuple[str, ...]:
    """Dedicated checks the native recipe/adapter layer owns (overriding the monolith
    binding) — data-driven: every recipe in ``config/collection.v1.json`` flagged
    ``"dedicated_native": true``. Adding a native Dedicated check needs no code change,
    only its recipe + that flag. The env var NATIVE_DEDICATED_ADAPTER_CHECKS still
    overrides this at runtime (empty = pure monolith) for the golden guardrail."""
    return tuple(cid for cid, recipe in load_recipes().items() if recipe.get("dedicated_native"))


_DISK_DEVICE_RE_DEFAULT = r"nvme.*|xvd.*|sd.*|vd.*|dm-.*"


def _resolve_disk_device_regex(api: ClinicAPI, cluster_id: str) -> str:
    """TiKV data-disk device regex for node-disk→pod join queries. Resolves the
    storage-type-specific device when the cluster detail exposes it; otherwise the
    catch-all default. Ported from the retired dedicated monolith's device logic."""
    try:
        detail = api.get_cluster_by_id(cluster_id)
        storage_type = ""
        if isinstance(detail, dict):
            tikv = (detail.get("components") or {}).get("COMPONENT_TYPE_TIKV") or {}
            storage_type = str(tikv.get("storageType") or "").strip().lower()
        if storage_type == "plus":
            devices = ["nvme2n1"]
        elif storage_type in {"standard", "performance"}:
            devices = ["nvme2n1", "nvme3n1"]
        else:
            devices = []
        if not devices:
            return _DISK_DEVICE_RE_DEFAULT
        return "|".join(re.escape(device) for device in devices)
    except Exception:
        return _DISK_DEVICE_RE_DEFAULT


def _collect_native_dedicated(api: ClinicAPI, cluster_id: str, args: argparse.Namespace,
                              check_ids: list[str], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect ported dedicated component checks via the shared recipe/adapter layer
    (single current window). Returns {check_id: result}. Disk checks (INF-04/05) need
    the cluster's k8s namespace + device regex for the node-disk→pod join; metadata
    checks (OVR-01/02) read ctx.metadata — both are threaded through here."""
    window = resolve_window(args.start, args.end, args.relative_window, args.step_seconds)
    needs_disk = any(c in ("INF-04", "INF-05") for c in check_ids)
    substitutions = (
        {"tikv_disk_device_regex": _resolve_disk_device_regex(api, cluster_id)}
        if needs_disk else {})
    ctx = CollectContext(api=api, cluster_id=str(cluster_id), window=window, plan="",
                         metadata=metadata or {}, namespace=f"tidb{cluster_id}",
                         rules=load_rules(), substitutions=substitutions)
    out = collect(ctx, {"runnable_check_ids": list(check_ids)}, load_recipes())
    return {r["check_id"]: r for r in out.get("check_results", [])}


def collect_full_stack(
    metadata: dict[str, Any],
    cluster_id: str,
    plan: str,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Native full-stack collection for Dedicated/BYOC — no monolith (retired
    2026-11-25). Every check comes from the recipe/adapter layer: the 23 dedicated
    signals (data-driven native set; OVR/AVL read overview metadata) + SCOPE
    (full-stack authorized) + tenant SQL workload (shared layer, matches the logical
    report). Overview metadata comes from the Clinic cluster API. Overall status is the
    engine worst-of the checks; deep sustained-window / root-cause judgment is
    delegated to o11y via the handoff. Fail-closed: a collection failure leaves checks
    N/A (engine-synthesized), never a fabricated HEALTHY."""
    api = ClinicAPI(args.env_file)
    window = resolve_window(args.start, args.end, args.relative_window, args.step_seconds)
    registry = load_registry(args.registry) if args.registry else load_registry()
    selection = select_checks(
        registry,
        SelectionRequest(
            plan=plan,
            profile=args.profile,
            target_scope="individual_cluster",
            component=None,
            capabilities=plan_capabilities(registry, plan),
        ),
    )
    selected_ids = set(selection["selected_check_ids"])

    # Overview from the Clinic cluster API (native; replaces the monolith report).
    overview = {k: (metadata.get(k) or "") for k in ("provider", "region", "version", "status")}
    cluster_name = metadata.get("name") or str(cluster_id)

    workload_prev: dict[str, Any] = {}
    workload_prev_window: dict[str, Any] | None = None
    by_id: dict[str, dict[str, Any]] = {}
    try:
        # The 23 dedicated signals via the native recipe/adapter layer (data-driven
        # native set). collect() is per-check resilient → a bad metric yields N/A.
        native_ids = [c for c in native_dedicated_checks() if c in selected_ids]
        if native_ids:
            by_id.update(_collect_native_dedicated(api, cluster_id, args, native_ids, metadata=overview))
        # SCOPE: full-stack is authorized for Dedicated/BYOC (physical infra probed).
        for sid in ("SCOPE-01", "SCOPE-04", "SCOPE-08"):
            if sid in selected_ids:
                by_id.setdefault(sid, {
                    "check_id": sid, "status": "HEALTHY", "reason": "scope_resolved",
                    "evidence": {"full_stack_authorized": True, "physical_infrastructure_queried": True}})
        # §3 tenant SQL workload (RED) via the shared layer — matches the logical report.
        workload_ids = [c for c in ("SQL-01", "SQL-02", "SQL-03", "SQL-05", "SQL-06",
                                    "SQLD-01", "SQLD-02", "SQLD-03") if c in selected_ids]
        if workload_ids:
            try:
                wl_cur, workload_prev, workload_prev_window = _collect_workload(
                    api, cluster_id, args, workload_ids)
                for r in wl_cur:
                    by_id.setdefault(r["check_id"], r)
            except Exception:  # fail-closed: workload stays N/A, run continues
                workload_prev = {}
                workload_prev_window = None
    except Exception:  # fail-closed: engine synthesizes N/A for every selected check
        by_id = {}
    bound = list(by_id.values())

    md = dict(overview)
    # §7 authorized feature inventory + §5.4 plan-cache hit rate (both fail-closed).
    md.update({k: v for k, v in _fetch_cluster_detail(api, cluster_id).items() if k == "feature_gates"})
    plan_cache = _fetch_plan_cache_hit_pct(api, cluster_id, window)
    if plan_cache is not None:
        md["plan_cache_hit_pct"] = plan_cache

    raw = {
        "cluster_id": str(cluster_id),
        "cluster_name": cluster_name,
        "metadata": md,
        "plan": plan,
        "collector": "native_full_stack",
        "window": window.evidence(),
        "check_results": bound,
    }
    normalized = evaluate(raw, selection)
    normalized["overall_status_source"] = "engine_worst_of_checks"
    normalized["lang"] = getattr(args, "lang", "zh")
    if workload_prev:
        normalized["previous_period"] = workload_prev  # §3 workload trend
    if workload_prev_window:
        normalized["previous_window"] = workload_prev_window  # §3 period-range note
    customer_report = render_health_report(normalized)
    return raw, normalized, customer_report


def run_gate(
    normalized_path: Path,
    report_path: Path,
    cluster_id: str,
    plan: str,
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(SCRIPTS / "inspection_gate.py"),
        "--input",
        str(normalized_path),
        "--target",
        cluster_id,
        "--plan",
        plan,
        "--profile",
        args.profile,
        "--target-scope",
        "individual_cluster",
        "--state-dir",
        str(output_dir / "state"),
        "--artifact-dir",
        str(output_dir / "artifacts"),
        "--report",
        str(report_path),
    ]
    if args.registry:
        command.extend(["--registry", args.registry])
    _gate_registry = load_registry(args.registry) if args.registry else load_registry()
    for capability in sorted(plan_capabilities(_gate_registry, plan) if plan in LOGICAL_PLANS else frozenset()):
        command.extend(["--capability", capability])
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"inspection gate failed: {completed.stderr.strip()[:2000]}")
    return json.loads(completed.stdout)


def validate_artifacts(
    raw: dict[str, Any],
    normalized: dict[str, Any],
    report: str,
    gate: dict[str, Any],
    plan: str,
) -> dict[str, Any]:
    validate_customer_report(report)
    checks = {
        "plan_consistent": raw.get("plan") == plan and normalized.get("plan") == plan,
        "window_present": isinstance(normalized.get("window"), dict),
        "status_present": normalized.get("status") in {"HEALTHY", "NOTICE", "WARNING", "CRITICAL", "N/A"},
        "confidence_present": normalized.get("confidence") in {"HIGH", "MEDIUM", "LOW"},
        "gate_consistent": gate.get("status") == normalized.get("status"),
        "customer_report_safe": True,
    }
    if plan in LOGICAL_PLANS:
        by_id = {item.get("check_id"): item for item in normalized.get("check_results", [])}
        valid_status = {"HEALTHY", "NOTICE", "WARNING", "CRITICAL", "N/A"}
        ru = by_id.get("RCU-01") or {}
        checks["ru_attempted"] = ru.get("status") in valid_status
        # A quiet cluster (empty latency histogram) is a legitimate N/A, not a
        # failure: gate on SQL-02 being attempted, not on a full percentile set.
        checks["sql_latency_attempted"] = (by_id.get("SQL-02") or {}).get("status") in valid_status
        checks["logical_scope_guard"] = all(
            not bool((item.get("evidence") or {}).get("physical_infrastructure_queried"))
            for item in normalized.get("check_results", [])
        )
    passed = all(checks.values())
    return {"passed": passed, "checks": checks}


def normalize_cluster_ids(values: list[str]) -> list[str]:
    """Flatten repeated/comma-separated --cluster-id values, de-duplicated, in order."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        for part in str(raw).split(","):
            cid = part.strip()
            if cid and cid not in seen:
                seen.add(cid)
                result.append(cid)
    return result


def inspect_one(api: ClinicAPI, cluster_id: str, args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    """Inspect a single cluster into ``output_dir`` and return its summary.

    Shared by the single-cluster and organization runners. Raises on failure; the
    caller decides whether to fail the whole run or continue.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = api.get_cluster_by_id(cluster_id)
    if not isinstance(metadata, dict) or metadata.get("error"):
        raise RuntimeError(f"cluster discovery failed: {metadata.get('error') if isinstance(metadata, dict) else metadata}")
    plan = normalize_plan(metadata)
    if plan == "Unknown":
        raise RuntimeError("cluster Plan could not be determined safely")
    if args.expected_plan and args.expected_plan != plan:
        raise RuntimeError(f"discovered Plan {plan} does not match --expected-plan {args.expected_plan}")

    if plan in LOGICAL_PLANS:
        raw, normalized, report = collect_logical(api, metadata, cluster_id, plan, args)
    else:
        raw, normalized, report = collect_full_stack(metadata, cluster_id, plan, args)

    # Privacy gate BEFORE anything touches disk: an unsafe customer report must not
    # be written (a later reader/publisher could pick it up). Raises on any leak.
    validate_customer_report(report)

    raw_path = output_dir / "collector.json"
    normalized_path = output_dir / "normalized.json"
    report_path = output_dir / "health-report.md"
    # Deep-analysis handoff: this skill collects + reports; per-component deep dives
    # are delegated to the external o11y skill via this routable contract.
    normalized["handoff"] = build_handoff(normalized)
    # Keep the bulky internal report out of collector.json; write it separately.
    internal_markdown = raw.pop("internal_report_markdown", None)
    write_json(raw_path, raw)
    write_json(normalized_path, normalized)
    write_json(output_dir / "handoff.json", normalized["handoff"])
    atomic_write(report_path, report)
    # Trend-comparison charts (optional: no-op if matplotlib is absent). The report
    # already references charts/<key>.png for metrics whose series were retained.
    try:
        report_charts.generate_charts(normalized, output_dir / "charts",
                                      lang=getattr(args, "lang", "zh"))
    except Exception:  # charts are a nicety, never fail the inspection over them
        pass
    internal_report_path: Path | None = None
    if internal_markdown:
        internal_report_path = output_dir / "internal-report.md"
        atomic_write(internal_report_path, internal_markdown)
    gate = run_gate(normalized_path, report_path, cluster_id, plan, output_dir, args)
    gate_path = output_dir / "gate.json"
    write_json(gate_path, gate)
    validation = validate_artifacts(raw, normalized, report, gate, plan)
    validation_path = output_dir / "validation.json"
    write_json(validation_path, validation)
    if not validation["passed"]:
        raise RuntimeError(f"artifact validation failed: {validation['checks']}")
    return {
        "validation": "passed",
        "cluster_id": str(cluster_id),
        "cluster_name": normalized.get("cluster_name"),
        "plan": plan,
        "status": normalized.get("status"),
        "confidence": normalized.get("confidence"),
        "analysis_candidates": normalized.get("analysis_candidates") or [],
        "report": str(report_path),
        "internal_report": str(internal_report_path) if internal_report_path else None,
        "internal_evidence": str(Path(gate["evidence_bundle"])),
        "gate": str(gate_path),
        "validation_file": str(validation_path),
    }


def run_inspect(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = inspect_one(ClinicAPI(args.env_file), args.cluster_id, args, output_dir)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        failure = {"validation": "failed", "status": "N/A", "confidence": "LOW", "error": str(exc)}
        write_json(output_dir / "failure.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


def list_org_cluster_ids(api: ClinicAPI, org_id: str) -> list[str]:
    """Enumerate cluster ids for an organization, tolerant of response shape."""
    response = api.list_clusters(org_id, show_deleted=False, page_size=200, page=1)
    if isinstance(response, dict) and response.get("error"):
        raise RuntimeError(f"organization cluster listing failed: {response['error']}")
    items: list[Any] = []
    if isinstance(response, list):
        items = response
    elif isinstance(response, dict):
        for key in ("items", "clusters", "data", "result"):
            value = response.get(key)
            if isinstance(value, list):
                items = value
                break
    ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("clusterID", "cluster_id", "clusterId", "id"):
            value = item.get(key)
            if value not in (None, ""):
                cid = str(value)
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
                break
    return ids


def aggregate_org_report(org_id: str, results: list[dict[str, Any]], failures: list[dict[str, Any]]) -> str:
    rank = {"CRITICAL": 3, "WARNING": 2, "NOTICE": 1, "HEALTHY": 0, "N/A": -1}
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    ordered = sorted(results, key=lambda item: rank.get(item.get("status"), -1), reverse=True)
    lines = [
        f"# TiDB Cloud Organization Health Screening — {org_id}",
        "",
        f"> Clusters inspected: **{len(results)}** · Failed to inspect: **{len(failures)}**",
        "",
        "## Status summary",
        "",
        "| Status | Clusters |",
        "|---|---:|",
    ]
    for status in ("CRITICAL", "WARNING", "NOTICE", "HEALTHY", "N/A"):
        if counts.get(status):
            lines.append(f"| {status} | {counts[status]} |")
    lines += ["", "## Clusters (most severe first)", "", "| Cluster | Plan | Status | Confidence |", "|---|---|---|---|"]
    for item in ordered:
        lines.append(
            f"| {item.get('cluster_name') or item['cluster_id']} | {item.get('plan')} | {item.get('status')} | {item.get('confidence')} |"
        )
    if failures:
        lines += ["", "## Not inspected", "", "| Cluster | Error |", "|---|---|"]
        for item in failures:
            lines.append(f"| {item['cluster_id']} | {item['error']} |")
    lines.append("")
    return "\n".join(lines)


def run_inspect_org(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        if not args.org_id and not args.cluster_id:
            raise RuntimeError("inspect-org requires --org-id or at least one --cluster-id")
        api = ClinicAPI(args.env_file)
        cluster_ids = normalize_cluster_ids(args.cluster_id) if args.cluster_id else list_org_cluster_ids(api, args.org_id)
        if not cluster_ids:
            raise RuntimeError("no clusters found for organization screening")
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for cluster_id in cluster_ids:
            try:
                results.append(inspect_one(api, cluster_id, args, output_dir / cluster_id))
            except Exception as exc:  # per-cluster isolation; screen the rest
                failures.append({"cluster_id": str(cluster_id), "error": str(exc)})
        org_report_path = output_dir / "org-report.md"
        atomic_write(org_report_path, aggregate_org_report(args.org_id or "explicit-set", results, failures))
        summary = {
            "validation": "passed" if results else "failed",
            "org_id": args.org_id,
            "clusters_inspected": len(results),
            "clusters_failed": len(failures),
            "org_report": str(org_report_path),
            "results": results,
            "failures": failures,
        }
        write_json(output_dir / "org-summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if results else 1
    except Exception as exc:
        failure = {"validation": "failed", "error": str(exc)}
        write_json(output_dir / "failure.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


def _add_inspection_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--profile", choices=["health"], default="health")
    sub.add_argument("--lang", choices=["zh", "en"], default="zh",
                     help="Customer report language (shared plans default to Chinese)")
    sub.add_argument("--start")
    sub.add_argument("--end")
    sub.add_argument("--relative-window")
    sub.add_argument("--step-seconds", type=int, default=300)
    sub.add_argument("--expected-plan", choices=["Starter", "Essential", "Premium", "Dedicated", "BYOC"])
    sub.add_argument("--env-file", default=".env")
    sub.add_argument("--registry")
    sub.add_argument("--output-dir", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single deterministic TiDB Cloud health inspection runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="Inspect one TiDB Cloud cluster")
    inspect.add_argument("--cluster-id", required=True)
    _add_inspection_args(inspect)
    inspect.set_defaults(func=run_inspect)

    org = subparsers.add_parser("inspect-org", help="Screen every cluster in an organization")
    org.add_argument("--org-id", help="Organization id; discovers its clusters")
    org.add_argument("--cluster-id", action="append", help="Explicit cluster id(s); repeat or comma-separate (instead of --org-id)")
    _add_inspection_args(org)
    org.set_defaults(func=run_inspect_org)

    # Feishu/Lark publishing is handled by the global `lark-cli` as the user's own
    # identity (markdown import embeds @./ local chart images directly), so this
    # CLI no longer carries a hand-written publisher.
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
