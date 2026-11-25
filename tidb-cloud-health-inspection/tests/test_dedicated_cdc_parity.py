"""CDC-01/02 active-path parity guardrail.

The live golden guardrail can only exercise the CDC **N/A** path: the validation
cluster has no changefeeds, so ``ticdc_coordinator_changefeed_state`` and the
dispatcher lag metrics come back empty. This test closes the remaining gap that
gates monolith retirement: for every **active** changefeed scenario, the native
recipe layer (``changefeed_state_check`` / ``combined_scalar_threshold``) must
produce the *same shape* — status + evidence_keys — as the monolith binding
(``dedicated_binding.bind_dedicated_results``). Identical input is fed to both
paths, so this proves retiring the monolith does not change CDC behavior.

Scope / honest caveat: this verifies native == binding (migration faithfulness),
NOT that the binding's state-name vocabulary (Total/Working/Failed/Error/Stopped)
matches what a given TiCDC version emits for ``ticdc_coordinator_changefeed_state``.
That semantic question is pre-existing (the native layer reproduces whatever the
binding does) and orthogonal to retirement; confirming it needs a live
changefeed-enabled cluster. See references/dedicated-basic-report-checklist.md §3.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import collection_adapters as ca
from dedicated_binding import bind_dedicated_results
from inspection_core import TimeWindow

_RECIPES = ca.load_recipes()
_RULES = ca.load_rules()


def _shape(result: dict) -> tuple[str, tuple[str, ...]]:
    """The golden 'hard-diff' shape: status + sorted evidence keys (values stripped)."""
    return result["status"], tuple(sorted((result.get("evidence") or {}).keys()))


def _window() -> TimeWindow:
    return TimeWindow(datetime(2026, 11, 20, tzinfo=timezone.utc),
                      datetime(2026, 11, 21, tzinfo=timezone.utc), 300)


class _StateAPI:
    """Fake metrics API returning ticdc_coordinator_changefeed_state keyed by state."""

    def __init__(self, states: dict[str, float] | None):
        self.states = states

    def query_instant(self, cid, q, t):
        if not self.states:
            return {"status": "success", "data": {"result": []}}
        return {"status": "success", "data": {"result": [
            {"metric": {"state": k}, "value": [0, str(v)]} for k, v in self.states.items()]}}

    def query_range(self, *a, **k):
        return {"status": "success", "data": {"result": []}}


class _LagAPI:
    """Fake metrics API returning checkpoint/resolved-ts lag scalars by query substring."""

    def __init__(self, checkpoint: float | None, resolved: float | None):
        self.checkpoint, self.resolved = checkpoint, resolved

    def query_instant(self, cid, q, t):
        val = self.checkpoint if "checkpoint_ts_lag" in q else (
            self.resolved if "resolved_ts_lag" in q else None)
        if val is None:
            return {"status": "success", "data": {"result": []}}
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, str(val)]}]}}

    def query_range(self, *a, **k):
        return {"status": "success", "data": {"result": []}}


def _native(check_id: str, api) -> dict:
    recipe = _RECIPES[check_id]
    ctx = ca.CollectContext(api=api, cluster_id="C1", window=_window(), plan="Dedicated", rules=_RULES)
    return ca.ADAPTERS[recipe["adapter"]](ctx, check_id, recipe["params"])


def _binding(check_id: str, metrics: dict) -> dict:
    report = {"metrics": metrics, "analysis": {}, "status": "", "provider": "",
              "region": "", "version": ""}
    for item in bind_dedicated_results(report):
        if item["check_id"] == check_id:
            return item
    raise AssertionError(f"{check_id} not produced by binding")


class Cdc01ParityTests(unittest.TestCase):
    """CDC-01 changefeed inventory/state — native vs binding, every branch."""

    # (label, states-dict, expected status)
    CASES = [
        ("no_metric", None, "N/A"),
        ("empty", {}, "N/A"),
        ("total_zero", {"Total": 0, "Working": 0}, "HEALTHY"),
        ("all_working", {"Total": 3, "Working": 3}, "HEALTHY"),
        ("failed", {"Total": 3, "Working": 2, "Failed": 1}, "WARNING"),
        ("errored", {"Total": 3, "Working": 2, "Error": 1}, "WARNING"),
        ("stopped", {"Total": 3, "Working": 2, "Stopped": 1}, "NOTICE"),
        ("not_all_working", {"Total": 3, "Working": 2}, "NOTICE"),
    ]

    def test_native_matches_binding_shape(self):
        for label, states, expected in self.CASES:
            with self.subTest(case=label):
                native = _native("CDC-01", _StateAPI(states))
                metrics = {"changefeed_states": states} if states is not None else {}
                binding = _binding("CDC-01", metrics)
                self.assertEqual(_shape(native), _shape(binding), f"{label}: native != binding")
                self.assertEqual(native["status"], expected, f"{label}: unexpected status")


class Cdc02ParityTests(unittest.TestCase):
    """CDC-02 checkpoint/resolved-ts lag — native vs binding, every band."""

    # (label, checkpoint, resolved, expected status) — warn 300, crit 3600
    CASES = [
        ("both_absent", None, None, "N/A"),
        ("below_warn", 10.0, 20.0, "HEALTHY"),
        ("warn_from_checkpoint", 400.0, 10.0, "WARNING"),
        ("warn_from_resolved", 10.0, 500.0, "WARNING"),
        ("critical", 4000.0, 10.0, "CRITICAL"),
        ("at_warn_boundary", 300.0, 0.0, "WARNING"),
        ("at_crit_boundary", 3600.0, 0.0, "CRITICAL"),
    ]

    def test_native_matches_binding_shape(self):
        for label, cp, rs, expected in self.CASES:
            with self.subTest(case=label):
                native = _native("CDC-02", _LagAPI(cp, rs))
                metrics = {}
                if cp is not None:
                    metrics["cdc_checkpoint_lag_max"] = cp
                if rs is not None:
                    metrics["cdc_resolved_lag_max"] = rs
                binding = _binding("CDC-02", metrics)
                self.assertEqual(_shape(native), _shape(binding), f"{label}: native != binding")
                self.assertEqual(native["status"], expected, f"{label}: unexpected status")


if __name__ == "__main__":
    unittest.main()
