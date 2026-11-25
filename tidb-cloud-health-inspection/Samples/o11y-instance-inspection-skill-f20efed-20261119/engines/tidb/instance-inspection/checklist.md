# Inspection Checklist

> Comprehensive health check, executed item by item, outputting 4-level scoring
> Data source: Clinic API, all PromQL filtered with `tidb_cluster_id="{cluster_id}"`
>
> **Automation:** the deterministic subset (~29 of the 45 items) is collected and scored by
> `providers/tidb-cloud/ops/scripts/checklist-collect.sh <cluster_id>` with per-tier routing
> built in — run it first, then use this file for interpretation, the not-collected items,
> and recommendations.

---

## Scoring Semantics (口径声明)

This checklist uses **instantaneous snapshot scoring**: every threshold is evaluated
against the metric value at the moment of inspection, and the score reflects the
current immediate state. All threshold numbers in this file are the baseline for
the instantaneous interpretation.

The same metrics under the **sustained-window trend** interpretation (e.g., CPU
staying high for 30 minutes before warning) are defined in
`playbooks/dedicated-daily/references/tikv-daily-inspection-rules.md`. Where the
two files appear to differ (e.g., compaction pending bytes, CPU), the numbers
here are the instantaneous-snapshot baseline; see the daily rules file for the
window-based trend semantics.

---

## Prerequisite 0: Cluster Topology Verification (Starter/Essential/Premium only)

For Starter/Essential/Premium clusters, verify the cluster topology BEFORE any metric collection. Essential and Premium share the same two-layer architecture: logical clusters (keyspaces) run on shared physical resource pools. A single physical pool hosts multiple logical clusters.

1. Call Clinic API `/clinic/api/v1/dashboard/clusters` — the authoritative cluster registry
2. Identify logical clusters: `deployType` in `{essential, premium, starter}` AND `active=true` (ID pattern `10...`)
3. Exclude physical pools: `deployType=nextgen-host` (arbitrary snowflake IDs — identify by deploy type, never by ID prefix) — these are multi-tenant infrastructure, not clusters
4. Map each logical cluster to its `cluster_name` and `parentID` (resource pool)
5. Confirm: are you inspecting logical clusters or the physical pool? Metrics differ completely.

See `knowledge/product-lines/tidb-x.md` for the shared architecture, and `knowledge/product-lines/essential.md` / `premium.md` / `starter.md` for tier-specific details.

**Tier-specific notes:**
- **Premium / Essential**: logical cluster metrics use the keyspace-scoped `tidb_cluster_id`; TiKV/PD metrics may aggregate the whole physical pool unless filtered by keyspace. Use SQL-layer metrics (`tidb_server_*`) for per-cluster business QPS/latency.
- **Starter**: also runs on shared pools, but resource limits and available metrics differ; some metrics may 500 — mark N/A and continue.

## Prerequisite 1: MODULE-CLUSTER-ID

Cluster information must be obtained before executing the inspection:
- cluster_id, product_line, version, topology
- For Starter/Essential/Premium: use the logical cluster ID (`10...`), not the physical pool ID
- Determine which check items are applicable (e.g., RCU only for Starter/Essential/Premium)

---

## Scoring Criteria

| Score | Meaning | Trigger Condition |
|-------|---------|-------------------|
| HEALTHY | Normal | Within expected range |
| NOTICE | Attention | Deviates from normal but does not affect service |
| WARNING | Warning | Needs to be addressed soon |
| CRITICAL | Critical | Needs immediate attention |

When metrics are unavailable: Mark as `N/A (metric unavailable)`, do not block other check items.

---

## 1. Cluster Overview

### C1: Cluster Information

```
Data source: clinic_search_cluster_by_id
Record: cluster_name, region, provider, tier, version, status
Scoring: status == "active" → HEALTHY, otherwise → WARNING
```

### C2: Instance Topology and Uptime

```
query: time() - process_start_time_seconds{tidb_cluster_id="{cluster_id}"}

Check:
  - Record TiDB/TiKV/PD instance counts
  - Instance with shortest uptime

Scoring:
  All instances uptime > 24h        → HEALTHY
  Any instance uptime < 24h         → NOTICE ("Recent restart")
  Any instance uptime < 1h          → WARNING ("Just restarted")
```

### C3: Version Consistency

```
query: tikv_server_info{tidb_cluster_id="{cluster_id}"}

Check: Whether all TiKV instance versions are consistent
Scoring:
  Versions consistent → HEALTHY
  Versions inconsistent → WARNING ("Mixed version deployment")
```

---

## 2. Resource Health

### R1: TiDB CPU

```
query: rate(process_cpu_seconds_total{tidb_cluster_id="{cluster_id}", job="tidb"}[5m]) by (instance)

Unit: the query result is CPU cores used, not a percentage.
Calculation: utilization = used_cores / instance_cpu_cores x 100
(instance_cpu_cores from the cluster metadata topology/spec obtained in MODULE-CLUSTER-ID)

Scoring (take highest instance utilization):
  < 60%   → HEALTHY
  60~80%  → NOTICE
  80~95%  → WARNING
  > 95%   → CRITICAL
```

### R2: TiKV CPU (Process-level + Thread-level)

```
query_process: rate(process_cpu_seconds_total{tidb_cluster_id="{cluster_id}", job="tikv"}[5m]) by (instance)
query_thread:  sum(rate(tikv_thread_cpu_seconds_total{tidb_cluster_id="{cluster_id}"}[1m])) by (instance, name)

Unit: query_process result is CPU cores used, not a percentage.
Calculation: utilization = used_cores / instance_cpu_cores x 100
(for TiKV, instance_cpu_cores can also come from the tikv_server_cpu_cores_quota metric)

Scoring (process-level, take highest instance utilization):
  < 60%   → HEALTHY
  60~80%  → NOTICE
  80~95%  → WARNING
  > 95%   → CRITICAL

Additional checks (thread-level):
  grpc_server_* total CPU > 4 cores → WARNING ("gRPC thread bottleneck risk")
  raftstore_* single thread > 80%    → WARNING ("Raftstore single-thread bottleneck risk")
  cop_* total CPU > pool_size x 80% → NOTICE ("Coprocessor thread pool pressure")
```

### R3: PD CPU

```
query: rate(process_cpu_seconds_total{tidb_cluster_id="{cluster_id}", job="pd"}[5m]) by (instance)

Unit: the query result is CPU cores used, not a percentage.
Calculation: utilization = used_cores / instance_cpu_cores x 100
(instance_cpu_cores from the cluster metadata topology/spec obtained in MODULE-CLUSTER-ID)

Scoring:
  < 50%   → HEALTHY
  50~70%  → NOTICE
  70~90%  → WARNING
  > 90%   → CRITICAL
```

### R4: Memory

```
query_tidb: process_resident_memory_bytes{tidb_cluster_id="{cluster_id}", job="tidb"} by (instance)
query_tikv: process_resident_memory_bytes{tidb_cluster_id="{cluster_id}", job="tikv"} by (instance)

Scoring (requires knowing memory limit; if unknown, use absolute value trend):
  RSS / limit < 70%  → HEALTHY
  70~85%             → NOTICE
  85~95%             → WARNING
  > 95%              → CRITICAL

TiKV additional check:
  query: tikv_engine_block_cache_size_bytes{tidb_cluster_id="{cluster_id}"}
  block_cache / RSS > 50% → NOTICE ("Block cache ratio is high")
```

### R5: Disk Space

```
query_used:     tikv_store_size_bytes{tidb_cluster_id="{cluster_id}", type="used"} by (instance)
query_capacity: tikv_store_size_bytes{tidb_cluster_id="{cluster_id}", type="capacity"} by (instance)

Calculation: used_pct = used / capacity

Scoring (take highest instance):
  used < 60%   → HEALTHY
  60~80%       → NOTICE
  80~90%       → WARNING
  > 90%        → CRITICAL
```

### R6: Disk I/O Throughput

```
query_read:  sum(rate(node_disk_read_bytes_total{tidb_cluster_id="{cluster_id}"}[5m])) by (instance,device)
query_write: sum(rate(node_disk_written_bytes_total{tidb_cluster_id="{cluster_id}"}[5m])) by (instance,device)
query_total: query_read + query_write

Scoring (take highest instance):
  < baseline × 1.5              → HEALTHY
  baseline × 1.5 ~ baseline × 2.5  → NOTICE
  > baseline × 2.5              → WARNING (escalate to CRITICAL if sustained > baseline × 4.0)

Default baseline: derive from same time-of-day historical window
(e.g., bucket[10]=18,204,918,211 B/s from SaaS Prod v85-01 pattern).
Absolute fallback:
  < 20 GB/s  → HEALTHY
  20~45 GB/s → NOTICE
  45~80 GB/s → WARNING
  > 80 GB/s  → CRITICAL
```

---

## 3. Performance Health

### P1: Query Duration P99

```
query: histogram_quantile(0.99,
  sum(rate(tidb_server_handle_query_duration_seconds_bucket{tidb_cluster_id="{cluster_id}"}[5m]))
  by (le))

Scoring:
  < 50ms   → HEALTHY
  50~200ms → NOTICE
  200ms~1s → WARNING
  > 1s     → CRITICAL

Note: Thresholds vary by workload; these are general baselines. Compare against historical data if available.
```

### P2: Query Duration P999

```
query: histogram_quantile(0.999,
  sum(rate(tidb_server_handle_query_duration_seconds_bucket{tidb_cluster_id="{cluster_id}"}[5m]))
  by (le))

Scoring:
  < 200ms  → HEALTHY
  200ms~1s → NOTICE
  1~5s     → WARNING
  > 5s     → CRITICAL
```

### P3: Percentile Spread (P999/P99 ratio)

```
Calculation: ratio = P2 result / P1 result

Scoring:
  ratio < 3   → HEALTHY ("Tail latency under control")
  3~10        → NOTICE ("Some slow queries present")
  10~50       → WARNING ("Tail severely degraded, possible hotspot or single-instance issue")
  > 50        → CRITICAL ("Extreme tail latency")
```

### P4: QPS Stability

```
query: sum(rate(tidb_server_query_total{tidb_cluster_id="{cluster_id}"}[5m]))

Calculation: Coefficient of variation within the check window CV = std / mean

Scoring:
  CV < 0.2   → HEALTHY ("QPS stable")
  0.2~0.5    → NOTICE ("QPS has fluctuations")
  0.5~1.0    → WARNING ("QPS fluctuating severely")
  > 1.0      → CRITICAL ("QPS extremely unstable")
```

### P5: Write Stall

```
query: sum(rate(tikv_engine_write_stall_duration_seconds{tidb_cluster_id="{cluster_id}", db="kv"}[5m])) by (instance)

Unit: fraction of time spent in write stall (0–1)

Scoring (take highest instance):
  = 0              → HEALTHY
  0 < stall_ratio ≤ 0.01  → NOTICE
  0.01 < stall_ratio ≤ 0.05 → WARNING
  > 0.05           → CRITICAL
```

### P6: Flow Control

```
query: sum(rate(tikv_scheduler_throttle_action_total{tidb_cluster_id="{cluster_id}"}[5m]))

Scoring:
  = 0       → HEALTHY
  < 1/s     → NOTICE
  1~10/s    → WARNING
  > 10/s    → CRITICAL
```

### P7: Slow Query Count

```
Data source: Slowlog SQL
sql: SELECT COUNT(*) AS cnt
     FROM slowlogs
     WHERE date = '{today}'
Scoring:
  < 100/day  → HEALTHY
  100~1000   → NOTICE
  1000~10000 → WARNING
  > 10000    → CRITICAL
```

### P8: TSO Wait P99

```
query: histogram_quantile(0.99,
  sum(rate(tidb_tikvclient_ts_future_wait_seconds_bucket{tidb_cluster_id="{cluster_id}"}[5m]))
  by (le))

Scoring:
  < 5ms    → HEALTHY
  5~20ms   → NOTICE
  20~100ms → WARNING
  > 100ms  → CRITICAL
```

### P9: TSO RPC P99 (PD-side TSO latency)

```
query: histogram_quantile(0.99,
  sum(rate(pd_client_cmd_handle_cmds_duration_seconds_bucket{tidb_cluster_id="{cluster_id}", type="wait"}[2m]))
  by (le, instance))

Scoring (absolute + baseline-relative):
  Absolute: < 5ms → HEALTHY, 5~10ms → NOTICE, 10~15.5ms → WARNING, > 15.5ms → CRITICAL
  Baseline-relative (when baseline is established):
    < baseline × 3.0 → HEALTHY
    baseline × 3.0 ~ baseline × 5.0 → WARNING
    > baseline × 5.0 → CRITICAL

Default baseline: 3.1ms (from SaaS Prod v85-01 pattern — historical, not re-verified in 2026; prefer the cluster's own baseline).
```

### P10: TiKV gRPC P99

```
query: histogram_quantile(0.99,
  sum(rate(tikv_grpc_msg_duration_seconds_bucket{tidb_cluster_id="{cluster_id}"}[5m]))
  by (le, instance))

Scoring (absolute + baseline-relative):
  Absolute: < 79ms → HEALTHY, 79~197ms → WARNING, > 197ms → CRITICAL
  Baseline-relative (when baseline is established):
    < baseline × 2.0 → HEALTHY
    baseline × 2.0 ~ baseline × 5.0 → WARNING
    > baseline × 5.0 → CRITICAL

Default baseline: 39ms (from SaaS Prod v85-01 pattern — historical, not re-verified in 2026; prefer the cluster's own baseline).
```

### P11: Failed Query OPM (ratio)

```
query: sum(rate(tidb_server_execute_error_total{tidb_cluster_id="{cluster_id}"}[2m]))
        /
      sum(rate(tidb_server_query_total{tidb_cluster_id="{cluster_id}"}[2m]))

Scoring:
  < 0.1%  → HEALTHY
  0.1~1%  → NOTICE
  1~2%    → WARNING
  > 2%    → CRITICAL
```

### P12: TiDB Critical Errors

```
query: sum(rate(tikv_critical_error_total{tidb_cluster_id="{cluster_id}"}[5m])) by (instance, type)

Scoring:
  = 0         → HEALTHY
  0~0.5 err/s → NOTICE
  0.5~1 err/s → WARNING
  > 1 err/s   → CRITICAL
```

### P13: TiDB Connections

```
query: sum(tidb_server_connections{tidb_cluster_id="{cluster_id}"}) by (resource_group)

Scoring (absolute + baseline-relative):
  Absolute: < baseline × 1.5 → HEALTHY
            baseline × 1.5 ~ baseline × 2.5 → NOTICE
            > baseline × 2.5 → WARNING
  When no baseline: use 80% of configured max_connections as CRITICAL

Default baseline pattern: bucket[N] × 1.5 = NOTICE, bucket[N] × 2.5 = WARNING threshold.
```

### P14: QPS Drop / Surge

```
query_current: sum(rate(tidb_executor_statement_total{tidb_cluster_id="{cluster_id}"}[2m]))
query_baseline:  sum(rate(tidb_executor_statement_total{tidb_cluster_id="{cluster_id}"}[2m])) offset 1d

Or use a fixed baseline bucket (e.g., bucket[10], bucket[11], bucket[13], bucket[14], bucket[23]
corresponding to time-of-day historical QPS from SaaS Prod pattern).

Drop scoring:
  current > baseline × 0.7  → HEALTHY
  baseline × 0.5 < current ≤ baseline × 0.7 → NOTICE
  current ≤ baseline × 0.5 → WARNING/CRITICAL (use CRITICAL if < baseline × 0.3)

Surge scoring:
  current < baseline × 1.5  → HEALTHY
  baseline × 1.5 ≤ current < baseline × 2.5 → NOTICE
  current ≥ baseline × 2.5 → WARNING/CRITICAL (use CRITICAL if > baseline × 4)
```

---

## 4. Storage Health

### S1: Region Count Balance

```
query: tikv_raftstore_region_count{tidb_cluster_id="{cluster_id}", type="region"} by (instance)

Calculation: max / min across instances

Scoring:
  ratio < 1.2  → HEALTHY ("Balanced")
  1.2~1.5      → NOTICE
  1.5~2.0      → WARNING
  > 2.0        → CRITICAL ("Severely unbalanced")
```

### S2: Leader Distribution Balance

```
query: tikv_raftstore_region_count{tidb_cluster_id="{cluster_id}", type="leader"} by (instance)

Calculation: max / min across instances

Scoring:
  ratio < 1.3  → HEALTHY
  1.3~1.5      → NOTICE
  1.5~2.0      → WARNING
  > 2.0        → CRITICAL ("Leaders severely unbalanced, possible hotspot")
```

### S3: Compaction Pending Bytes

```
query: tikv_engine_pending_compaction_bytes{tidb_cluster_id="{cluster_id}"} by (instance)

Scoring (take highest instance):
  < 10GB   → HEALTHY
  10~50GB  → NOTICE
  50~100GB → WARNING ("Approaching soft-pending-compaction-bytes-limit 192GB default")
  > 100GB  → CRITICAL ("Compaction severely behind")
```

### S4: L0 File Count

```
query: tikv_engine_num_files_at_level{tidb_cluster_id="{cluster_id}", db="kv", level="0"} by (instance)

Scoring (take highest instance):
  < 10  → HEALTHY
  10~20 → NOTICE ("Approaching slowdown trigger")
  20~36 → WARNING ("Exceeds level0-slowdown-writes-trigger=20")
  > 36  → CRITICAL ("Exceeds level0-stop-writes-trigger=36")
```

### S5: GC Safe Point Lag

```
query: time() - max(tikv_gcworker_autogc_safe_point{tidb_cluster_id="{cluster_id}"}) / (2^18)

Note: The unit of `tikv_gcworker_autogc_safe_point` varies by TiDB version and
monitoring source. Treat the formula as a first-order approximation:
  - If the metric is a raw TSO, the precise conversion is `(TSO >> 18) / 1000` seconds.
  - If the metric is already a Unix timestamp in seconds, use `time() - max(...)` directly.
  - If in milliseconds, use `(time()*1000 - max(...)) / 1000`.
Cross-check with `tikv_gcworker_gc_task_duration_vec` when the unit is uncertain.
This metric may not exist in some environments; fail-open.

Scoring:
  < 600s (10min) → HEALTHY
  600s~1800s     → NOTICE
  1800s~3600s    → WARNING ("GC lagging, MVCC versions accumulating")
  > 3600s        → CRITICAL ("GC severely lagging")
```

### S6: MVCC Version Growth

```
query: tikv_engine_estimate_num_keys{tidb_cluster_id="{cluster_id}", db="kv"} by (instance)

Check: slope (growth trend) within the window
Scoring:
  slope <= 0 or < 1%/hour  → HEALTHY
  1~5%/hour                → NOTICE
  5~10%/hour               → WARNING
  > 10%/hour               → CRITICAL ("Keys growing rapidly, possible GC lag")
```

### S7: Store Available Ratio

```
query: sum(pd_scheduler_store_status{tidb_cluster_id="{cluster_id}", type="store_available"}) by (address, store)
        /
        sum(pd_scheduler_store_status{tidb_cluster_id="{cluster_id}", type="store_capacity"}) by (address, store)

Scoring (take lowest instance):
  > 40%  → HEALTHY
  30~40% → NOTICE
  20~30% → WARNING
  < 20%  → CRITICAL ("Store may run out of space soon")
```

### S8: Region Score Spread

```
query: pd_scheduler_store_status{tidb_cluster_id="{cluster_id}", type="region_score"} by (address, store)

Calculation: max(score) - min(score) across stores

Scoring: **REPORT-ONLY — thresholds pending recalibration.** The legacy
thresholds below assume a normalized 0–1 score, but production
`pd_scheduler_store_status{type="region_score"}` returns raw scores
(verified 2026-08-06: a healthy Dedicated cluster shows spread ≈ 35, a
Premium pool ≈ 29,000). Do NOT score this item until recalibrated against
SaaS Prod; report the raw spread as a relative trend only.
  (legacy, unverified: < 0.20 HEALTHY / 0.20~0.30 NOTICE / 0.30~0.40 WARNING / > 0.40 CRITICAL)
```

### S9: Pending Peer Count

```
query: sum(pd_regions_status{tidb_cluster_id="{cluster_id}", type="pending_peer_region_count"})

Scoring:
  < 100  → HEALTHY
  100~300 → NOTICE
  300~500 → WARNING
  > 500  → CRITICAL ("Too many regions waiting for peers")
```

---

## 5. Availability Risk

### A1: Instance Latency Outlier

```
query: histogram_quantile(0.99,
  sum(rate(tikv_grpc_msg_duration_seconds_bucket{tidb_cluster_id="{cluster_id}"}[5m]))
  by (le, instance))

Calculation: max_instance_p99 / avg_all_p99

Scoring:
  ratio < 1.5  → HEALTHY
  1.5~2.0      → NOTICE ("An instance is slightly slow")
  2.0~3.0      → WARNING ("Instance significantly abnormal")
  > 3.0        → CRITICAL ("Instance severely abnormal, may affect some requests")
```

### A2: gRPC Error Rate

```
query_error: sum(rate(tikv_grpc_msg_fail_total{tidb_cluster_id="{cluster_id}"}[5m]))
query_total: sum(rate(tikv_grpc_msg_duration_seconds_count{tidb_cluster_id="{cluster_id}"}[5m]))

Calculation: error_rate = error / total

Scoring:
  < 0.1%  → HEALTHY
  0.1~1%  → NOTICE
  1~5%    → WARNING
  > 5%    → CRITICAL
```

### A3: Backoff Frequency

```
query: sum(rate(tidb_tikvclient_backoff_seconds_count{tidb_cluster_id="{cluster_id}"}[5m])) by (type)

Scoring (total backoff rate):
  < 1/s   → HEALTHY
  1~10/s  → NOTICE
  10~100/s → WARNING
  > 100/s → CRITICAL

Additional: Report top 3 backoff types broken down by type
```

### A4: PD Scheduling Status

```
query: sum(rate(pd_schedule_operators_count{tidb_cluster_id="{cluster_id}"}[5m])) by (type)

Scoring:
  Has balance-leader + balance-region → HEALTHY ("Scheduling normal")
  No scheduling operations (but region/leader unbalanced) → WARNING ("Scheduling may be disabled")
  Scheduling frequency extremely high (> 100/s)            → NOTICE ("Intensive scheduling, may affect latency")
```

### A5: RCU Utilization / Resource-Control Queuing (Starter/Essential/Premium only)

```
Applicable: product_line == PREMIUM || STARTER_ESSENTIAL
Dedicated: Skip this item

Starter/Essential — headroom-based (resource_manager_resource_unit_service_limit
was removed upstream, verified 2026-08-06 — do not use it):
  query_provisioned: sum(serverless_metering_rcu_provisioned_capacity{keyspace_id="{ks_id}"})
  query_headroom:  resource_manager_resource_unit_available_ru{name="{ks_id}"}
  query_rate:      resource_manager_resource_unit_request_unit_consume_rate{name="{ks_id}"}
  Headroom metrics may have no series for some keyspace modes — then score
  from rc_wait evidence (rcu-check.md Step A2) instead of marking HEALTHY.
  Scoring:
    available_ru comfortably positive           → HEALTHY
    available_ru trending toward 0              → WARNING ("Approaching RCU limit")
    available_ru ~ 0 sustained / rc_wait > 0    → CRITICAL ("RCU capped")

Premium — resource-control queuing duration (no hard RCU limit):
  query_kv_wait_p99: histogram_quantile(0.99,
    sum(rate(resource_manager_client_request_success_bucket{tidb_cluster_id="{cluster_id}"}[2m]))
    by (le))
  query_throttle_rate: sum(rate(tidb_server_resource_control_throttled_total{tidb_cluster_id="{cluster_id}"}[2m]))
  Scoring (kv_wait_p99):
    < 5ms   → HEALTHY
    5~20ms  → NOTICE ("RC queuing present")
    20~50ms → WARNING ("Severe RC queuing")
    > 50ms  → CRITICAL ("Resource control throttling likely")
  Supporting signal: throttle_rate > 0 confirms throttling events.

Cross-check (all of Starter/Essential/Premium):
  query_rcu_consumption: sum(rate(tidb_server_request_unit_total{tidb_cluster_id="{cluster_id}"}[1m]))
```

### A6: PD Leader Changes

```
query: changes(pd_server_etcd_state{tidb_cluster_id="{cluster_id}", type="term"}[1h])

Scoring:
  = 0        → HEALTHY
  1~2        → NOTICE
  3          → WARNING
  > 3        → CRITICAL ("Frequent PD leader changes, TSO instability risk")
```

### A7: PD RPC Backoff (TiDB side)

```
query: sum(rate(tidb_tikvclient_backoff_seconds_count{tidb_cluster_id="{cluster_id}", type=~"pd.*"}[5m]))

Scoring:
  = 0     → HEALTHY
  0~2/s   → NOTICE
  2~5/s   → WARNING
  > 5/s   → CRITICAL ("TiDB is backing off from PD frequently")
```

### A8: PD Leader CPU

```
query: rate(process_cpu_seconds_total{tidb_cluster_id="{cluster_id}", job="pd"}[5m]) by (instance)

Calculation: utilization = used_cores / instance_cpu_cores × 100

Scoring:
  < 50%      → HEALTHY
  50~70%     → NOTICE
  70~90%     → WARNING
  > 90%      → CRITICAL

Note: SaaS Prod pattern uses 25.6 cores as PD leader CPU threshold; adjust by instance size.
```

### A9: Coprocessor Error Ratio

```
query: sum(rate(tikv_coprocessor_request_error{tidb_cluster_id="{cluster_id}"}[5m]))
        /
        sum(rate(tikv_coprocessor_request_duration_seconds_count{tidb_cluster_id="{cluster_id}"}[5m]))

Scoring:
  < 0.1%  → HEALTHY
  0.1~1%  → NOTICE
  1~2%    → WARNING
  > 2%    → CRITICAL
```

### A10: Component Restart Detection

```
query_uptime:   time() - process_start_time_seconds{tidb_cluster_id="{cluster_id}", job=~"tidb|tikv|pd|tiflash|ticdc"}
query_restarts: changes(process_start_time_seconds{tidb_cluster_id="{cluster_id}", job=~"tidb|tikv|pd|tiflash|ticdc"}[1h]) by (instance)

Scoring (per component):
  All instances uptime > 24h and restart_count = 0 → HEALTHY
  Any instance uptime < 24h                         → NOTICE ("Recent restart")
  Any instance uptime < 1h                          → WARNING ("Just restarted")
  Same component restart_count > 2 in window        → CRITICAL ("Frequent restarts")
```

---

## 6. Optimization Opportunities

### O1: Top 5 Slow SQL

```
Data source: Slowlog SQL
sql:
  SELECT digest, digest_text, COUNT(*) AS cnt,
         AVG(query_time) AS avg_time, MAX(query_time) AS max_time,
         AVG(total_keys) AS avg_keys
  FROM slowlogs
  WHERE date = '{today}'  GROUP BY digest, digest_text
  ORDER BY cnt DESC
  LIMIT 5

Scoring:
  No slow SQL          → HEALTHY
  Present but avg_time < 1s → NOTICE
  avg_time 1~10s    → WARNING
  avg_time > 10s    → CRITICAL

Output: List (digest_text summary + frequency + average duration)
```

### O2: Top 5 Resource-Consuming SQL

```
sql:
  SELECT digest, digest_text, COUNT(*) AS cnt,
         AVG(total_keys) AS avg_keys, AVG(mem_max) AS avg_mem,
         AVG(disk_max) AS avg_disk
  FROM slowlogs
  WHERE date = '{today}'  GROUP BY digest, digest_text
  ORDER BY avg_keys DESC
  LIMIT 5

Scoring:
  avg_keys < 10000    → HEALTHY
  10000~100000        → NOTICE
  100000~1000000      → WARNING
  > 1000000           → CRITICAL ("Wide-range scan")

Output: List (digest_text summary + avg_keys + avg_mem)
```

### O3: Block Cache Hit Rate

```
query_hit:  sum(rate(tikv_engine_cache_efficiency{tidb_cluster_id="{cluster_id}", type="block_cache_hit"}[5m]))
query_miss: sum(rate(tikv_engine_cache_efficiency{tidb_cluster_id="{cluster_id}", type="block_cache_miss"}[5m]))

Calculation: hit_rate = hit / (hit + miss)

Scoring:
  > 99%    → HEALTHY
  95~99%   → NOTICE
  90~95%   → WARNING
  < 90%    → CRITICAL ("Block cache hit rate extremely low")
```

---

## Execution Order

```
1. MODULE-CLUSTER-ID (Obtain cluster information)
2. C1~C3 (Cluster overview)
3. R1~R6 (Resources) ← Can be parallelized
4. P1~P14 (Performance) ← Can be parallelized
5. S1~S9 (Storage) ← Can be parallelized
6. A1~A10 (Availability) ← Can be parallelized
7. O1~O3 (Optimization items) ← Depends on Slowlog SQL, serial
8. Aggregate scores, generate report
```

Total **45 check items** (30 core + 15 derived from SaaS Prod / OP Asia-Pacific / Grafana Cluster Health Check patterns), of which R/P/S/A categories can be executed in parallel to speed up.

---

## Appendix: Document-Derived Metric Reference

This checklist incorporates concrete metrics, thresholds, and report formats from four source documents:

1. **SaaS Prod v85-01 巡检** — automatic inspection logs with L1 (ingress) and L2 (root-cause) metrics.
2. **[外部]TiDB OP 亚太 集群服务巡检总结报告 2026-07-18** — 16-cluster fleet summary covering 54 metric instances across 5 categories.
3. **health check.json** — Grafana "Cluster Health Check" dashboard with 55 panels and PromQL.
4. **[20260717][世界杯]OP 亚太_中东集群巡检指标** — per-cluster inspection template with topology, non-default parameters, and 5-category metric tables.

### Mapping to Checklist Items

| Document Metric | Checklist Item | Source |
|-----------------|----------------|--------|
| P99 Query Latency | P1 | SaaS Prod L1, health check.json Cluster Status |
| QPS Drop / Surge | P14 | SaaS Prod L1 |
| Failed Query OPM | P11 | SaaS Prod L1, OP 亚太 |
| TiDB Critical Errors | P12 | SaaS Prod L1 |
| TiDB Connections | P13 | SaaS Prod L1, OP 亚太 |
| TiKV CPU / Memory / Disk Usage | R2 / R4 / R5 | SaaS Prod L2, OP 亚太 |
| Disk Read/Write Bytes | R6 | SaaS Prod L2, health check.json Resource Utilization |
| TSO RPC p99 | P9 | SaaS Prod L2, health check.json Duration |
| PD Leader Changes | A6 | SaaS Prod L2, OP 亚太 |
| PD RPC Backoff | A7 | SaaS Prod L2 |
| PD Leader CPU | A8 | SaaS Prod L2 |
| Region Score Spread | S8 | SaaS Prod L2 |
| Pending Peer Count | S9 | SaaS Prod L2 |
| TiKV gRPC p99 | P10 | SaaS Prod L2, health check.json Duration |
| Coprocessor Error Ratio | A9 | SaaS Prod L2 |
| Store Available Ratio | S7 | health check.json Capacity |
| Region Count / Leader per TiKV | S1 / S2 | health check.json Capacity |
| Duration Breakdown (Parse, Compile, gRPC, Storage async write) | P8 + report-template D3 | health check.json Duration |
| Stability Metrics (Raft term, WAL fsync, Region health, Abnormal Stores, GC safe point, TiCDC lag) | A4 / A10 / S5 + report-template D4 | health check.json Stability, OP 亚太 |
| Resource Utilization (TiDB/TiKV/TiFlash/TiCDC CPU & Memory, Disk IOPS/Latency) | R1-R6 + report-template D5 | health check.json Resource Utilization, OP 亚太 |

### Key Threshold Patterns from SaaS Prod

| Metric | Baseline | Yellow Multiplier | Red Multiplier | Absolute Red |
|--------|----------|-------------------|----------------|--------------|
| P99 Query Latency | 48ms | ×2.0 | ×5.0 | 238ms |
| TSO RPC p99 | 3.1ms | ×3.0 | ×5.0 | 15.5ms |
| TiKV gRPC p99 | 39ms | ×2.0 | ×5.0 | 197ms |
| QPS Drop | bucket[N] | ×0.7 (Y) | ×0.5 (R) | — |
| QPS Surge | bucket[N] | ×1.5 (Y) | ×2.5 (R) | — |
| TiDB Connections | bucket[N] | ×1.5 (Y) | ×2.5 (R) | — |
| Disk Read/Write Bytes | bucket[N] | ×1.5 (Y) | ×2.5 (R) | — |

**Usage note:** When historical baseline is available, prefer baseline-relative thresholds. When unavailable, use the absolute thresholds in the checklist items above.

### Report Format Reference

- **SaaS Prod** format: L1/L2 two-layer summary, "今日累计: 🔴0 🟡0 🟢N", per-check status with reason.
- **OP 亚太** format: fleet summary table, per-cluster 54-item matrix, follow-up register with owner/ETA.
- **health check.json** format: 5 dashboard rows (Capacity, Cluster Status, Duration, Stability, Resource Utilization).
- **世界杯** format: per-cluster sections covering version, topology, non-default parameters, 5-category metrics, and recommendations.

Use [report-template.md](report-template.md) to render all four formats into a unified Markdown report.

---

## Component Health Modules

### TiFlash Health

For clusters with TiFlash, run the dedicated TiFlash health inspection skill. It covers Query, MPP, Resource, Storage IO, Network/Raft, and Pipeline domains.

→ [../tiflash-health-inspection/SKILL.md](../tiflash-health-inspection/SKILL.md)

### TiCDC Health (New Architecture)

For clusters running TiCDC new architecture, run the dedicated TiCDC health inspection skill. It covers Liveness, State, Control-plane, Lag, Throughput, Backpressure, Resource trends, Sink errors, DDL/Scheduler, and upstream TiKV.

→ [ticdc/SKILL.md](ticdc/SKILL.md)
