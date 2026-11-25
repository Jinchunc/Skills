# Instance Inspection — Domain Methodology

> Engine-agnostic health check framework for database instances and clusters.
> Systematic checklist inspection producing a scored health report.

## Activation

User requests: health check, inspection, cluster review, report generation, routine check.

## Engine Detection and Provider Loading

Follow the canonical procedure in `INDEX.md#engine-detection-and-provider-loading` (engine detection 3-step, conditional engine skill loading, DISPATCH Layer 3 provider loading).

---

## Fleet / Org-wide Mode

Activated when the request targets a whole organization rather than one cluster — signals: org, tenant, all clusters, fleet, overview, 整体情况, 全部集群, 总览, 盘点 (e.g. "整理某 org 下所有集群的整体情况").

Fleet mode is a **different workflow** from the single-cluster framework below. Do NOT run the full 6-category checklist per cluster — with dozens of clusters that is slow and likely to abort midway. Instead:

1. **Enumerate** — list all clusters belonging to the org/tenant. Paginate until an empty page; never assume the first page is complete. Verify the final count and report it.
2. **Filter** — keep active clusters; de-duplicate; drop branch/derived cluster records; confirm each candidate actually belongs to the requested org (fuzzy search may over-include).
3. **Lightweight per-cluster collection** — for each cluster collect metadata (name, ID, product line/tier, region, version, status, node topology) plus a small fixed set of key metrics (QPS, latency, CPU, storage usage). **Isolate failures**: if one cluster fails or times out, mark it `UNREACHABLE` and continue with the rest — a single bad cluster must not abort the fleet run.
4. **Aggregate report** — fleet-level output: cluster inventory table, distribution statistics (versions, tiers, regions, total scale), an exceptions section (non-active, unreachable, metric anomalies), and drill-down pointers.

Drill-down: when the user asks for depth on one specific cluster, re-enter this domain's standard single-cluster framework below for that cluster only.

→ Engine: provides the concrete enumeration workflow, per-cluster query set, and fleet report template (engine playbook).

---

## Health Check Framework

Inspect 6 categories in order. Each category contains multiple check items.

| # | Category | Focus |
|---|----------|-------|
| 1 | **Cluster Overview** | Topology, version consistency, uptime, basic status |
| 2 | **Resource Health** | CPU, memory, disk, I/O utilization per component |
| 3 | **Performance Health** | Latency percentiles, QPS, write stalls, slow queries, coordinator latency |
| 4 | **Storage Health** | Data distribution balance, compaction, garbage collection, MVCC versions |
| 5 | **Availability Risk** | Instance outliers, error rates, backoff/retry rates, failover readiness |
| 6 | **Security Quick-Scan** | Auth configuration, encryption status, privilege review (quick, not deep audit) |

→ Engine: provides specific check items, metrics, thresholds, and normal ranges per category.
→ Provider: follow DISPATCH.md Layer 3 Provider Loading Protocol (progressive: data API → product line → billing).

## Scoring Model

Each check item receives a 4-level score:

| Score | Meaning | Criteria |
|-------|---------|----------|
| **HEALTHY** | Normal | Within expected range for this workload |
| **NOTICE** | Attention | Deviates from normal but no service impact |
| **WARNING** | Action needed | Should be addressed soon to prevent degradation |
| **CRITICAL** | Immediate action | Needs immediate attention, risk of outage or data loss |

**Rules:**
- Category score = worst score among its check items
- Overall score = worst score among all categories
- When a metric is unavailable: mark as `N/A (metric unavailable)`, do not block other items
- If >50% of items are N/A: overall confidence = LOW, note in report

## Check Item Structure

For each check item, the engine provides:

| Field | Description |
|-------|-------------|
| ID | Unique identifier (e.g., C1, R2, P5) |
| Name | What is being checked |
| Metric | What to query |
| Thresholds | HEALTHY/NOTICE/WARNING/CRITICAL boundaries |
| Why | Why this check matters |
| Action | What to do if non-HEALTHY |

## Execution Order

1. **Cluster overview first** — establishes context (topology, version, product line)
2. **Resources** — physical resource utilization
3. **Performance** — application-level health
4. **Storage** — data layer health
5. **Availability** — risk assessment
6. **Security** — quick security scan

Execute all items even if early categories show CRITICAL — the full picture matters.

## Baseline Comparison

When historical data is available:
- Compare current values against same time window in previous week
- Flag items that degraded (were HEALTHY, now WARNING)
- Note items that improved
- Trend analysis: is the cluster getting better or worse over time?

## Report Generation

Output format:

```
# Health Inspection Report

**Cluster:** {name/id}
**Engine:** {engine} {version}
**Inspection Time:** {timestamp}
**Overall Score:** {worst category score}

## Summary
- {count} HEALTHY / {count} NOTICE / {count} WARNING / {count} CRITICAL / {count} N/A
- Priority actions: {top 3 most urgent items}

## Category Details

### 1. Cluster Overview — {score}
| ID | Check | Score | Value | Threshold | Note |
|....|.......|.......|.......|...........|......|

### 2. Resource Health — {score}
...

## Priority Actions
1. [CRITICAL] {item} — {recommended action}
2. [WARNING] {item} — {recommended action}
...
```

All change recommendations labeled "recommended for user execution."

## Terminal State

Inspection is complete when: all 6 categories evaluated, per-item scores assigned, overall score computed, priority actions listed, and the report follows the format above. If baseline data is available, include trend comparison.

## Component-Specific Inspections

When the user requests inspection of a specific component (e.g., "check replication health", "inspect change data capture"), or the general inspection reveals component-specific findings:

→ Engine: provides component-specific inspection modules under `engines/<engine>/instance-inspection/<component>/SKILL.md`

Component inspections follow the same scoring model but have their own specialized metrics and thresholds.

## Cross-Domain

If inspection reveals issues belonging to another domain:
- CRITICAL performance items → load `domains/performance-optimization/skill.md` for deep diagnosis
- CRITICAL availability risks → load `domains/fault-diagnosis/skill.md` for proactive investigation
- Security findings beyond quick-scan → load `domains/compliance-audit/skill.md` when available (domain under development)
