#!/usr/bin/env python3
"""Deterministic state and LLM gate for TiDB Cloud inspection output.

The gate accepts JSON produced by a collector, derives a stable status and alert
signature, persists state/idempotency artifacts, and prints the runner contract.
It does not send notifications and does not call an LLM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from checklist_registry import SelectionRequest, load_registry, select_checks

STATUS_RANK = {"HEALTHY": 0, "NOTICE": 1, "N/A": 1, "WARNING": 2, "CRITICAL": 3}
VERDICT_MAP = {
    "healthy": "HEALTHY",
    "notice": "NOTICE",
    "needs review": "N/A",
    "unknown": "N/A",
    "warning": "WARNING",
    "error": "CRITICAL",
    "critical": "CRITICAL",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: str | None, command: list[str]) -> dict[str, Any]:
    if command:
        proc = subprocess.run(command, text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"collector failed with exit code {proc.returncode}: "
                f"{proc.stderr.strip()[:1000]}"
            )
        raw = proc.stdout
    elif path and path != "-":
        raw = Path(path).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"collector returned malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("collector JSON must be an object")
    if value.get("error"):
        raise RuntimeError(f"collector reported an error: {value['error']}")
    return value


def normalize_status(value: Any) -> str:
    text = str(value or "").strip()
    upper = text.upper()
    if upper in STATUS_RANK:
        return upper
    return VERDICT_MAP.get(text.lower(), "N/A")


def iter_reports(payload: dict[str, Any]) -> list[dict[str, Any]]:
    reports = payload.get("reports")
    if isinstance(reports, list):
        return [item for item in reports if isinstance(item, dict)]
    results = payload.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    return [payload]


def _report_status(report: dict[str, Any]) -> str:
    """Independent status for a single report = worst of its verdict/status signal
    and its own ``check_results`` (N/A excluded, matching checklist_engine.evaluate).
    This keeps the gate's safety-net derivation aligned with the collector's
    ``worse_status(verdict, engine_worst)`` so a per-check severity (e.g. a tenant SQL
    check going CRITICAL) that exceeds the collector verdict is not silently dropped.
    Reports without ``check_results`` behave exactly as before (verdict/status only)."""
    candidates: list[str] = []
    base = normalize_status(report.get("verdict") or report.get("status"))
    if base != "N/A":
        candidates.append(base)
    checks = report.get("check_results")
    if isinstance(checks, list):
        candidates.extend(
            s for s in (normalize_status(c.get("status")) for c in checks if isinstance(c, dict))
            if s != "N/A"
        )
    if not candidates:
        return "N/A"
    return max(candidates, key=lambda item: STATUS_RANK[item])


def derive_status(payload: dict[str, Any]) -> str:
    statuses = [_report_status(item) for item in iter_reports(payload)]
    return max(statuses or ["N/A"], key=lambda item: STATUS_RANK[item])


def derive_confidence(payload: dict[str, Any], status: str) -> str:
    explicit = str(payload.get("confidence") or "").upper()
    if explicit in {"HIGH", "MEDIUM", "LOW"}:
        return explicit
    reports = iter_reports(payload)
    unknown_count = 0
    failure_count = len(payload.get("failures") or [])
    for report in reports:
        unknowns = report.get("unknown_checks") or report.get("unknowns") or []
        unknown_count += len(unknowns) if isinstance(unknowns, list) else 1
    if failure_count or status == "N/A":
        return "LOW"
    if unknown_count:
        return "MEDIUM"
    return "HIGH"


def finding_signature(payload: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    normalized: list[dict[str, str]] = []
    for report in iter_reports(payload):
        target = str(report.get("cluster_id") or report.get("clusterID") or report.get("target") or "unknown")
        findings = report.get("findings") or []
        if isinstance(findings, list):
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                normalized.append(
                    {
                        "target": target,
                        "severity": normalize_status(finding.get("severity") or finding.get("status")),
                        "title": str(finding.get("title") or finding.get("name") or finding.get("id") or "finding"),
                    }
                )
    normalized.sort(key=lambda item: (item["target"], item["severity"], item["title"]))
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), normalized


def derive_analysis_request(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return an explicit interpretation request and its normalized reasons.

    Severity alone does not require an LLM. Collectors request interpretation
    only when deterministic rules cannot complete the analysis.
    """
    items: list[dict[str, Any]] = [payload]
    for report in iter_reports(payload):
        if report is not payload:
            items.append(report)
        findings = report.get("findings") or []
        if isinstance(findings, list):
            items.extend(item for item in findings if isinstance(item, dict))

    required = False
    reasons: list[str] = []
    for item in items:
        if item.get("llm_required") is True or item.get("analysis_required") is True:
            required = True
        for key in ("analysis_reason", "llm_reason"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                reasons.append(value.strip())
        value = item.get("analysis_reasons")
        if isinstance(value, list):
            reasons.extend(str(reason).strip() for reason in value if str(reason).strip())

    return required, sorted(set(reasons))


def target_identity(payload: dict[str, Any], override: str | None) -> str:
    if override:
        return override
    ids = []
    for report in iter_reports(payload):
        value = report.get("cluster_id") or report.get("clusterID")
        if value:
            ids.append(str(value))
    return ",".join(sorted(set(ids))) or "inspection"


def safe_key(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]


def payload_check_ids(payload: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    explicit_binding = False
    for key in ("executed_check_ids", "attempted_check_ids"):
        raw = payload.get(key)
        if isinstance(raw, list):
            explicit_binding = True
            values.update(str(item) for item in raw if item)
    if explicit_binding:
        return values
    check_results = payload.get("check_results")
    if isinstance(check_results, list):
        for item in check_results:
            if isinstance(item, dict) and item.get("check_id"):
                values.add(str(item["check_id"]))
    return values


def validate_check_coverage(payload: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    reported = payload_check_ids(payload)
    selected = set(selection["selected_check_ids"])
    prohibited = set(selection["prohibited_check_ids"])
    unknown = reported - selected
    prohibited_reported = reported & prohibited
    if prohibited_reported:
        raise ValueError(f"collector reported prohibited checks: {sorted(prohibited_reported)}")
    if unknown:
        raise ValueError(f"collector reported checks outside registry selection: {sorted(unknown)}")
    runnable = set(selection["runnable_check_ids"])
    return {
        "binding": "bound" if reported else "unbound",
        "reported_check_ids": sorted(reported),
        "missing_runnable_check_ids": sorted(runnable - reported) if reported else sorted(runnable),
        "na_check_ids": list(selection["na_check_ids"]),
        "complete": bool(reported) and runnable.issubset(reported),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist inspection state and decide whether Agent interpretation is required")
    parser.add_argument("--input", default="-", help="Collector JSON path, or - for stdin")
    parser.add_argument("--target", help="Stable target key; derived from collector JSON when omitted")
    parser.add_argument("--plan", required=True, choices=["Starter", "Essential", "Premium", "Dedicated", "BYOC", "Unknown"])
    parser.add_argument("--profile", default="health", choices=["health", "component_deep_dive"])
    parser.add_argument("--target-scope", default="individual_cluster", choices=["individual_cluster", "organization"])
    parser.add_argument("--component", help="Required for component_deep_dive")
    parser.add_argument("--capability", action="append", default=[], help="Verified capability key; repeat as needed")
    parser.add_argument("--registry", help="Checklist registry path; defaults to config/checklist.v1.json")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--artifact-dir", default="artifacts")
    parser.add_argument("--report", help="Existing Markdown report path")
    parser.add_argument("--llm-for-warning", action="store_true", help="Operator override: require Agent interpretation for new/changed warnings")
    parser.add_argument("--llm-for-critical", action="store_true", help="Operator override: require Agent interpretation for new/changed critical results")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Optional collector command after --")
    args = parser.parse_args()

    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    run_id = str(uuid.uuid4())
    created_at = utc_now()
    try:
        payload = load_json(args.input, command)
        registry = load_registry(args.registry) if args.registry else load_registry()
        checklist_selection = select_checks(
            registry,
            SelectionRequest(
                plan=args.plan,
                profile=args.profile,
                target_scope=args.target_scope,
                component=args.component,
                capabilities=frozenset(args.capability),
            ),
        )
        checklist_coverage = validate_check_coverage(payload, checklist_selection)
        status = derive_status(payload)
        confidence = derive_confidence(payload, status)
        signature, findings = finding_signature(payload)
        target = target_identity(payload, args.target)
        key = safe_key(target)
        state_path = Path(args.state_dir) / f"{key}.json"
        previous: dict[str, Any] = {}
        if state_path.exists():
            try:
                previous = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
        checklist_signature = hashlib.sha256(
            json.dumps(
                {
                    "registry_version": checklist_selection["registry_version"],
                    "selected_check_ids": checklist_selection["selected_check_ids"],
                    "capabilities": checklist_selection["capabilities"],
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        changed = (
            previous.get("finding_signature") != signature
            or previous.get("status") != status
            or previous.get("checklist_signature") != checklist_signature
        )
        worsening = STATUS_RANK.get(status, 1) > STATUS_RANK.get(str(previous.get("status") or "HEALTHY"), 0)
        actionable = status in {"WARNING", "CRITICAL"}
        llm_required, analysis_reasons = derive_analysis_request(payload)
        if args.llm_for_warning and status == "WARNING" and changed:
            llm_required = True
            analysis_reasons.append("operator_override:warning")
        if args.llm_for_critical and status == "CRITICAL" and changed:
            llm_required = True
            analysis_reasons.append("operator_override:critical")
        analysis_reasons = sorted(set(analysis_reasons))
        notification_owner = "agent" if llm_required else ("python" if actionable and (changed or worsening) else "none")

        artifact_dir = Path(args.artifact_dir) / run_id
        evidence_path = artifact_dir / "evidence.json"
        result_path = artifact_dir / "result.json"
        evidence = {
            "run_id": run_id,
            "created_at": created_at,
            "target": {"id": target, "plan": args.plan},
            "inspection": {
                "profile": args.profile,
                "target_scope": args.target_scope,
                "component": args.component,
            },
            "checklist_selection": checklist_selection,
            "checklist_coverage": checklist_coverage,
            "status": status,
            "confidence": confidence,
            "findings": findings,
            "collector_output": payload,
        }
        atomic_write(evidence_path, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        state = {
            "target": target,
            "plan": args.plan,
            "profile": args.profile,
            "target_scope": args.target_scope,
            "component": args.component,
            "status": status,
            "confidence": confidence,
            "finding_signature": signature,
            "checklist_signature": checklist_signature,
            "registry_version": checklist_selection["registry_version"],
            "selected_check_ids": checklist_selection["selected_check_ids"],
            "last_run_id": run_id,
            "last_seen": created_at,
            "last_changed": created_at if changed else previous.get("last_changed"),
            "last_delivery_owner": notification_owner,
        }
        atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        result = {
            "run_id": run_id,
            "target": {"id": target, "plan": args.plan},
            "inspection": {
                "profile": args.profile,
                "target_scope": args.target_scope,
                "component": args.component,
            },
            "registry_version": checklist_selection["registry_version"],
            "checklist": {
                "selected_check_ids": checklist_selection["selected_check_ids"],
                "runnable_check_ids": checklist_selection["runnable_check_ids"],
                "na_check_ids": checklist_selection["na_check_ids"],
                "coverage": checklist_coverage,
            },
            "status": status,
            "confidence": confidence,
            "changed": changed,
            "actionable": actionable,
            "llm_required": llm_required,
            "analysis_reasons": analysis_reasons,
            "fire": llm_required,
            "evidence_bundle": str(evidence_path.resolve()),
            "report": str(Path(args.report).resolve()) if args.report else None,
            "notification_owner": notification_owner,
            "state_path": str(state_path.resolve()),
        }
        atomic_write(result_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        failure = {
            "run_id": run_id,
            "created_at": created_at,
            "status": "N/A",
            "confidence": "LOW",
            "llm_required": False,
            "fire": False,
            "notification_owner": "none",
            "error": str(exc),
        }
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
