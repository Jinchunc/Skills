# Live Validation Checklist（联调清单）

这些采集是离线写的,需在真实集群联调。用一个真实 **Premium**（shared）集群和一个 **Dedicated** 集群各跑一次,按下表核对。

## 联调进度（Premium 集群 10260745576873721799，已验）

| 项 | 状态 | 结论 |
|---|---|---|
| A1 SQL Top-N | ✅ 已验+已修 | RU Top-N 直接可用（`sql_digest`+`total_ru`）；SQLD-02/03 原方式错（`order_by=count` 无效、未按 digest 汇总）→ 改为客户端按 digest 聚合,三表均正确；零敏感泄漏。 |
| A2 慢日志总条数 | ✅ 已找到+已实现+已验 | 源 = `clinic_data_proxy.slow_query_logs` 按 `date IN(...)` count；渲染为总量+趋势（实测 4,343 条）。 |
| A3 活动会话 | ⚠️ 未找到干净指标 | 只有 `tidb_server_active_users`(=0)、`internal_sessions`(=1)、`connections`；无"并发执行会话"量表。生产 avg 7.64 来源存疑,**需产品确认**。保持 N/A。 |
| A4 逻辑存储 | ⚠️ 找到指标待确认 | `tikv_keyspace_size_bytes{keyspace_name=<cid>,type="used"}` 按租户归属；但 `sum` 跨 6 实例=1.6TB≠生产 1208 GiB（副本因子/聚合方式待定），且属 `tikv_*`（scope 灰区）。**需你确认聚合+是否可用于 shared 报告**。保持 N/A。 |
| A5 统计健康度 | ✅ 已找到+已接 | **Prometheus 指标 `tidb_statistics_stats_healthy{type="[0,50)"}`**(Grafana "Stats Health Distribution" 面板源),值=该区间表数;跨 TiDB 实例取 `max()`(保守)。报告显示"N 张表"(实测 1 张)。(Data Proxy SQL 走不通,但指标走得通。) |
| A6 上周期对比 | ✅ 已验 | 趋势列真实数值（如 QPS Max -27.84%）。 |
| A7 RCU Min/Max（成本） | ✅ 已找到+已接 | 源 = Clinic 集群详情 API `/clinic/api/v1/orgs/{clinic_org}/clusters/{cid}` 的 `minRCU`/`maxRCU`(= 门户显示值,实测 10000/100000)。clinic_org 由 `search_cluster_by_id().orgID` 解析。`clinic_api.legacy_get_cluster(org,cid)` 即此端点。(先前试的 `service_limit` 是池级共享值,已弃用。) |
| A4′ 存储（dataSize 尝试） | ❌ 不可用 | 同详情 API 有 `dataSizeUsed=0`/`dataSizeLimit=50GiB`,但与真实逻辑容量(百 GiB 级)矛盾,未采用。存储仍走 **A4**（`tikv_keyspace_size_bytes` 聚合待定）。 |
| A8 指标严谨声明 | ✅ 已改 | 用量→`RU 用量（Max/Avg，RU/s）`;P99/P999→`查询延迟 P99/P999`(源 = `tidb_server_handle_query_duration_seconds`)。 |

## Dedicated 联调（集群 10698648422638660035，已验）

| 项 | 状态 | 结论 |
|---|---|---|
| B1 TiDB CPU/内存 | ✅ 已验 | TIDB-01 CPU（`tidb_server_maxprocs` 配额假设**正确**,拿到数据）、TIDB-02 RSS 观测,报告 §3.2 填充。 |
| B2 扩展 TiKV/INF 绑定 | ✅ 已验 | TIKV-04/05/08/09、INF-04/05 全部拿到真实数据,§3.1/§3.4 充实（TiKV 10 项全列）。 |
| B3 整体状态/隐私 | ✅ 已验 | overall=max(verdict,engine);零敏感泄漏（仅集群名出现,合法）。 |

## 额外修复（真实 deployType 暴露）

- `normalize_plan`：`essential v2` 曾被判 Unknown（default-deny）导致 Essential 集群巡检失败 → 已修（容忍 `<plan> vN` 版本后缀）；`premium resource pool` 仍为 Unknown（资源池记录,非客户集群,正确排除）。已用真实 Essential v2 集群端到端验证通过。

## ⏳ 待补口径（下次从这里继续，仅剩这 2 项，其余全部定稿并联调通过）

两行当前渲染 N/A，等你一句口径就能接上 + 联调：

1. **活动会话（SQL-08）** —— 需你给**指标名/表达式**。已探测且排除的候选：`tidb_server_connections`(=连接数，已用于 SQL-07)、`tidb_server_active_users`(实测 0)、`tidb_server_internal_sessions`(=1)；都对不上生产的 Max/Avg = 1000/7.64。接法：确认后 → checklist 置 SQL-08 `implemented` + collection.v1.json 加 `promql_distribution` recipe（rule:none，产 Max/Avg）。

2. **存储容量（LST-01，shared）** —— 需你给**聚合口径 + scope 许可**。候选指标 `tikv_keyspace_size_bytes{keyspace_name=<cid>, type="used"}`：`sum()` 跨 6 个 TiKV 实例 ≈ 1.6 TB（含副本），生产显示 1208 GiB → 疑似 `÷ 副本数` 或取 leader，待你定；且它是 `tikv_*` 指标（scope 灰区），需你确认能否用于 shared 客户报告。（注：Clinic 详情的 `dataSizeUsed/Limit`=0/50GiB 与真实容量矛盾，已弃用。）接法：确认后 → 在 collect_logical 取该指标按你口径聚合，写入 metadata.storage_* ，渲染器 `_storage_row` 已就位。

> 其余全部完成：A1 Top-N、A2 慢日志、A5 统计健康度、A6 上周期对比、A7 RCU Min/Max、指标严谨声明、Dedicated 全栈绑定（含 TiDB CPU/内存）、normalize_plan（essential v2）修复 —— 均已联调验证。

---

## 原始清单（逐项核对细则）

每项给出：看哪个产物、期望、不对时改哪一行。

> 需要 `CLINIC_API_KEY`（dedicated/TiDB 查询还可能需要对应环境）。全部为**读取**操作。

## 怎么跑

```bash
python3 scripts/health_inspection.py inspect --cluster-id "<PREMIUM_ID>" --relative-window 7d --lang zh --output-dir ./output/premium
```

```bash
python3 scripts/health_inspection.py inspect --cluster-id "<DEDICATED_ID>" --relative-window 2h --output-dir ./output/dedicated
```

看两处产物：
- `output/<x>/normalized.json` → 每个 check 的 `status` + `reason` + `evidence`（判对错看这里）。
- `output/<x>/health-report.md` → 渲染出的客户报告（看表是否填实、趋势/Top-N 是否正常）。

---

## A. Shared-plan（Premium）

### A1. SQL Top-N 响应形状（SQLD-01/02/03）— 最高优先
采集器方法已存在,但**响应字段名是我防御式猜的**,要确认。

- **看**：`normalized.json` 里 `SQLD-01/02/03` 的 `status` 与 `evidence.rows`；`health-report.md` 的 `SQL 分析` 三个 Top-N 表。
- **期望**：status=HEALTHY、`rows` 非空；报告表里每行有 digest + 数值。
- **若 rows 为空但集群确有 SQL**（reason=`sql_topn_empty`）或 digest/数值显示 `N/A`：响应字段名不符。
  - 行提取键：[collection_adapters.py `_topn_rows`](../scripts/collection_adapters.py) —— 现试 `items/data/list/result/rows/top`。
  - digest 键：[customer_report.py `_safe_digest`](../scripts/customer_report.py) —— 现试 `digest/sql_digest/plan_digest`。
  - 数值键：[customer_report.py `_safe_metric`](../scripts/customer_report.py) —— ru: `ru/total_ru/ru_sum/sum_ru`；slow_count: `count/exec_count/cnt/frequency`；slow_duration: `query_time/sum_latency/total_time/sum_query_time`。
  - **修**：把真实返回里的字段名补进上述键列表即可（各一行）。
- 另确认 `top_ru_summary` / `ngm_slow_query_list` 的 `start/end` 入参单位（现传 **epoch 秒**，见 [collection_adapters.py `sql_topn`](../scripts/collection_adapters.py)）。若接口要毫秒/ISO,改该处。

### A2. 慢日志总条数（SQL-06）— 源未绑定
- **现状**：`normalized.json` 中 `SQL-06` = N/A（planned，无 recipe）；报告「慢日志总条数」行 N/A。
- **要做**：确认慢日志总数的来源（PromQL 计数指标，或基于 `ngm_slow_query_list` 的计数）。
- **修**：确认后，在 [checklist.v1.json](../config/checklist.v1.json) 把 SQL-06 置 `implemented`，并在 [collection.v1.json](../config/collection.v1.json) 加 `{"adapter":"slow_log_count","params":{"query":"<窗口总量 increase() 查询>"}}`（adapter 已就绪）。

### A3. 活动会话（SQL-08）— 源未绑定
- **现状**：N/A。**要做**：确认活动会话指标名（非连接数 `tidb_server_connections`）。
- **修**：确认后 SQL-08 置 implemented + 加 recipe `{"adapter":"promql_distribution","params":{"query":"<会话指标>","kind":"range","unit":"sessions","rule":"none"}}`。

### A4. 逻辑存储 起点/终点/增长（LST-01/02/03）— 源未绑定
- **现状**：N/A；报告「存储容量」行 N/A。**注意**：**不能**用物理 `tikv_store_size`（共享计划排除项）。
- **要做**：找产品暴露的**逻辑**存储指标/API。
- **修**：给 [collection.v1.json](../config/collection.v1.json) 的 `LST-01` recipe 加 `params.query`（adapter `logical_storage_usage` 已算 start/end/growth）；LST-02/03 置 implemented。

### A5. 统计信息健康度 [0,50)（基础信息行）— 源未绑定
- **现状**：报告基础信息里恒显示 `N/A（源待接入）`。
- **要做**：确认 shared 计划能否拿到 stats healthy（SQL 查 `mysql.stats_healthy` / 产品 API）。
- **修**：确认后再决定新增一个 check（如 SQL-09）还是在渲染器直接接数据。

### A6. 上周期对比
- **看**：报告「集群用量」表`趋势`列应为 `Max ±x%；Avg ±y%`；`normalized.json` 应含 `previous_period`。
- **期望**：上周期有数据时趋势为数值,无数据时 `N/A`（不应报错或造零）。

---

## B. Dedicated

### B1. TiDB CPU / 内存（TIDB-01/02）— 新 live PromQL,本轮新增
- **看**：`normalized.json` 的 `TIDB-01/02`；报告 `### 3.2 TiDB 资源` 表。
- **期望**：TIDB-01 有 CPU% 状态（≥85→WARNING）、TIDB-02 有 RSS（HEALTHY 观测）；非 `tidb_cpu_metric_unavailable`。
- **关键确认**：核数配额指标是否为 **`tidb_server_maxprocs`**（见 [dedicated_health_inspection.py](../scripts/dedicated_health_inspection.py) `tidb_cpu_usage_q`）。若该指标不存在 → TIDB-01 恒 N/A,换成正确的 TiDB 核数配额指标。
- TiDB 内存 **仅观测**（无限额源无法算 %）；若后续有限额指标,可升级为阈值判定。

### B2. 扩展的 TiKV/基础设施绑定（TIKV-04/05/08/09、INF-04/05）
这些**复用采集器已有 metrics**,风险较低,但确认这些键在真实运行里有值（非 None）：
- **看**：`normalized.json` 对应 check 的 status/reason；报告 `### 3.1`/`### 3.4` 表是否有这些行。
- **期望**：有数据时给出 HEALTHY/WARNING,而非 `*_metric_unavailable`。
- 涉及的采集器 metrics 键：`write_stall_peak`、`scheduler_discard_ratio_peak`、`critical_error_total`/`channel_full_total`/`server_report_failure_total`/`error_log_count`、`slow_score_peak`、`raft_log_lag_p99_peak`、`iops_exceed_peak`/`disk_iops_peak`、`disk_read_latency_peak`/`disk_write_latency_peak`。
- 阈值取自 [rules.v1.json](../config/rules.v1.json)（slow_score=100、raft_lag=5000、iops_exceeded=1.0）——核对与生产告警线一致。

### B3. 整体状态不被掩盖
- **期望**：`normalized.json` 的 `overall_status_source` = `max(collector_verdict, engine)`；绑定只能**升**严重度。确认 overall status 合理。

---

## 校对基准
把两份 `health-report.md` 与生产 Feishu 报告逐节比对：
- Premium：`pingcap.feishu.cn/wiki/ZRxXw9iM6ixM1uklpEtcUdPsn9e`
- Dedicated Draft：`pingcap.feishu.cn/wiki/O3l6w6CjmiiEtAkR4XfcW6rcn9e`

每核对完一项,就把对应的"源未绑定/待确认"落实为一行 recipe 或查询修正。全部离线测试与门禁在改后仍须通过：
```bash
python3 -m unittest discover -s tests && python3 scripts/collection_adapters.py validate && python3 scripts/checklist_registry.py verify-markdown
```
