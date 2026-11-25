#!/usr/bin/env python3
"""Registry-driven deterministic checklist evaluation engine.

Collectors return evidence as JSON with optional row-level `check_results`.
This engine selects the allowed checks from the registry, rejects prohibited
results, converts missing/unimplemented evidence to explicit N/A, and emits one
normalized payload for the common inspection gate.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from checklist_registry import SelectionRequest, load_registry, select_checks

STATUS_RANK = {"HEALTHY": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3, "N/A": -1}


def load_payload(path: str, command: list[str]) -> dict[str, Any]:
    if command:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"collector failed ({completed.returncode}): {completed.stderr.strip()}")
        text = completed.stdout
    elif path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("collector output must be a JSON object")
    return value


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("inspection window timestamps must include a timezone")
    return parsed


def normalize_window(payload: dict[str, Any]) -> dict[str, Any]:
    window = payload.get("window")
    if not isinstance(window, dict):
        raise ValueError("collector output must include window evidence")
    start_value = window.get("start") or window.get("start_utc")
    end_value = window.get("end") or window.get("end_utc")
    if not isinstance(start_value, str) or not isinstance(end_value, str):
        raise ValueError("window.start/start_utc and window.end/end_utc are required")
    start = parse_time(start_value)
    end = parse_time(end_value)
    if end <= start:
        raise ValueError("inspection window end must be after start")
    duration = int((end - start).total_seconds())
    return {
        "timezone": window.get("timezone") or "UTC",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_seconds": duration,
        "step": window.get("step"),
        "aggregation": window.get("aggregation"),
    }


def normalize_reported_results(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("check_results")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("check_results must be an array")
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or not item.get("check_id"):
            raise ValueError("each check result needs check_id")
        check_id = str(item["check_id"])
        if check_id in result:
            raise ValueError(f"duplicate check result: {check_id}")
        status = str(item.get("status") or "N/A").upper()
        if status not in STATUS_RANK:
            raise ValueError(f"invalid status for {check_id}: {status}")
        normalized = dict(item)
        normalized["check_id"] = check_id
        normalized["status"] = status
        result[check_id] = normalized
    return result


def evaluate(payload: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    window = normalize_window(payload)
    reported = normalize_reported_results(payload)
    rows = {row["check_id"]: row for row in selection["checks"]}
    prohibited = set(selection["prohibited_check_ids"])
    selected = set(selection["selected_check_ids"])
    invalid = set(reported) - selected
    if set(reported) & prohibited:
        raise ValueError(f"collector returned prohibited checks: {sorted(set(reported) & prohibited)}")
    if invalid:
        raise ValueError(f"collector returned checks outside selected scope: {sorted(invalid)}")

    results: list[dict[str, Any]] = []
    for check_id in selection["selected_check_ids"]:
        row = rows[check_id]
        if check_id in reported:
            item = reported[check_id]
            item.setdefault("reason", "collector_result")
            item["registry_mark"] = row["mark"]
            item["implementation"] = row["implementation"]
            results.append(item)
        elif row["disposition"] == "N/A":
            results.append({
                "check_id": check_id,
                "status": "N/A",
                "reason": row["reason"],
                "registry_mark": row["mark"],
                "implementation": row["implementation"],
            })
        else:
            results.append({
                "check_id": check_id,
                "status": "N/A",
                "reason": "selected_check_missing_collector_result",
                "registry_mark": row["mark"],
                "implementation": row["implementation"],
            })

    ranked = [item for item in results if item["status"] != "N/A"]
    status = max(ranked, key=lambda item: STATUS_RANK[item["status"]])["status"] if ranked else "N/A"
    missing = [item["check_id"] for item in results if item["status"] == "N/A"]
    # Explicit signal for the interpretation step: which checks warrant a closer
    # look. This does not force an LLM (Python still owns deterministic results);
    # it tells the agent what to focus on.
    analysis_candidates = [
        {"check_id": item["check_id"], "status": item["status"], "reason": item.get("reason")}
        for item in results
        if item["status"] in ("WARNING", "CRITICAL")
    ]
    missing_runnable = sorted(set(selection["runnable_check_ids"]) - set(reported))
    if not missing and results:
        confidence = "HIGH"
    elif ranked:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    normalized = dict(payload)
    normalized.update({
        "plan": selection["plan"],
        "profile": selection["profile"],
        "target_scope": selection["target_scope"],
        "component": selection["component"],
        "registry_version": selection["registry_version"],
        "window": window,
        "status": status,
        "confidence": confidence,
        "check_results": results,
        "attempted_check_ids": sorted(reported),
        "selected_check_ids": selection["selected_check_ids"],
        "na_check_ids": missing,
        "missing_runnable_check_ids": missing_runnable,
        "checklist_complete": not missing_runnable,
        "analysis_candidates": analysis_candidates,
        "deep_dive_recommended": bool(analysis_candidates),
        "checklist_selection": selection,
    })
    return normalized


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Registry-driven deterministic checklist engine")
    value.add_argument("--registry")
    value.add_argument("--plan", required=True, choices=["Starter", "Essential", "Premium", "Dedicated", "BYOC", "Unknown"])
    value.add_argument("--profile", required=True, choices=["health", "component_deep_dive"])
    value.add_argument("--target-scope", required=True, choices=["individual_cluster", "organization"])
    value.add_argument("--component")
    value.add_argument("--capability", action="append", default=[])
    value.add_argument("--input", default="-", help="Collector JSON path, or - for stdin")
    value.add_argument("--output", help="Normalized JSON output path")
    value.add_argument("--plan-only", action="store_true")
    value.add_argument("command", nargs=argparse.REMAINDER, help="Optional JSON collector command after --")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        registry = load_registry(args.registry) if args.registry else load_registry()
        selection = select_checks(
            registry,
            SelectionRequest(args.plan, args.profile, args.target_scope, args.component, frozenset(args.capability)),
        )
        if args.plan_only:
            value: dict[str, Any] = selection
        else:
            command = args.command[1:] if args.command and args.command[0] == "--" else args.command
            value = evaluate(load_payload(args.input, command), selection)
        output = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    except Exception as exc:
        print(json.dumps({"status": "N/A", "confidence": "LOW", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
