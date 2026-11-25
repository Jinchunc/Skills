#!/usr/bin/env python3
"""Load, validate, select, and render the executable checklist registry.

`config/checklist.v1.json` is the only source of truth for Plan/category/check
applicability. The Markdown matrix is generated from it and is never parsed at
runtime.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "checklist.v1.json"
DEFAULT_MARKDOWN = ROOT / "references" / "plan-checklist-matrix.md"
KNOWN_PLANS = ("Starter", "Essential", "Premium", "Dedicated", "BYOC", "Unknown")
KNOWN_PROFILES = ("health", "component_deep_dive")
KNOWN_SCOPES = ("individual_cluster", "organization")
KNOWN_MARKS = ("R", "C", "D", "X", "—")
KNOWN_IMPLEMENTATION = ("implemented", "partial", "planned")


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class SelectionRequest:
    plan: str
    profile: str
    target_scope: str
    component: str | None = None
    capabilities: frozenset[str] = frozenset()


def load_registry(path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError(f"cannot read checklist registry: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"malformed checklist registry JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RegistryError("checklist registry root must be an object")
    validate_registry(value)
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RegistryError(message)


def validate_registry(registry: dict[str, Any]) -> None:
    _require(registry.get("schema_version") == "1.0.0", "unsupported schema_version")
    _require(registry.get("source_of_truth") is True, "source_of_truth must be true")
    _require(tuple(registry.get("plans") or ()) == KNOWN_PLANS, "plans must be canonical and ordered")
    _require(tuple(registry.get("profiles") or ()) == KNOWN_PROFILES, "profiles must be canonical and ordered")
    _require(tuple(registry.get("target_scopes") or ()) == KNOWN_SCOPES, "target_scopes must be canonical and ordered")

    categories = registry.get("categories")
    checks = registry.get("checks")
    adapters = registry.get("adapters")
    _require(isinstance(categories, list) and categories, "categories must be a non-empty array")
    _require(isinstance(checks, list) and checks, "checks must be a non-empty array")
    _require(isinstance(adapters, dict), "adapters must be an object")

    category_ids: set[str] = set()
    for category in categories:
        _require(isinstance(category, dict), "each category must be an object")
        category_id = category.get("id")
        _require(isinstance(category_id, str) and category_id.startswith("CAT-"), "invalid category id")
        _require(category_id not in category_ids, f"duplicate category id: {category_id}")
        category_ids.add(category_id)
        _require(category.get("target_scope") in KNOWN_SCOPES, f"invalid target scope for {category_id}")
        _require(bool(category.get("component")), f"missing component for {category_id}")
    _require(category_ids == {f"CAT-{i:02d}" for i in range(14)}, "registry must define CAT-00 through CAT-13")

    check_ids: set[str] = set()
    for check in checks:
        _require(isinstance(check, dict), "each check must be an object")
        check_id = check.get("id")
        _require(isinstance(check_id, str) and check_id, "check id is required")
        _require(check_id not in check_ids, f"duplicate check id: {check_id}")
        check_ids.add(check_id)
        _require(check.get("category") in category_ids, f"unknown category for {check_id}")
        _require(check.get("profile") in KNOWN_PROFILES, f"invalid profile for {check_id}")
        _require(check.get("target_scope") in KNOWN_SCOPES, f"invalid target scope for {check_id}")
        applicability = check.get("applicability")
        _require(isinstance(applicability, dict), f"missing applicability for {check_id}")
        _require(set(applicability) == set(KNOWN_PLANS[:-1]), f"invalid Plan keys for {check_id}")
        _require(all(mark in KNOWN_MARKS for mark in applicability.values()), f"invalid mark for {check_id}")
        capabilities = check.get("capabilities")
        _require(isinstance(capabilities, list) and len(capabilities) == len(set(capabilities)), f"invalid capabilities for {check_id}")
        if "C" in applicability.values():
            _require(bool(capabilities), f"capability-gated check {check_id} needs a capability key")
        implementation = check.get("implementation")
        _require(isinstance(implementation, dict), f"missing implementation for {check_id}")
        _require(implementation.get("status") in KNOWN_IMPLEMENTATION, f"invalid implementation status for {check_id}")
        for adapter in implementation.get("adapters") or []:
            _require(adapter in adapters, f"unknown adapter {adapter} for {check_id}")

    # Hard safety invariants for Shared Plans.
    for check in checks:
        if check["category"] in {"CAT-05", "CAT-06", "CAT-07", "CAT-08"}:
            for plan in ("Starter", "Essential", "Premium"):
                _require(check["applicability"][plan] == "X", f"{check['id']} must be X for {plan}")
    rcu07 = next((check for check in checks if check["id"] == "RCU-07"), None)
    _require(rcu07 is not None, "RCU-07 is required")
    for plan in ("Starter", "Essential", "Premium"):
        _require(rcu07["applicability"][plan] == "X", f"RCU-07 must be X for {plan}")


def _capability_satisfied(required: Iterable[str], supplied: frozenset[str], check: dict[str, Any]) -> bool:
    required_set = set(required)
    if not required_set:
        return True
    aliases = {
        "all",
        f"category.{check['category'].lower()}",
        f"component.{check['component']}",
        f"check.{check['id'].lower()}",
    }
    return bool(aliases & supplied) or required_set.issubset(supplied)


def select_checks(registry: dict[str, Any], request: SelectionRequest) -> dict[str, Any]:
    if request.plan not in KNOWN_PLANS:
        raise RegistryError(f"unknown Plan: {request.plan}")
    if request.profile not in KNOWN_PROFILES:
        raise RegistryError(f"unknown profile: {request.profile}")
    if request.target_scope not in KNOWN_SCOPES:
        raise RegistryError(f"unknown target scope: {request.target_scope}")
    if request.profile == "component_deep_dive" and not request.component:
        raise RegistryError("component_deep_dive requires --component")

    rows: list[dict[str, Any]] = []
    deep_dive_components = {
        check["component"] for check in registry["checks"] if check["profile"] == "component_deep_dive"
    }
    selected_ids: list[str] = []
    runnable_ids: list[str] = []
    na_ids: list[str] = []
    prohibited_ids: list[str] = []

    for check in registry["checks"]:
        if check["target_scope"] != request.target_scope:
            continue
        if request.target_scope == "organization" and check["category"] != "CAT-13":
            continue
        if request.target_scope == "individual_cluster" and check["category"] == "CAT-13":
            continue
        if request.profile == "health" and check["profile"] != "health":
            continue
        if request.profile == "component_deep_dive":
            if request.component not in {check["component"], check["category"], check["id"]}:
                continue
            # When a dedicated deep-dive category exists for a component (SQL),
            # do not silently mix its default Health checks into that request.
            if (
                request.component in deep_dive_components
                and check["profile"] != "component_deep_dive"
            ):
                continue

        mark = "X" if request.plan == "Unknown" else check["applicability"][request.plan]
        disposition: str
        reason: str
        if mark == "X":
            disposition, reason = "prohibited", "excluded_for_plan"
            prohibited_ids.append(check["id"])
        elif mark == "—":
            disposition, reason = "not_applicable", "product_capability_not_applicable"
        elif mark == "D" and request.profile != "component_deep_dive":
            disposition, reason = "skipped", "explicit_deep_dive_required"
        elif mark == "C" and not _capability_satisfied(check["capabilities"], request.capabilities, check):
            disposition, reason = "N/A", "capability_not_verified"
            selected_ids.append(check["id"])
            na_ids.append(check["id"])
        else:
            selected_ids.append(check["id"])
            implementation = check["implementation"]["status"]
            if implementation == "planned":
                disposition, reason = "N/A", "no_runtime_adapter"
                na_ids.append(check["id"])
            else:
                disposition = "run"
                reason = "required" if mark == "R" else "eligible"
                runnable_ids.append(check["id"])
        rows.append({
            "check_id": check["id"],
            "category": check["category"],
            "component": check["component"],
            "mark": mark,
            "disposition": disposition,
            "reason": reason,
            "capabilities": check["capabilities"],
            "implementation": check["implementation"],
        })

    return {
        "registry_version": registry["registry_version"],
        "plan": request.plan,
        "profile": request.profile,
        "target_scope": request.target_scope,
        "component": request.component,
        "capabilities": sorted(request.capabilities),
        "default_denied": request.plan == "Unknown",
        "selected_check_ids": selected_ids,
        "runnable_check_ids": runnable_ids,
        "na_check_ids": na_ids,
        "prohibited_check_ids": prohibited_ids,
        "checks": rows,
    }


def render_markdown(registry: dict[str, Any]) -> str:
    plans = list(KNOWN_PLANS[:-1])
    category_by_id = {item["id"]: item for item in registry["categories"]}
    lines = [
        "# Plan Checklist Matrix",
        "",
        "> Generated from `config/checklist.v1.json` by `scripts/checklist_registry.py render-markdown`.",
        "> Do not edit this file directly; the JSON registry is the runtime and documentation source of truth.",
        "",
        "This document maps canonical TiDB Cloud `plan` values to categories and stable `check_id` values.",
        "",
        "## Applicability legend",
        "",
        "| Mark | Meaning |",
        "|---|---|",
        "| **R** | Required attempt in default Health. Missing or unusable evidence returns N/A with a reason. |",
        "| **C** | Capability-gated Health check. Run only after capability and source validation. |",
        "| **D** | Explicit component deep dive only. |",
        "| **X** | Excluded for this Plan. The runtime must not query it. |",
        "| **—** | Product capability is not applicable to this Plan. |",
        "",
        "`Unknown` is default-deny for every check except safe metadata needed to explain routing failure.",
        "",
        "## Category map",
        "",
        "| Category | Purpose | Scope | Component |",
        "|---|---|---|---|",
    ]
    for category in registry["categories"]:
        lines.append(f"| `{category['id']}` {category['title']} | {category['purpose']} | `{category['target_scope']}` | `{category['component']}` |")
    lines.extend(["", "## Complete check matrix", ""])

    checks_by_category: dict[str, list[dict[str, Any]]] = {}
    for check in registry["checks"]:
        checks_by_category.setdefault(check["category"], []).append(check)
    for category_id in sorted(checks_by_category):
        category = category_by_id[category_id]
        lines.extend([
            f"### {category_id} — {category['title']}",
            "",
            "| Check ID | Check | Starter | Essential | Premium | Dedicated | BYOC | Runtime status | Notes |",
            "|---|---|---:|---:|---:|---:|---:|---|---|",
        ])
        for check in checks_by_category[category_id]:
            marks = " | ".join(check["applicability"][plan] for plan in plans)
            status = check["implementation"]["status"]
            lines.append(f"| `{check['id']}` | {check['title']} | {marks} | `{status}` | {check['notes']} |")
        lines.append("")

    lines.extend(["## Plan-specific default Health guidance", ""])
    for plan in KNOWN_PLANS:
        lines.extend([f"### {plan}", "", registry["plan_guidance"][plan], ""])
    lines.extend(["## Profile and scoring boundaries", ""])
    lines.extend(f"- {item}" for item in registry["profile_boundaries"])
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate, select, and render the checklist registry")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    select = sub.add_parser("select")
    select.add_argument("--plan", required=True, choices=KNOWN_PLANS)
    select.add_argument("--profile", required=True, choices=KNOWN_PROFILES)
    select.add_argument("--target-scope", required=True, choices=KNOWN_SCOPES)
    select.add_argument("--component")
    select.add_argument("--capability", action="append", default=[])
    render = sub.add_parser("render-markdown")
    render.add_argument("--output", default=str(DEFAULT_MARKDOWN))
    verify = sub.add_parser("verify-markdown")
    verify.add_argument("--document", default=str(DEFAULT_MARKDOWN))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        registry = load_registry(args.registry)
        if args.command == "validate":
            print(json.dumps({"valid": True, "registry_version": registry["registry_version"], "categories": len(registry["categories"]), "checks": len(registry["checks"])}, indent=2))
        elif args.command == "select":
            result = select_checks(registry, SelectionRequest(args.plan, args.profile, args.target_scope, args.component, frozenset(args.capability)))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "render-markdown":
            output = Path(args.output)
            output.write_text(render_markdown(registry), encoding="utf-8")
            print(str(output))
        elif args.command == "verify-markdown":
            expected = render_markdown(registry)
            actual = Path(args.document).read_text(encoding="utf-8")
            if actual != expected:
                raise RegistryError(f"generated Markdown is stale: {args.document}")
            print(json.dumps({"valid": True, "document": args.document}, indent=2))
        return 0
    except (RegistryError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
