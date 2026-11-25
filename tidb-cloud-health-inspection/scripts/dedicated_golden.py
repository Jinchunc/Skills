#!/usr/bin/env python3
"""Golden guardrail for the Dedicated/BYOC collection migration (see
references/dedicated-migration-plan.md, phase 1).

The dedicated collector (`dedicated_health_inspection.py`, a ~2800-line monolith)
is being migrated, check by check, into the `collection_adapters` recipe model.
Before any check is ported we freeze a **shape** baseline — per check_id, its
status, reason, and the SET of evidence keys — and diff the ported output against
it. Shape, not values: raw evidence may carry volatile numbers and internal
identifiers (instance names), so the fixture stores only keys. A port is correct
when it reproduces the same statuses and the same evidence fields.

Usage:
  dedicated_golden.py shape   <normalized.json>           # print the shape
  dedicated_golden.py capture <normalized.json> <out.json># write a golden fixture
  dedicated_golden.py compare <golden.json> <normalized.json>  # diff; exit 1 on drift
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# The checks the Dedicated/BYOC collector is expected to bind (migration scope).
BOUND_DEDICATED_CHECKS = [
    "AVL-01", "AVL-02", "AVL-08", "CDC-01", "CDC-02",
    "INF-03", "INF-04", "INF-05", "OVR-01", "OVR-02", "PD-03",
    "TIDB-01", "TIDB-02",
    "TIKV-01", "TIKV-02", "TIKV-03", "TIKV-04", "TIKV-05",
    "TIKV-06", "TIKV-07", "TIKV-08", "TIKV-09", "TIKV-10",
]


def result_shape(check_results: list[dict[str, Any]],
                 check_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """{check_id: {status, reason, evidence_keys}} — values stripped (privacy-safe).
    When check_ids is given, only those checks are included (missing ones omitted)."""
    wanted = set(check_ids) if check_ids is not None else None
    shape: dict[str, dict[str, Any]] = {}
    for r in check_results:
        cid = str(r.get("check_id"))
        if wanted is not None and cid not in wanted:
            continue
        evidence = r.get("evidence") or {}
        shape[cid] = {
            "status": str(r.get("status", "N/A")),
            "reason": r.get("reason"),
            "evidence_keys": sorted(str(k) for k in evidence.keys()),
        }
    return shape


def compare_shapes(baseline: dict[str, dict[str, Any]],
                   candidate: dict[str, dict[str, Any]]) -> list[str]:
    """Diffs that a correct port must NOT introduce: a missing check, a changed
    status, or a changed evidence-key set. A changed `reason` is reported as a soft
    note (prefixed 'note:') since a reimplementation may word it differently."""
    diffs: list[str] = []
    for cid in sorted(baseline):
        if cid not in candidate:
            diffs.append(f"{cid}: MISSING in candidate")
            continue
        b, c = baseline[cid], candidate[cid]
        if b.get("status") != c.get("status"):
            diffs.append(f"{cid}: status {b.get('status')} → {c.get('status')}")
        if b.get("evidence_keys") != c.get("evidence_keys"):
            diffs.append(f"{cid}: evidence_keys {b.get('evidence_keys')} → {c.get('evidence_keys')}")
        if b.get("reason") != c.get("reason"):
            diffs.append(f"note: {cid}: reason {b.get('reason')} → {c.get('reason')}")
    for cid in sorted(set(candidate) - set(baseline)):
        diffs.append(f"{cid}: EXTRA in candidate (not in baseline)")
    return diffs


def _load_results(normalized_path: str) -> list[dict[str, Any]]:
    return (json.loads(Path(normalized_path).read_text(encoding="utf-8")) or {}).get("check_results", [])


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(__doc__ or "")
        return 2
    cmd = argv[0]
    if cmd == "shape" and len(argv) == 2:
        print(json.dumps(result_shape(_load_results(argv[1]), BOUND_DEDICATED_CHECKS),
                         ensure_ascii=False, indent=2))
        return 0
    if cmd == "capture" and len(argv) == 3:
        shape = result_shape(_load_results(argv[1]), BOUND_DEDICATED_CHECKS)
        Path(argv[2]).write_text(json.dumps(
            {"_note": "Dedicated collection shape baseline (status + evidence keys, no raw values). "
                      "See references/dedicated-migration-plan.md.", "shapes": shape},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sys.stderr.write(f"captured {len(shape)} check shapes → {argv[2]}\n")
        return 0
    if cmd == "compare" and len(argv) == 3:
        golden = json.loads(Path(argv[1]).read_text(encoding="utf-8")).get("shapes", {})
        cand = result_shape(_load_results(argv[2]), BOUND_DEDICATED_CHECKS)
        diffs = compare_shapes(golden, cand)
        hard = [d for d in diffs if not d.startswith("note:")]
        for d in diffs:
            print(d)
        return 1 if hard else 0
    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
