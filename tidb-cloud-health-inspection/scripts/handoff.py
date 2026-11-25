#!/usr/bin/env python3
"""Deep-analysis handoff contract.

This skill does **collection + customer report** only; deep per-component analysis
(TiDB / TiKV / PD / TiCDC …) is delegated to the external o11y instance-inspection
skill, invoked by the orchestrating agent. This module turns a run's flagged checks
into a routable handoff the orchestrating agent consumes to decide which deep module
to load for which cluster/window.

Shape (also written as ``handoff.json`` and embedded in ``normalized.json``):

    {
      "deep_analysis": "delegated",
      "cluster_id", "cluster_name", "plan", "window",
      "items": [ {check_id, component, severity, signal} ... ]   # WARNING/CRITICAL
    }

``component`` is the routing key (which subsystem the deep dive belongs to); it is
derived from the check-id prefix. The contract carries no raw metric values or
internal PromQL — only the routing signal.
"""
from __future__ import annotations

from typing import Any

# check-id prefix → component the orchestrating agent routes the deep dive to.
COMPONENT_BY_PREFIX = {
    "TIDB": "TiDB",
    "TIKV": "TiKV",
    "PD": "PD",
    "CDC": "TiCDC",
    "INF": "Infrastructure",
    "AVL": "Availability",
    "OVR": "ClusterOverview",
    "SQL": "Workload",
    "SQLD": "Workload",
    "RCU": "ResourceControl",
    "LST": "Storage",
}


def component_of(check_id: str) -> str:
    """Routing component for a check id (by prefix); 'Other' when unknown."""
    prefix = str(check_id).split("-", 1)[0]
    return COMPONENT_BY_PREFIX.get(prefix, "Other")


def build_handoff(normalized: dict[str, Any]) -> dict[str, Any]:
    """Build the deep-analysis handoff from a normalized run. Items are the
    WARNING/CRITICAL analysis candidates, routed by component."""
    items = []
    for c in normalized.get("analysis_candidates") or []:
        cid = str(c.get("check_id"))
        items.append({
            "check_id": cid,
            "component": component_of(cid),
            "severity": c.get("status"),
            "signal": c.get("reason"),
        })
    return {
        "deep_analysis": "delegated",
        "cluster_id": normalized.get("cluster_id"),
        "cluster_name": normalized.get("cluster_name"),
        "plan": normalized.get("plan"),
        "window": normalized.get("window"),
        "items": items,
    }
