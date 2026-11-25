#!/usr/bin/env python3
"""Single deterministic entry point for TiDB Cloud health inspection."""
from __future__ import annotations

import argparse
import json
import os
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


def logical_capabilities() -> frozenset[str]:
    return frozenset(
        {
            "component.scope",
            "component.overview",
            "component.sql",
            "component.resource_control",
            "component.logical_storage",
            "component.availability",
            # The runner always collects an equal-duration previous window, so the
            # comparable-window capability LST-03 gates on is satisfied.
            "check.lst-03",
        }
    )


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
            capabilities=logical_capabilities(),
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


def dedicated_capabilities() -> frozenset[str]:
    return frozenset(
        {
            "component.scope",
            "component.overview",
            "component.tikv",
            "component.pd",
            "component.infrastructure",
            "component.availability",
        }
    )


def _collect_reliability(client: Any, cluster_id: str, window: Any) -> dict[str, Any]:
    """TiCDC changefeed health (state counts + checkpoint/resolved-ts lag) via the
    dedicated metrics client, for §6. Fail-closed: {} on error; a missing metric
    simply stays absent → N/A at binding time."""
    out: dict[str, Any] = {}
    end_iso = window.end.isoformat()

    def _scalar(query: str) -> float | None:
        try:
            resp = client.metrics.query_instant(cluster_id, query, end_iso)
            res = (resp or {}).get("data", {}).get("result", [])
            return float(res[0]["value"][1]) if res else None
        except Exception:
            return None

    try:
        resp = client.metrics.query_instant(cluster_id, "ticdc_coordinator_changefeed_state", end_iso)
        states: dict[str, float] = {}
        for series in (resp or {}).get("data", {}).get("result", []):
            state = (series.get("metric") or {}).get("state")
            if state is None:
                continue
            try:
                value = float(series["value"][1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            states[state] = max(states.get(state, 0.0), value)
        if states:
            out["changefeed_states"] = states
    except Exception:
        pass

    lag = _scalar("max(ticdc_dispatchermanager_checkpoint_ts_lag)")
    if lag is not None:
        out["cdc_checkpoint_lag_max"] = lag
    resolved = _scalar("max(ticdc_dispatchermanager_resolved_ts_lag)")
    if resolved is not None:
        out["cdc_resolved_lag_max"] = resolved

    # §5.4 execution-plan health: plan-cache hit rate (%).
    hit = _scalar(
        "100 * sum(rate(tidb_server_plan_cache_total[5m])) / "
        "(sum(rate(tidb_server_plan_cache_total[5m])) + sum(rate(tidb_server_plan_cache_miss_total[5m])))"
    )
    if hit is not None:
        out["plan_cache_hit_pct"] = hit
    return out


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


def _resolve_disk_device_regex(api: ClinicAPI, cluster_id: str) -> str:
    """TiKV data-disk device regex for node-disk→pod join queries. Resolves the
    storage-type-specific device when the cluster detail exposes it; otherwise the
    catch-all default (which matches the collector's own fallback)."""
    try:
        from dedicated_health_inspection import (
            DISK_DEVICE_RE, build_device_regex, extract_tikv_storage_type, resolve_tikv_disk_devices)
        detail = api.get_cluster_by_id(cluster_id)
        storage_type = extract_tikv_storage_type(detail) if isinstance(detail, dict) else ""
        return build_device_regex(resolve_tikv_disk_devices(storage_type))
    except Exception:
        return r"nvme.*|xvd.*|sd.*|vd.*|dm-.*"


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
    cluster_id: str,
    plan: str,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    from dedicated_health_inspection import InspectionClient, inspect_cluster, resolve_window as resolve_dedicated_window
    from dedicated_binding import bind_dedicated_results, verdict_to_status

    window = resolve_dedicated_window(args.start, args.end, args.relative_window, "UTC")
    client = InspectionClient(args.env_file)
    try:
        report = inspect_cluster(client, cluster_id, window)
        # §6 reliability signals (TiCDC changefeed health) via the same metrics
        # client; merged into report.metrics for dedicated_binding. Fail-closed.
        report.setdefault("metrics", {}).update(_collect_reliability(client, cluster_id, window))
    except Exception as exc:
        # Top-level degradation: a full-stack collector failure must NOT crash the
        # whole run. Produce an all-N/A report (fail-closed) with the error recorded,
        # so the cluster is reported as N/A/LOW rather than aborting the inspection.
        report = {
            "cluster_id": str(cluster_id),
            "cluster_name": str(cluster_id),
            "provider": "", "region": "", "version": "", "status": "",
            "verdict": "unknown",
            "analysis": {}, "metrics": {},
            "findings": [],
            "unknown_checks": [f"collector_error: {exc}"],
            "window": {
                "timezone": getattr(window, "timezone_name", "UTC"),
                "start_utc": window.start.isoformat(),
                "end_utc": window.end.isoformat(),
                "seconds": getattr(window, "seconds", 0),
            },
            "markdown": f"# Full-stack collection failed\n\n{exc}\n",
            "collector_error": str(exc),
        }

    registry = load_registry(args.registry) if args.registry else load_registry()
    selection = select_checks(
        registry,
        SelectionRequest(
            plan=plan,
            profile=args.profile,
            target_scope="individual_cluster",
            component=None,
            capabilities=dedicated_capabilities(),
        ),
    )
    # Bind only within the registry selection so a registry/capability change can
    # never make evaluate() reject a hard-coded id and crash the dedicated run.
    # On a collector failure, bind nothing: the engine then synthesizes N/A for every
    # selected check (fail-closed) instead of the binding's default-HEALTHY (scope,
    # compaction/read-pool) masking a total outage as HEALTHY.
    selected_ids = set(selection["selected_check_ids"])
    workload_prev: dict[str, Any] = {}
    workload_prev_window: dict[str, Any] | None = None
    if report.get("collector_error"):
        bound = []
    else:
        bound = [item for item in bind_dedicated_results(report) if item["check_id"] in selected_ids]
        # §3 workload (tenant SQL RED) + §5 owner SQL Top-N — also in the dedicated
        # selection; collect via the shared layer so dedicated §3/§5 match logical.
        workload_ids = [c for c in ("SQL-01", "SQL-02", "SQL-03", "SQL-05", "SQL-06",
                                    "SQLD-01", "SQLD-02", "SQLD-03") if c in selected_ids]
        if workload_ids:
            try:
                wl_cur, workload_prev, workload_prev_window = _collect_workload(
                    ClinicAPI(args.env_file), cluster_id, args, workload_ids)
                bound_ids = {b["check_id"] for b in bound}
                bound += [r for r in wl_cur if r["check_id"] not in bound_ids]
            except Exception:  # fail-closed: workload stays N/A, run continues
                workload_prev = {}
                workload_prev_window = None
        # Binding收口 (2026-11-25): the native recipe/adapter layer is now the
        # AUTHORITATIVE source for all 23 bound dedicated checks (guardrail-verified at
        # zero hard diff, same-window monolith baseline). dedicated_binding is retained
        # only as (a) the SCOPE-01/04/08 source and (b) the error-path fallback — the
        # monolith still runs for the overall verdict floor + metadata, so its bound
        # results stay available for free until monolith retirement. The merge is
        # native-primary and add-based: native wins for the checks it owns, and the
        # monolith's binding fills any check native did not produce (robust to a
        # per-check native gap, not only a whole-run failure).
        # The native set is data-driven (recipes flagged dedicated_native, via
        # native_dedicated_checks()) unless NATIVE_DEDICATED_ADAPTER_CHECKS is set in the
        # environment — a comma list (empty = pure monolith) — which lets the golden
        # guardrail capture a same-window monolith baseline without a code edit.
        _env_native = os.environ.get("NATIVE_DEDICATED_ADAPTER_CHECKS")
        _native_set = ([c.strip() for c in _env_native.split(",") if c.strip()]
                       if _env_native is not None else list(native_dedicated_checks()))
        native_ids = [c for c in _native_set if c in selected_ids]
        if native_ids:
            try:
                overview_md = {k: report.get(k) for k in ("provider", "region", "version", "status")}
                native = _collect_native_dedicated(ClinicAPI(args.env_file), cluster_id, args,
                                                   native_ids, metadata=overview_md)
                by_id = {b["check_id"]: b for b in bound}  # monolith binding = fallback/SCOPE
                by_id.update(native)                       # native authoritative for the 23
                bound = list(by_id.values())
            except Exception:  # fail-closed: keep the monolith's result on error
                pass
    metadata = {
        "provider": report.get("provider"),
        "region": report.get("region"),
        "version": report.get("version"),
        "status": report.get("status"),
    }
    # §7 authorized feature inventory from the Clinic cluster detail (fail-closed).
    metadata.update({k: v for k, v in _fetch_cluster_detail(ClinicAPI(args.env_file), cluster_id).items()
                     if k == "feature_gates"})
    # §5.4 execution-plan health: plan-cache hit rate collected above.
    plan_cache = (report.get("metrics") or {}).get("plan_cache_hit_pct")
    if isinstance(plan_cache, (int, float)):
        metadata["plan_cache_hit_pct"] = plan_cache
    raw = {
        "cluster_id": str(cluster_id),
        "cluster_name": report.get("cluster_name") or str(cluster_id),
        "metadata": metadata,
        "plan": plan,
        "collector": "dedicated_health_inspection",
        "window": report.get("window") or {},
        "check_results": bound,
        "verdict": report.get("verdict"),
        "findings": report.get("findings"),
        "unknown_checks": report.get("unknown_checks"),
        "analysis": report.get("analysis"),
    }
    normalized = evaluate(raw, selection)
    # Overall status is the worse of the collector's verdict and the engine's
    # worst bound check — the binding may only RAISE severity, never mask it.
    normalized["status"] = worse_status(verdict_to_status(report.get("verdict")), normalized.get("status") or "N/A")
    # Respect the engine's N/A-aware confidence; collection failures only lower it.
    engine_confidence = normalized.get("confidence") or "LOW"
    normalized["confidence"] = "MEDIUM" if (report.get("unknown_checks") and engine_confidence == "HIGH") else engine_confidence
    normalized["overall_status_source"] = "max(collector_verdict, engine)"
    normalized["lang"] = getattr(args, "lang", "zh")
    if workload_prev:
        normalized["previous_period"] = workload_prev  # §3 workload trend
    if workload_prev_window:
        normalized["previous_window"] = workload_prev_window  # §3 period-range note

    # The collector's Feishu report is an internal artifact, not the customer
    # report. The customer report is rendered from canonical check_results.
    raw["internal_report_markdown"] = str(report.get("markdown") or "")
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
    for capability in sorted(logical_capabilities() if plan in LOGICAL_PLANS else frozenset()):
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
        raw, normalized, report = collect_full_stack(cluster_id, plan, args)

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
