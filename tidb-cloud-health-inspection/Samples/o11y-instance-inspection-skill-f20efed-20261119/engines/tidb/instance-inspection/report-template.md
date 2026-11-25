# Inspection Report Template

> Convert TiDB cluster monitoring metrics and `checklist.md` scoring results into a structured, executive-ready health report. This template unifies four source formats: SaaS Prod automatic inspection, OP Asia-Pacific fleet summary, Grafana "Cluster Health Check" dashboard, and per-cluster World Cup inspection template.

---

## What This Template Is For

This template is the **reporting layer** of `tidb-instance-inspection`. It takes two kinds of inputs:

1. **Raw monitoring metrics** — values extracted from Prometheus / Clinic / O11Y / Grafana dashboards.
2. **Checklist scores** — the `HEALTHY / NOTICE / WARNING / CRITICAL` verdicts produced by the 45 items in [checklist.md](checklist.md).

It produces a Markdown report that is readable by non-technical stakeholders and actionable by DBAs.

**Automation:** both inputs are collected by scripts — per cluster,
`providers/tidb-cloud/ops/scripts/checklist-collect.sh <cluster_id>` returns the scored
checklist table plus the report-header inputs (`gc_life_time`, key config snapshot);
for fleets, `providers/tidb-cloud/ops/scripts/fleet-overview.sh <org_id>` feeds the
multi-cluster summary. The scripts collect and score; THIS template governs how the
report is written. `non_default_params` is only partially collectable (numeric configs
exported as metrics) — a full non-default diff needs `SHOW CONFIG` / TiUP; mark the
remainder N/A rather than inferring.

---

## Input Data Model

Before generating the report, normalize inputs into the following structure:

| Field | Description | Example Source |
|-------|-------------|----------------|
| `cluster_name` | Human-readable cluster name | Clinic metadata |
| `cluster_id` | Cluster identifier | Clinic / Prometheus label |
| `product_line` | Dedicated / Premium / Starter / Essential / BYOC | Clinic metadata |
| `version` | TiDB version | `tidb_server_info` |
| `topology` | Counts of TiDB / TiKV / PD / TiFlash / TiCDC | Clinic topology |
| `non_default_params` | TiKV/TiDB/PD/TiFlash config deviations from default | TiUP / Clinic config |
| `gc_life_time` | GC lifetime setting | Config / metric |
| `check_time` | Inspection timestamp | User-provided |
| `metrics` | Instant or window-aggregated metric values | Prometheus queries |
| `scores` | Per-check-item severity from checklist.md | Checklist evaluation |

**Metric availability rule:** If a metric cannot be collected, record it as `N/A` and explicitly state the gap. Never fabricate a value or silently downgrade severity.

---

## Scoring Semantics

Use the same 4-level scale as `checklist.md`:

| Level | Emoji | Meaning | Action |
|-------|-------|---------|--------|
| HEALTHY | 🟢 | Normal | None |
| NOTICE | 🟡 | Attention | Schedule follow-up |
| WARNING | 🟠 | Warning | Address in next maintenance window |
| CRITICAL | 🔴 | Critical | Immediate attention |
| N/A | ⚪ | Data missing | Do not infer; note gap |

**Aggregation rules:**

- Category score = worst score among items in that category.
- Overall score = worst score across all categories.
- A single 🔴 anywhere makes the whole report 🔴.

---

## Report Structure

### Single-Cluster Report Template

```markdown
# {{cluster_name}} TiDB 集群健康巡检报告

> **巡检时间：** {{check_time}}  
> **报告生成时间：** {{now}}  
> **集群信息：** {{product_line}} · TiDB {{version}} · {{region}}  
> **集群规模：** TiDB {{tidb_count}} / TiKV {{tikv_count}} / PD {{pd_count}} / TiFlash {{tiflash_count}} / TiCDC {{ticdc_count}}  
> **GC life time：** {{gc_life_time}}  
> **数据来源：** {{data_source}}  
> **报告版本：** v1.0

---

## 一、执行摘要

### 1.1 整体健康度

{{overall_emoji}} **整体状态：{{overall_status}}**

| 维度 | 状态 | 关键发现 |
|------|------|----------|
| L1 业务健康（入口指标） | {{emoji}} | {{summary}} |
| L2 根因定位（资源/存储/调度） | {{emoji}} | {{summary}} |
| 容量与增长 | {{emoji}} | {{summary}} |
| 查询耗时拆解 | {{emoji}} | {{summary}} |
| 稳定性与可靠性 | {{emoji}} | {{summary}} |
| 资源使用 | {{emoji}} | {{summary}} |

### 1.2 今日累计

📊 **今日累计：** 🔴{{critical_count}} 🟠{{warning_count}} 🟡{{notice_count}} 🟢{{healthy_count}}（共 {{total_checks}} 次巡检）  
**本次状态：** {{current_status}} | **触发原因：** {{reason}}

### 1.3 需要立即关注的事项

{{#if has_critical}}
- 🔴 **{{critical_count}} 项高风险**：{{list_critical_titles}}
{{/if}}
{{#if has_warning}}
- 🟠 **{{warning_count}} 项警告**：{{list_warning_titles}}
{{/if}}
{{#if has_notice}}
- 🟡 **{{notice_count}} 项注意**：{{list_notice_titles}}
{{/if}}
{{#unless has_risks}}
- ✅ 本次巡检未发现风险项，集群整体健康。
{{/unless}}

---

## 二、L1 业务健康（入口指标）

| 监控指标 | 当前值 | 阈值 | 状态 | 说明 |
|----------|--------|------|------|------|
| P99 Query Latency | {{p99_latency}} | {{p99_threshold}} | {{emoji}} | baseline={{baseline}} |
| QPS Drop | {{qps_drop}} | {{qps_drop_threshold}} | {{emoji}} | bucket[{{bucket}}]={{bucket_value}} ×0.5/0.7 |
| QPS Surge | {{qps_surge}} | {{qps_surge_threshold}} | {{emoji}} | bucket[{{bucket}}]={{bucket_value}} ×1.5/2.5 |
| Failed Query OPM (ratio) | {{failed_ratio}} | < 2.00% | {{emoji}} | {{comment}} |
| TiDB Critical Errors | {{critical_errors}} | < 1.00 err/s | {{emoji}} | {{comment}} |
| TiDB Connections | {{connections}} | {{conn_threshold}} | {{emoji}} | bucket[{{bucket}}]={{bucket_value}} ×1.5/2.5 |

---

## 三、L2 根因定位（资源 / 存储 / 调度）

| 监控指标 | 当前值 | 阈值 | 状态 | 说明 |
|----------|--------|------|------|------|
| TiDB CPU | {{tidb_cpu}} | 60/80/95% | {{emoji}} | {{comment}} |
| TiKV CPU | {{tikv_cpu}} | 60/80/95% | {{emoji}} | {{comment}} |
| PD CPU | {{pd_cpu}} | 50/70/90% | {{emoji}} | {{comment}} |
| TiKV Memory | {{tikv_mem}} | 70/85/95% | {{emoji}} | {{comment}} |
| TiKV Disk Usage | {{tikv_disk}} | 60/80/90% | {{emoji}} | {{comment}} |
| Disk Read/Write Bytes | {{disk_io_bytes}} | {{threshold}} | {{emoji}} | bucket[{{bucket}}]={{bucket_value}} ×1.5/2.5 |
| TSO Wait P99 (TiDB side) | {{tso_wait}} | 5/20/100ms | {{emoji}} | P8 from checklist.md |
| TSO RPC p99 | {{tso_rpc_p99}} | {{threshold}} | {{emoji}} | baseline={{baseline}} ×3.0/×5.0 |
| PD Leader Changes | {{leader_changes}} | < 3 | {{emoji}} | {{comment}} |
| PD RPC Backoff (tidb) | {{pd_backoff}} | < 2.00/s | {{emoji}} | {{comment}} |
| Region Score Spread | {{region_spread}} | 0.20/0.30/0.40 | {{emoji}} | {{comment}} |
| Pending Peer Count | {{pending_peer}} | < 500 | {{emoji}} | {{comment}} |
| TiKV gRPC p99 | {{tikv_grpc_p99}} | {{threshold}} | {{emoji}} | baseline={{baseline}} ×2.0/×5.0 |
| gRPC Error Rate | {{grpc_error}} | 0.1/1/5% | {{emoji}} | {{comment}} |
| Backoff Frequency | {{backoff_freq}} | 1/10/100/s | {{emoji}} | {{comment}} |
| Instance Latency Outlier | {{latency_outlier}} | 1.5/2.0/3.0× | {{emoji}} | max/avg P99 ratio |
| RCU Utilization / RC Queuing | {{rcu_util}} | tier-specific | {{emoji}} | Starter/Essential/Premium only |
| Coprocessor Error Ratio | {{copr_error}} | 0.1/1/2% | {{emoji}} | {{comment}} |

---

## 四、五维度详细指标

### 4.1 容量与增长

| 监控指标 | 当前值 | 阈值 / 参考值 | 状态 | 说明 |
|----------|--------|---------------|------|------|
| 数据容量 | {{storage_size}} | — | {{emoji}} | 日增长 {{daily_diff}} |
| Region 数量 | {{region_count}} | — | {{emoji}} | 日增长 {{region_daily_diff}} |
| Leader / TiKV | {{leader_per_tikv}} | — | {{emoji}} | {{distribution_comment}} |
| Region / TiKV | {{region_per_tikv}} | — | {{emoji}} | {{distribution_comment}} |
| Store available ratio | {{store_available_ratio}} | > 40% | {{emoji}} | {{comment}} |
| GC life time | {{gc_life_time}} | — | {{emoji}} | {{comment}} |

### 4.2 集群状态与流量

| 监控指标 | 当前值 | 阈值 | 状态 | 说明 |
|----------|--------|------|------|------|
| 实例数量 | TiDB {{tidb_count}} / TiKV {{tikv_count}} / PD {{pd_count}} / TiFlash {{tiflash_count}} / TiCDC {{ticdc_count}} | 与拓扑一致 | {{emoji}} | {{comment}} |
| 最短运行时长 | {{min_uptime}} | > 24h | {{emoji}} | {{comment}} |
| 异常重启次数 | TiDB {{tidb_restarts}} / TiKV {{tikv_restarts}} / PD {{pd_restarts}} / TiFlash {{tiflash_restarts}} / TiCDC {{ticdc_restarts}} | 0 | {{emoji}} | {{comment}} |
| QPS | {{qps}} | {{threshold}} | {{emoji}} | {{comment}} |
| TPS | {{tps}} | {{threshold}} | {{emoji}} | {{comment}} |
| CPS | {{cps}} | {{threshold}} | {{emoji}} | {{comment}} |
| Affected Rows | {{affected_rows}} | {{threshold}} | {{emoji}} | {{comment}} |
| Connections | {{connections}} | {{threshold}} | {{emoji}} | {{comment}} |
| Active Connections | {{active_connections}} | {{threshold}} | {{emoji}} | {{comment}} |
| Failed Query Ratio | {{failed_query_ratio}} | < 2.00% | {{emoji}} | {{comment}} |
| SQL Duration P99 | {{p99_latency}} | {{threshold}} | {{emoji}} | baseline={{baseline}} |
| SQL Duration P999 | {{p999_latency}} | {{threshold}} | {{emoji}} | baseline={{baseline}} |

### 4.3 查询耗时拆解

| 阶段 | 监控指标 | P99 | 阈值 | 状态 | 说明 |
|------|----------|-----|------|------|------|
| PD TSO Wait | `pd_client_cmd_handle_cmds_duration_seconds_bucket` | {{tso_wait}} | < {{threshold}} | {{emoji}} | {{comment}} |
| Get Token | `tidb_server_get_token_duration_seconds_bucket` | {{get_token}} | < {{threshold}} | {{emoji}} | {{comment}} |
| Parse | `tidb_session_parse_duration_seconds_bucket` | {{parse}} | < {{threshold}} | {{emoji}} | {{comment}} |
| Compile | `tidb_session_compile_duration_seconds_bucket` | {{compile}} | < {{threshold}} | {{emoji}} | {{comment}} |
| gRPC Message | `tikv_grpc_msg_duration_seconds_bucket` | {{grpc_msg}} | < {{threshold}} | {{emoji}} | {{comment}} |
| Storage Async Write | `tikv_storage_engine_async_request_duration_seconds_bucket` | {{async_write}} | < {{threshold}} | {{emoji}} | {{comment}} |

### 4.4 稳定性与可靠性

| 监控指标 | 当前值 | 阈值 | 状态 | 说明 |
|----------|--------|------|------|------|
| Raft term | {{raft_term}} | 稳定不变 | {{emoji}} | {{comment}} |
| 99% WAL fsync duration | {{wal_fsync}} | < {{threshold}} | {{emoji}} | {{comment}} |
| 99% backend commit duration | {{backend_commit}} | < {{threshold}} | {{emoji}} | {{comment}} |
| Syncer index | {{syncer_index}} | 稳定 | {{emoji}} | {{comment}} |
| Region health | {{region_health}} | 无异常 | {{emoji}} | {{comment}} |
| Abnormal Stores | {{abnormal_stores}} | 0 | {{emoji}} | {{comment}} |
| Stats Healthy Distribution | {{stats_healthy}} | 健康占比高 | {{emoji}} | {{comment}} |
| Pessimistic Statement Retry OPS | {{pessimistic_retry}} | 低 | {{emoji}} | {{comment}} |
| Critical error | {{critical_error}} | 0.5/1 err/s | {{emoji}} | P12 from checklist.md |
| Server is Busy | {{server_is_busy}} | 0 | {{emoji}} | {{comment}} |
| Approximate Region size | {{region_size}} | < {{threshold}} | {{emoji}} | {{comment}} |
| Pessimistic lock activities | {{pessimistic_lock}} | 低 | {{emoji}} | {{comment}} |
| Flow controller actions | {{flow_control}} | 0/1/10/s | {{emoji}} | P6 from checklist.md |
| Raft log lag | {{raft_log_lag}} | 低 | {{emoji}} | {{comment}} |
| TiKV Auto GC SafePoint lag | {{gc_lag}} | < 10min | {{emoji}} | {{comment}} |
| TiFlash Raft Wait Index Duration | {{tiflash_wait_index}} | < {{threshold}} | {{emoji}} | {{comment}} |
| TiFlash Write Stall Duration | {{tiflash_write_stall}} | 0/1%/5% | {{emoji}} | P5 from checklist.md |
| TiCDC Changefeed checkpoint lag | {{cdc_lag}} | < 10s | {{emoji}} | {{comment}} |
| gRPC Error Rate | {{grpc_error}} | 0.1/1/5% | {{emoji}} | A2 from checklist.md |
| Backoff Frequency | {{backoff_freq}} | 1/10/100/s | {{emoji}} | A3 from checklist.md |
| Instance Latency Outlier | {{latency_outlier}} | 1.5/2.0/3.0× | {{emoji}} | A1 from checklist.md |
| RCU Utilization / RC Queuing | {{rcu_util}} | tier-specific | {{emoji}} | A5 from checklist.md |
| Component Restart Count | {{restart_count}} | 0 | {{emoji}} | A10 from checklist.md |

### 4.5 资源使用

| 组件 | CPU | Memory | 状态 | 备注 |
|------|-----|--------|------|------|
| TiDB | {{tidb_cpu}} | {{tidb_mem}} | {{emoji}} | {{comment}} |
| TiKV | {{tikv_cpu}} | {{tikv_mem}} | {{emoji}} | {{comment}} |
| TiFlash | {{tiflash_cpu}} | {{tiflash_mem}} | {{emoji}} | {{comment}} |
| TiCDC | {{ticdc_cpu}} | {{ticdc_mem}} | {{emoji}} | {{comment}} |

磁盘 I/O：

| 监控指标 | 当前值 | 阈值 | 状态 | 说明 |
|----------|--------|------|------|------|
| Disk Read/Write Bytes | {{disk_io_bytes}} | {{threshold}} | {{emoji}} | {{comment}} |
| Disk Throughput | {{disk_throughput}} | {{threshold}} | {{emoji}} | {{comment}} |
| Disk Latency | {{disk_latency}} | {{threshold}} | {{emoji}} | {{comment}} |
| IOPS | {{iops}} | {{threshold}} | {{emoji}} | {{comment}} |

### 4.6 慢查询与优化机会

| 监控指标 | 当前值 | 阈值 | 状态 | 说明 |
|----------|--------|------|------|------|
| Top 5 Slow SQL (avg time) | {{slow_sql_avg}} | 1/10s | {{emoji}} | O1 from checklist.md |
| Top 5 Resource-Consuming SQL (avg keys) | {{heavy_sql_keys}} | 10K/100K/1M | {{emoji}} | O2 from checklist.md |
| Block Cache Hit Rate | {{block_cache_hit}} | 90/95/99% | {{emoji}} | O3 from checklist.md |

---

## 五、非默认参数记录

> From World Cup per-cluster inspection template. Record parameters that deviate from TiDB defaults.

| 组件 | 参数 | 当前值 | 默认值 | 说明 |
|------|------|--------|--------|------|
| TiKV | `coprocessor.region-max-size` | {{value}} | 384MiB | {{comment}} |
| TiKV | `rocksdb.defaultcf.titan.enabled` | {{value}} | false | {{comment}} |
| TiKV | `raftstore.apply-pool-size` | {{value}} | 2 | {{comment}} |
| TiKV | `readpool.unified.max-thread-count` | {{value}} | — | {{comment}} |
| TiDB | `performance.force-init-stats` | {{value}} | false | {{comment}} |
| PD | `replication.isolation-level` | {{value}} | — | {{comment}} |
| TiFlash | `profiles.default.task_scheduler_active_set_soft_limit` | {{value}} | — | {{comment}} |

---

## 六、Checklist 评分映射（45 项）

Map the 45 items from [checklist.md](checklist.md) into the report sections above:

| Checklist Item | Report Section | Document Source |
|----------------|----------------|-----------------|
| C1~C3 | 4.2 集群状态与流量 | checklist.md |
| R1~R6 | 4.5 资源使用 / 3. L2 根因定位 | checklist.md + SaaS Prod L2 |
| P1~P14 | 二、L1 业务健康 / 4.2 / 4.3 / 3. L2 | checklist.md + SaaS Prod L1/L2 |
| S1~S9 | 4.1 容量与增长 / 3. L2 根因定位 | checklist.md + health check.json |
| A1~A10 | 4.4 稳定性与可靠性 / 3. L2 根因定位 | checklist.md + SaaS Prod L2 |
| O1~O3 | 4.6 慢查询与优化机会 | checklist.md |

---

## 七、关注事项与跟进

| 风险等级 | 监控指标 | 当前状态 | 建议动作 | 责任人 | 预计完成 |
|----------|----------|----------|----------|--------|----------|
| 🔴 | {{metric_name}} | {{value}} | {{action}} | {{owner}} | {{eta}} |
| 🟠 | {{metric_name}} | {{value}} | {{action}} | {{owner}} | {{eta}} |
| 🟡 | {{metric_name}} | {{value}} | {{action}} | {{owner}} | {{eta}} |

---

## 八、巡检建议

{{#each recommendations}}
{{number}}. **{{title}}**（{{level}}）  
   - 现象：{{phenomenon}}  
   - 建议：{{suggestion}}  
   - 参考：{{reference}}
{{/each}}

---

## 九、附录

### 9.1 关键监控指标说明

| 监控指标 | 说明 | 数据来源 |
|----------|------|----------|
| P99 Query Latency | 99% 查询处理耗时 | `tidb_server_handle_query_duration_seconds_bucket` |
| QPS | 每秒查询数 | `tidb_executor_statement_total` |
| TPS | 每秒事务数 | `tidb_session_transaction_duration_seconds_count` |
| Store available ratio | TiKV 可用容量比例 | `pd_scheduler_store_status` |
| Pending Peer Count | 等待补副本的 Region peer 数量 | `pd_regions_status` |
| TiCDC Checkpoint Lag | 数据同步延迟 | `ticdc_owner_checkpoint_ts_lag` |

### 9.2 关键 PromQL 速查

Replace `{tidb_cluster_id="{cluster_id}"}` with the inspected cluster id before execution.

```promql
# Storage size
sum(pd_cluster_status{tidb_cluster_id="{cluster_id}", type="storage_size"})

# Region count
sum(pd_cluster_status{tidb_cluster_id="{cluster_id}", type="region_count"})

# Leader count
sum(pd_cluster_status{tidb_cluster_id="{cluster_id}", type="leader_count"})

# QPS
sum(rate(tidb_executor_statement_total{tidb_cluster_id="{cluster_id}"}[2m])) by (resource_group)

# P99 Query Latency
histogram_quantile(0.99,
  sum(rate(tidb_server_handle_query_duration_seconds_bucket{tidb_cluster_id="{cluster_id}"}[2m])) by (le)
)

# TiCDC Lag
max(ticdc_owner_checkpoint_ts_lag{tidb_cluster_id="{cluster_id}"}) by (namespace, changefeed)

# GC Safe Point Lag (seconds) — verify metric unit before use
(time() - max(tikv_gcworker_autogc_safe_point{tidb_cluster_id="{cluster_id}"}) / (2^18))

# Disk I/O throughput
sum(rate(node_disk_read_bytes_total{tidb_cluster_id="{cluster_id}"}[5m])) by (instance, device)
+
sum(rate(node_disk_written_bytes_total{tidb_cluster_id="{cluster_id}"}[5m])) by (instance, device)
```

### 9.3 参考文档

- [checklist.md](checklist.md) — 45 项巡检清单与评分口径
- [../performance-optimization/SKILL.md](../performance-optimization/SKILL.md) — 性能瓶颈定位路径
- [../fault-diagnosis/SKILL.md](../fault-diagnosis/SKILL.md) — 故障诊断路由
- [../../providers/tidb-cloud/o11y/o11y-data.md](../../providers/tidb-cloud/o11y/o11y-data.md) — 可观测性数据获取

---

*报告由 TiDB Instance Inspection skill 生成。数据截止时间：{{data_end_time}}。*
```

---

## Multi-Cluster Fleet Summary Template

When the user provides data for multiple clusters, produce a fleet summary first, then per-cluster details. This format mirrors the OP Asia-Pacific fleet report.

```markdown
# {{scope}} TiDB 集群服务巡检总结报告

> **巡检时间：** {{inspection_date}}  
> **巡检对象：** {{scope}}，共 {{cluster_count}} 套集群  
> **巡检团队：** {{team}}  
> **报告版本：** {{version}}

## 一、巡检范围

本期巡检覆盖核心组件 5 大类、监控指标 {{metric_count}} 项（覆盖 {{cluster_count}} 套集群）。

| 维度 | 监控指标 | 指标数 |
|------|----------|--------|
| 容量与增长 | 数据容量/日增长、Region 日增长、Leader/TiKV、Region/TiKV、Store available ratio、GC life time | 6 |
| 集群状态与流量 | 实例数、运行时长、异常重启、QPS、TPS、CPS、Affected Rows、Connections、Active Connections、Duration P99/P999、Failed Query OPM | 12 |
| 查询耗时拆解 | PD TSO Wait、Get Token、Parse、Compile、gRPC message、Storage async write | 6 |
| 稳定性与可靠性 | Raft term、WAL fsync、backend commit、Syncer index、Region health、Abnormal Stores、Stats Healthy、Pessimistic retry、Critical error、Server is Busy、Region size、Pessimistic lock、Flow control、GC safe point、Raft log lag、Raft Wait Index、Write Stall、Changefeed checkpoint lag | 18 |
| 资源使用 | TiDB/TiKV/TiFlash/TiCDC CPU & Memory、Disk Read/Write Bytes、Disk Throughput、Disk Latency、IOPS | 12 |
| **合计** | | **54** |

## 二、巡检结果总览

整体健康度：{{overall_status}}

| 等级 | 数量 | 占比 |
|------|------|------|
| ✅ 正常 | {{healthy_count}} | {{healthy_pct}}% |
| ⚠️ 注意 | {{notice_count}} | {{notice_pct}}% |
| ❌ 高风险 | {{critical_count}} | {{critical_pct}}% |
| **合计** | **{{total}}** | **100%** |

## 三、各集群明细

| 集群 | ✅ 正常 | ⚠️ 注意 | ❌ 高风险 | 关键风险项 |
|------|---------|---------|-----------|------------|
{{#each clusters}}
| {{name}} | {{healthy}} | {{notice}} | {{critical}} | {{top_risk}} |
{{/each}}

## 四、关注事项与跟进

| 风险等级 | 集群 | 监控指标 | 当前状态 | 建议动作 | 责任人 | 预计完成 |
|----------|------|----------|----------|----------|--------|----------|
| 🔴 | {{cluster}} | {{metric}} | {{value}} | {{action}} | {{owner}} | {{eta}} |
| 🟠 | {{cluster}} | {{metric}} | {{value}} | {{action}} | {{owner}} | {{eta}} |
| 🟡 | {{cluster}} | {{metric}} | {{value}} | {{action}} | {{owner}} | {{eta}} |

## 五、巡检建议

{{recommendations}}
```

### Fleet Aggregation Rules

- **Per-cluster overall status:** highest severity among all sub-items. A single 🔴 makes the whole cluster 🔴.
- **Fleet overall status:** highest severity among clusters.
- **Per-dimension fleet status:** count clusters in each color; report the dominant color + outliers.
- **Metric gaps:** mark as `N/A` at the cluster level; do not drop the cluster from the inventory.

---

## Generation Rules

### 1. Score Values

Use the same severity scale as `checklist.md`:

```
HEALTHY  → Within normal range
NOTICE   → Deviates from normal but does not affect service
WARNING  → Needs to be addressed soon
CRITICAL → Needs immediate attention
```

### 2. Category and Overall Scores

```
category_score = max(severity(item) for item in category)
overall_score  = max(severity(category) for category in all_categories)
severity: HEALTHY=0, NOTICE=1, WARNING=2, CRITICAL=3
```

### 3. Baseline-Relative Thresholds

When historical baseline is available, use document-derived multipliers:

```
P99 Query Latency:
  Y threshold = baseline × 2.0
  R threshold = baseline × 5.0

TSO RPC p99:
  Y threshold = baseline × 3.0
  R threshold = baseline × 5.0

TiKV gRPC p99:
  Y threshold = baseline × 2.0
  R threshold = baseline × 5.0

QPS Drop:
  Y threshold = baseline × 0.7
  R threshold = baseline × 0.5

QPS Surge:
  Y threshold = baseline × 1.5
  R threshold = baseline × 2.5

TiDB Connections:
  Y threshold = baseline × 1.5
  R threshold = baseline × 2.5

Disk Read/Write Bytes:
  Y threshold = baseline × 1.5
  R threshold = baseline × 2.5
```

When baseline is unavailable, fall back to the absolute thresholds defined in `checklist.md`.

### 4. Metric Unavailable Handling

```
metric unavailable (HTTP 500 / no data / query failed):
  Score column: "⚪ N/A"
  Remarks column: "Metric unavailable — reason: {reason}"
  Not counted in category score
  Record failure reason in appendix
```

### 5. Product Line Adaptation

```
Dedicated:
  - Full checklist — all 45 items
  - Skip RCU utilization (no RCU limit)

Starter / Essential / Premium:
  - Full checklist
  - Add RCU utilization check
  - Add Cop worker limit check

Starter / Essential:
  - Skip Loki-dependent items
  - Add RCU utilization check
  - Some metrics may 500 — mark N/A and continue
```

### 6. Recommended Action Generation

For every WARNING / CRITICAL metric:

1. Extract the corresponding recommendation from `checklist.md`.
2. Link to detailed investigation paths in `../fault-diagnosis/paths/` or `../performance-optimization/paths/`.
3. Include concrete next steps: what to query, what threshold to watch, who to escalate to.
4. Sort by severity: CRITICAL > WARNING > NOTICE.

### 7. one_line_summary Generation

```
All HEALTHY  → "All metrics normal"
Has NOTICE   → "{N} items need attention: {most important 1-2 items}"
Has WARNING  → "{N} items need action: {most important 1-2 items}"
Has CRITICAL → "{N} items critical: {most important 1-2 items}"
```

---

## Quality Checklist

Before returning the report, verify:

- [ ] Executive summary can be read in 30 seconds.
- [ ] L1 business health and L2 root-cause sections are clearly separated.
- [ ] Every metric has a color, a value, and a threshold or baseline.
- [ ] Baseline-relative thresholds show the baseline value and multiplier.
- [ ] All WARNING/CRITICAL items appear in the risk register.
- [ ] Recommendations are specific, not generic "继续观察".
- [ ] N/A metrics are explicitly marked and explained.
- [ ] PromQL references are syntactically valid.
- [ ] Report uses consistent emoji/color semantics throughout.
- [ ] Multi-cluster reports include both fleet summary and per-cluster detail.
