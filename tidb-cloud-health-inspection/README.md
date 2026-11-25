# TiDB Cloud Health Inspection

A deterministic, plan-safe health inspection for TiDB Cloud clusters. This document
explains **how the whole skill is designed** — its principles, architecture, data
model, and how to extend it. For day-to-day operation (how to run it, how to read
the output, how to handle errors) see [`SKILL.md`](SKILL.md).

---

## 1. Purpose

Given one cluster id and a time window, the skill measures the health of a TiDB
Cloud cluster and produces:

- a **customer-safe Markdown report** (`health-report.md`), and
- an **internal evidence bundle** (normalized model, gate state, per-check results).

It supports every plan — Starter, Essential, Premium (shared, tenant-safe) and
Dedicated, BYOC (full-stack) — and auto-detects which it is.

The user-facing command is always:

```bash
python3 scripts/health_inspection.py inspect --cluster-id "<id>" --relative-window 7d --output-dir "./output/<id>"
```

The same runner also offers `inspect-org` (screen every cluster in an organization
and aggregate). Feishu/Lark delivery is done with the global `lark-cli` as the
user's own identity (see "Deliver the report" in `SKILL.md`), not a built-in command.

Other Python files are implementation modules, not alternate workflows.

---

## 2. Design principles

Everything in the skill follows from six principles:

1. **Deterministic Python first.** All measurement, threshold decisions, and
   status come from Python. An LLM may *interpret* flagged risks, but never
   changes a status, number, or persisted artifact.
2. **Fail closed.** Missing, empty, unauthorized, prohibited, or non-comparable
   evidence is `N/A` with a reason — never silently `Healthy`, never `0`. An
   `Unknown` plan is default-deny and stops before any metric is read.
3. **Tenant safety is structural, not best-effort.** For shared plans, physical
   infrastructure is never even queried; the registry marks those checks `X` and
   selection never emits them, so there is nothing to accidentally leak.
4. **One source of truth per concern.** Applicability, collection, and thresholds
   each live in exactly one place (§4), cross-validated so they cannot drift.
5. **Selection drives collection.** The registry decides *what* to check first;
   collectors only gather what was selected (§6). Prohibited checks can't be run
   by construction.
6. **Privacy by construction in the report.** The customer report is rendered only
   from canonical statuses and curated labels, so raw evidence, instance names,
   URLs, and PromQL cannot reach it (§11).

---

## 3. Architecture and data flow

```text
cluster discovery (clinic_api)
  -> canonical plan routing          (inspection_core.normalize_plan)
  -> registry selection              (checklist_registry: what to check / what is prohibited)
  -> plan-safe collection            (logical: collection_adapters;  full-stack: collector + dedicated_binding)
  -> normalized checklist evaluation (checklist_engine: bind, fail-closed N/A, status + confidence)
  -> deterministic gate / state      (inspection_gate: coverage, signature, notification owner)
  -> internal evidence bundle        (artifacts/, normalized.json, gate.json, state/)
  -> customer-safe report            (customer_report.render_health_report)
  -> artifact + privacy validation   (health_inspection.validate_artifacts)
```

Module responsibilities:

| Module | Responsibility |
|---|---|
| `scripts/health_inspection.py` | The single public runner: discovery → routing → collection → evaluation → gate → report → validation. |
| `scripts/clinic_api.py` | Zero-dependency Clinic / Data Proxy HTTP client (metrics, SQL, metadata). |
| `scripts/inspection_core.py` | Shared primitives: time-window parsing, percentiles (linear R-7), distributions, status thresholds, plan normalization. |
| `scripts/checklist_registry.py` | Loads/validates the registry; selects checks by plan/profile/scope/capability; generates the Markdown matrix. |
| `scripts/collection_adapters.py` | Selection-driven collection layer: recipes + adapters + per-run query cache; `apply_rule`; offline recipe/rule validation. |
| `scripts/dedicated_binding.py` | [test oracle] Maps a dedicated collector report to canonical `check_results`. The monolith (`dedicated_health_inspection.py`) was retired 2026-11-25; Dedicated/BYOC now collects natively via `collection_adapters`. This module is kept only as the reference oracle for the CDC parity + golden shape guardrails. See `references/dedicated-migration-plan.md`. |
| `scripts/dedicated_golden.py` | [test oracle] Golden shape capture/compare for the 23 Dedicated checks (regression anchor against `tests/fixtures/dedicated_golden.json`). |
| `scripts/checklist_engine.py` | Binds collector results to the selection, rejects prohibited/out-of-scope, synthesizes N/A, computes status + confidence. |
| `scripts/inspection_gate.py` | Persists state, coverage, finding/checklist signatures; decides the single notification owner; never calls an LLM or sends a notification. |
| `scripts/customer_report.py` | Single customer-report renderer for every plan + the privacy validator. |
| `scripts/report_to_html.py` | Bundles `health-report.md` + its chart PNGs into one self-contained HTML (base64-inlined) for local preview; Feishu publishing goes through the global `lark-cli` instead. |

---

## 4. The three sources of truth

Applicability, collection, and scoring are separated so each can change
independently and be validated against the others:

| File | Owns | Answers |
|---|---|---|
| `config/checklist.v1.json` | Categories, Check IDs, per-plan applicability (`R/C/D/X/—`), profile, scope, capability keys, implementation status | **What** may be checked, and what is prohibited |
| `config/collection.v1.json` | A recipe per Check ID (`adapter` + `params`) | **How** each check is collected |
| `config/rules.v1.json` | Named thresholds (`high`/`low`/`params`) | **How** a value becomes a status |

Each has a published JSON Schema (`*.schema.json`), and the runtime also
cross-validates them:

```bash
python3 scripts/checklist_registry.py validate         # registry is well-formed
python3 scripts/checklist_registry.py verify-markdown   # generated matrix is not stale
python3 scripts/collection_adapters.py validate         # recipes/rules consistent with the registry
```

`collection_adapters.py validate` fails if a recipe references an unknown Check ID
or adapter, an `implemented` check has no recipe, a referenced rule is undefined, a
derived check (`worst_of`) references a missing or itself-derived member, or a rule
has non-numeric or non-monotonic bands.

---

## 5. Plan routing and the evidence boundary

The plan is discovered from cluster metadata (`deployTypeV2`/`deployType`) and
normalized to one of `Starter`, `Essential`, `Premium`, `Dedicated`, `BYOC`, or
`Unknown`. `--expected-plan` may assert the result but cannot override it.

- **Starter / Essential / Premium** use the selection-driven collection layer
  (`collection_adapters.py`, recipes in `config/collection.v1.json`, thresholds in
  `config/rules.v1.json`) and **tenant-scoped logical-cluster evidence only**.
  Shared physical TiKV/PD CPU and memory, node and disk metrics, physical store
  capacity, Region/Peer/leader distribution, scheduler state, compaction, Raft,
  physical topology, and pool logs are excluded — the registry marks those
  categories `X` for these plans and a hard invariant in `checklist_registry.py`
  enforces it.
- **Dedicated / BYOC** use the full-stack collector, bound to canonical Check IDs
  through `dedicated_binding.py`; the collector's own verdict remains the
  authoritative overall status (§9).
- **Unknown** is default-deny and fails before metric collection.

See [`references/plan-and-data-scope.md`](references/plan-and-data-scope.md) for the
full per-plan scope.

---

## 6. Selection-driven collection

Collection never owns a fixed metric set. The registry selection runs first and
returns `runnable_check_ids`; the collector iterates exactly those, looks up each
one's recipe, and runs the named **adapter**. Consequences:

- A prohibited (`X`) check is never in `runnable_check_ids`, so it is never
  collected — fail-closed at collection time.
- A runnable check with no recipe becomes `N/A(no_collection_recipe)`; it never
  crashes the run.
- A per-run query cache de-duplicates shared PromQL, so one-recipe-per-check does
  not multiply API calls.

Adapters (in `collection_adapters.py`):

| Adapter | Use |
|---|---|
| `promql_distribution` | A range or instant metric → distribution/scalar; optional threshold `rule`. |
| `promql_ratio` | numerator/denominator rates → ratio (+ optional estimated count). |
| `cluster_metadata` | Picks whitelisted metadata fields. |
| `scope_guard` | Asserts tenant-safe scope (no physical infrastructure queried). |
| `connections_headroom` | Collects connections but stays N/A until a configured limit source exists. |
| `ru_consumption` | RU rate distribution + exact window total + legacy cross-check. |
| `logical_storage_usage`, `availability_status` | Fail-closed N/A until a verified source is bound. |
| `worst_of` *(derived)* | Summarizes the worst status across sibling checks; runs in a second pass. |
| `tikv_thread_pool_pressure` | Stub, pending the dedicated per-thread port. |

Thresholds are resolved by name through `apply_rule`:

- `high` — larger is worse; `low` — smaller is worse.
- `notice` defaults to `warning` (no NOTICE band); `critical` is optional (omit to
  never escalate to CRITICAL).
- `params` rules hold knobs read by a named adapter that owns its own logic.
- Unknown/malformed rules fail closed to `N/A`.

---

## 7. The checklist model (categories & checks)

The registry is organized into 14 categories (`CAT-00`…`CAT-13`), each scoped to a
component and a target scope. Checks have a stable ID (`SCOPE-01`, `SQL-02`,
`TIKV-01`, …) and, per plan, one applicability mark:

| Mark | Meaning |
|---|---|
| `R` | Required attempt in default Health; missing evidence → N/A with a reason. |
| `C` | Capability-gated; run only after capability + source validation. |
| `D` | Explicit component deep dive only. |
| `X` | Excluded for this plan — the runtime must never query it. |
| `—` | Product capability not applicable to this plan. |

Each check also carries an honest **implementation status**: `implemented`,
`partial`, or `planned`. A `planned` check is selected but returns `N/A`
(`no_runtime_adapter`) — the matrix never overstates what runs.

The human-readable catalog is **generated** from the registry and must not be
hand-edited:

- [`references/plan-checklist-matrix.md`](references/plan-checklist-matrix.md) — the
  full category map, check matrix, per-plan marks, and runtime status.

Regenerate and verify it with:

```bash
python3 scripts/checklist_registry.py render-markdown --output references/plan-checklist-matrix.md
python3 scripts/checklist_registry.py verify-markdown
```

Deeper design references: [`references/core-rule-catalog.md`](references/core-rule-catalog.md),
[`references/baseline-and-alerting.md`](references/baseline-and-alerting.md), and
[`references/tikv-inspection-rules.md`](references/tikv-inspection-rules.md).

---

## 8. Evaluation, status, and confidence

`checklist_engine.evaluate()` takes the collector output plus the selection and:

1. validates the time window,
2. binds each reported result to a selected Check ID,
3. **rejects** any prohibited or out-of-selection result (fail-closed error),
4. synthesizes explicit `N/A` for selected checks with no result,
5. computes the overall status and confidence.

Status is the worst of all evaluated checks:

```text
CRITICAL > WARNING > NOTICE > HEALTHY
```

`N/A` means there is no evaluated signal (it is not ranked as healthy). Confidence
reflects evaluated coverage: `HIGH` only when nothing is N/A, `MEDIUM` when some
signal exists but coverage is incomplete, `LOW` when there is no ranked signal.

The engine also emits `analysis_candidates` (the `WARNING`/`CRITICAL` checks with
their reasons) and `deep_dive_recommended`, so the interpretation step has an
explicit list of what to look at without re-scanning every result.

### Percentile semantics

For QPS, RU, request duration, and request wait, **P99 across the inspection
window** is the primary analysis and display value; P50/P95 describe the baseline;
P999 and Max are supplementary tail/peak signals. Time-window percentiles use the
linear R-7 interpolation method (compatible with NumPy's default). Reports
distinguish the per-sample percentile from the percentile across time (e.g.
"request-latency P99 series; 7-day time-window P99").

---

## 9. Dedicated / BYOC binding

The Dedicated/BYOC collector is a large single pass organized around *findings*
and per-domain signals, not Check IDs. Rather than re-implement its live-only
PromQL and sustained-window logic, `dedicated_binding.py` maps its **structured**
output (`analysis`, `metrics`, `unknown_checks`) to canonical `check_results`, and
`health_inspection.collect_full_stack` routes those through the engine. So the
full-stack path no longer bypasses the registry, and coverage is recorded as
`bound`.

Two safeguards:

- The bound results are filtered to the registry selection, so a registry change
  can never make the engine reject a hard-coded id and crash the run.
- The overall status is `worst(collector_verdict, engine_status)` — the binding may
  only **raise** severity, never mask it. The collector's verdict stays
  authoritative; today only a structurally derivable subset of full-stack checks is
  bound, and the rest are explicit `N/A`.

---

## 10. Gate, state, and the interpretation boundary

`inspection_gate.py` is deterministic. It persists the registry version, selected
Check IDs, coverage, a finding signature, and run state; detects change vs the
previous run; and emits exactly one `notification_owner` (`python`, `agent`, or
`none`). It never calls an LLM and never sends a notification.

Severity alone does not require an LLM. Deterministic WARNING and CRITICAL results
stay owned by Python. An LLM/agent interpretation step is requested only when a
collector sets `llm_required` / `analysis_required` with a reason, or via the
operator overrides `--llm-for-warning` / `--llm-for-critical`. The interpretation
step may add narrative or root-cause analysis; it must never change the status,
evidence, or persistence.

The selected time range is part of the evidence and state contract: runs for
different ranges are not interchangeable evidence. See
[`references/automation-and-cron.md`](references/automation-and-cron.md) for
scheduled execution and [`references/organization-workflow.md`](references/organization-workflow.md)
for multi-cluster screening.

---

## 11. Reports and privacy

One renderer, `customer_report.render_health_report`, produces **one unified
9-section report for every plan** — the structure is identical; each section fills
what the plan's capability allows and renders N/A (with a reason) otherwise. The
full rationale is in [`references/report-design.md`](references/report-design.md)
(industry-grounded: SRE golden signals / USE / RED, AWS Well-Architected, ADDM).

Skeleton (all plans): `1. 执行摘要` → `2. 基础信息与基线` → `3. 工作负载健康 (RED)`
（本周期/上周期/趋势 对比表）→ `4. 资源健康 (USE)`（4.1 TiDB / 4.2 TiKV / 4.3 PD /
4.4 基础设施）→ `5. SQL 分析`（owner Top-N）→ `6. 可靠性/HA/备份/DR` → `7. 配置与安全`
→ `8. 发现与优先建议`（LLM narrative slots）→ `9. 巡检覆盖` + 附录.

- **Two-layer model**: workload health (RED/golden signals) in §3 over resource
  health (USE) in §4. Shared plans fill §3 + tenant-safe parts of §4 and N/A the
  physical layer by design; Dedicated/BYOC fill §4 component status tables.
- **Language**: Chinese by default, English via `--lang en`; labels from a `zh/en`
  dictionary.
- Python renders the deterministic scaffold + tables from canonical statuses and
  curated labels; a section with no bound collector renders a narrative slot — never
  fabricated content.

**Previous-period comparison.** The shared runner collects the equal-duration
window immediately preceding the current one (`collect_logical` runs the pipeline
twice) and the renderer shows each usage metric's current / previous / trend %. A
missing previous window yields trend `N/A`, never a fabricated comparison.

**Owner-facing SQL Top-N.** The shared-plan report includes the customer's own SQL
Top-N (RU / slow-frequency / slow-duration), identified by **SQL digest** (an opaque
hex id), never raw SQL text. The privacy boundary is redefined accordingly: the
risk is cross-tenant, infrastructure, or internal-identifier leakage — not a
customer seeing their own SQL. SQLD-01/02/03 are therefore part of shared-plan
Health; SQLD-04..09 (avg/exec/keys/full digest list/normalized text/users) remain
explicit deep-dive only.

Because the renderer reads only statuses and whitelisted fields, raw evidence,
instance names, pods, Grafana links, and PromQL cannot reach the customer report by
construction. `validate_customer_report` is a structural safety net on top: it
rejects a report containing internal check IDs, URLs, PromQL/infrastructure tokens,
or forbidden terminology **in English or Chinese**. Missing or prohibited evidence
is `N/A`, never Healthy.

**Source bindings still pending (render N/A until bound).** Slow-log total count
(SQL-06), active sessions (SQL-08), logical storage start/end/growth (LST-01/02/03),
and statistics health are wired but have no verified shared-plan metric/API source
yet; each is a one-line recipe once the source is confirmed.

The report structure is defined in `references/report-design.md` (one unified skeleton).
(The `config/report-contract.v1.json` auto-loading and cross-format PDF/DOCX/Feishu
parity are not yet implemented; see §14.)

---

## 12. Artifacts

Every successful run writes, under `--output-dir`:

- `health-report.md` — customer-safe report (every plan).
- `collector.json` — raw collector output.
- `normalized.json` — the canonical internal model (`cluster_id`, `plan`, `window`,
  `status`, `confidence`, `check_results[]`, selection, coverage).
- `gate.json` — deterministic gate result.
- `artifacts/<run_id>/` — immutable internal evidence bundle.
- `state/<target>.json` — state and idempotency data.
- `validation.json` — artifact and privacy validation result.

A failed run writes `failure.json` and exits non-zero; it never substitutes a
temporary collector or a fabricated result.

---

## 13. Running, environment, and tests

`clinic_api.py` reads credentials from `--env-file` (default `.env`) or the
environment:

- `CLINIC_API_KEY` (required; Bearer token)
- `CLINIC_ENDPOINT` (optional override; defaults to the prod Clinic endpoint)

For Feishu delivery, authenticate the global `lark-cli` once (`lark-cli auth login`;
the user completes OAuth in a browser, credentials stored in the OS keychain) — no
Feishu secrets live in `.env`. Inject the Clinic key at runtime; never commit it.

Validation gates (all offline, standard library only):

```bash
python3 -m py_compile scripts/*.py tests/*.py
python3 -m unittest discover -s tests -v
python3 scripts/checklist_registry.py validate
python3 scripts/checklist_registry.py verify-markdown
python3 scripts/collection_adapters.py validate
```

---

## 14. Extending the skill

To add or change a check, touch the sources of truth in order — never hand-edit the
generated matrix:

1. **Applicability** — add/modify the check in `config/checklist.v1.json` (ID,
   category, per-plan `R/C/D/X/—`, capability key, implementation status).
2. **Collection** — add a recipe in `config/collection.v1.json` mapping the Check
   ID to an adapter + params (reuse a generic adapter where possible; add a named
   adapter in `collection_adapters.py` only for real logic).
3. **Thresholds** — add the named rule in `config/rules.v1.json`.
4. **Regenerate** the matrix and run all validation gates.
5. **Test** — add coverage for the new route, threshold, N/A reason, and any
   report field.

Maintenance rules:

- Keep applicability in the registry, metric semantics/thresholds in config, not in
  the skill prompt.
- Do not add a second public collector command; extend the unified runner and the
  canonical model.
- Mark a check `implemented` only once it emits canonical `check_results`.

### Current limitations / roadmap

- `config/report-contract.v1.json` is documentation only (not auto-loaded). Feishu
  delivery runs through the global `lark-cli`; PDF/DOCX multi-format parity does not.
- Full-stack Dedicated/BYOC binding now covers the TiKV, TiDB and infrastructure
  domains: TiKV CPU/memory/gRPC/compaction/read-pool/restarts/write-stability/flow-
  control/error-signals/slow-score/raft-lag, **TiDB compute-node CPU (vs GOMAXPROCS)
  and RSS**, disk capacity/IO/latency, and region balance. Domains the collector does
  not yet expose structurally — SQL & execution plan, statistics, index,
  transaction/locking, backup/DR — remain `N/A` narrative slots in the report.
  (Thresholds live in `config/rules.v1.json`, read by both the collector and the
  binding — no longer hardcoded. TiDB RSS is observation-only: no verified product
  memory limit for a logical %.)
- TiFlash and the SQL component deep dive are not yet routed through the unified
  runner (organization screening is, via `inspect-org`).
- The interpretation step has an explicit signal (`analysis_candidates`) and can be
  forced by the gate's `llm_required`, but no collector sets `llm_required`
  automatically yet.
