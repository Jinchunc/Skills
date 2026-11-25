# Dedicated 基础报告清单（Basic-Report Checklist）

> **定位**：这是 Dedicated/BYOC **客户基础报告**的权威检查清单——确定"检查哪些 Category、哪些 Check、基础信号是什么、阈值是什么、定什么级"。它是 `dedicated_health_inspection.py`（巨石）**native 化 + 退役**的验收口径：清单里的 23 项**全部保留**，每项在本 Skill 产出**基础信号 + 粗粒度定级**，**深度判定（持续窗口 / 根因 / 相关性）交 o11y**。
>
> **决策（2026-11-25，用户确认）**：**保留全部 23 项**，不删除、不裁剪。原先讨论过把 TIKV-04（写阻塞）/ TIKV-05（流控）当纯深度删掉——**否决**：巨石对这两项本就产出基础 present/absent 信号（`write_stall_peak` / `scheduler_discard_ratio_peak`），保留为 native 基础信号即可，深度证据分析仍交 o11y。
>
> 真相源：适用性与类别 `config/checklist.v1.json`；采集配方 `config/collection.v1.json`；阈值/定级 `config/rules.v1.json`。行级渲染以 `customer_report.py` 为准。本清单是设计口径，与三份 config 对齐。

---

## 1. 两层信号与本 Skill 的边界

- **基础信号层（本 Skill 产出）**：每个 check 给**确定性状态**（HEALTHY / NOTICE / WARNING / CRITICAL / N·A）+ **客户安全证据**（峰值 / 计数 / 状态，按实例聚合，无原始实例名透出到客户报告）。定级用**粗粒度触发**：峰值≥阈值、计数>0、状态异常。
- **深度判定层（交 o11y）**：持续窗口分析、内存增长 vs 基线、根因相关性、均衡性判断等——**不在本 Skill 产出**。WARNING/CRITICAL 的 check 经 `handoff.json`（`scripts/handoff.py`）按组件路由给外部 o11y 深度分析 Skill。
- **诚实 N·A**：指标缺失/不可得一律 N·A + 原因，**绝不冒充 HEALTHY 或 0**。

---

## 2. 定级口径

| 等级 | 含义 | 触发（基础信号层） |
|---|---|---|
| CRITICAL | 严重、需立即处理 | 峰值 ≥ critical 阈值 |
| WARNING | 需关注，进 handoff → o11y 深挖 | 峰值 ≥ warning 阈值 / 计数 > 0 / 状态异常 |
| NOTICE | 轻度提示 | 介于 notice 与 warning 之间 |
| HEALTHY | 正常 | 低于阈值 / 无异常信号 |
| 观测（observation） | 无定级，仅展示事实 | 无 rule，只给证据 |
| N·A | 不可得/不适用 | 指标缺失 + 原因 |

---

## 3. 清单（23 项 · 全部保留）

**native 列**：✅ 已 native 化并护栏零硬差异 ｜ ✅\* native 化、仅 N/A 路径护栏验证（活跃路径已忠实搬移，待带 changefeed 集群验证）｜ ⏳ 待 native

| # | Cat | Check | 基础信号（客户报告展示） | 指标族 | 阈值 | 等级 | native |
|---|---|---|---|---|---|---|---|
| 1 | 01 概览 | OVR-01 | 名称/ID/Plan/Provider/Region/版本/状态 | cluster metadata | — | 观测 | ✅ |
| 2 | 01 概览 | OVR-02 | 集群状态（active/paused/…） | cluster metadata | — | 观测 | ✅ |
| 3 | 05 TiDB | TIDB-01 | CPU%（vs 核配额）峰值 | tidb cpu / quota | 85% | WARNING（warn-only） | ✅ |
| 4 | 05 TiDB | TIDB-02 | 内存 RSS 峰值 | tidb RSS | — | 观测 | ✅ |
| 5 | 06 TiKV | TIKV-01 | CPU%（vs 核配额）峰值 | tikv cpu / quota | 85% | WARNING（warn-only） | ✅ |
| 6 | 06 TiKV | TIKV-02 | 内存%（vs 配额）峰值 | tikv RSS / quota | 85 / 90% | WARNING / CRITICAL | ✅ |
| 7 | 06 TiKV | TIKV-03 | gRPC QPS 均值（观测；深度 fail/P99 → o11y） | tikv grpc | — | 观测 | ✅ |
| 8 | 06 TiKV | TIKV-04 | 写阻塞峰值（present/absent） | tikv write stall | 峰值 > 0 | WARNING | ✅ |
| 9 | 06 TiKV | TIKV-05 | 调度丢弃/限流峰值 | tikv scheduler throttle/discard | discard > 0 | WARNING | ✅ |
| 10 | 06 TiKV | TIKV-06 | 待 compaction 字节峰值 | tikv pending compaction | 512 GiB（peak） | WARNING | ✅ |
| 11 | 06 TiKV | TIKV-07 | 统一读池存在性探针（深度热点 → o11y） | tikv unified read pool | — | 观测 | ✅ |
| 12 | 06 TiKV | TIKV-08 | 错误计数 + 错误日志数 | tikv error counters + loki | 计数 > 0 | WARNING | ✅ |
| 13 | 06 TiKV | TIKV-09 | slow score + raft log lag P99 峰值 | tikv slow score / raft lag | 100 / 5000 | WARNING | ✅ |
| 14 | 06 TiKV | TIKV-10 | 重启次数（+版本一致性） | tikv restarts | 计数 ≥ 1 | 观测 / WARNING | ✅ |
| 15 | 07 PD | PD-03 | region / leader 分布 | pd region+leader by instance | — | 观测 | ✅ |
| 16 | 08 基础设施 | INF-03 | 磁盘使用% 峰值 | tikv store used/available | 80% | WARNING | ✅ |
| 17 | 08 基础设施 | INF-04 | 磁盘 IOPS 峰值 + 超限峰值 | disk iops + exceeded | exceeded 1.0 | WARNING | ✅ |
| 18 | 08 基础设施 | INF-05 | 磁盘读/写延迟峰值 | node-disk→pod latency | — | 观测 | ✅ |
| 19 | 09 可用性 | AVL-01 | 产品可用性/状态 | product status | 异常 | WARNING | ✅ |
| 20 | 09 可用性 | AVL-02 | 组件重启次数 | restarts by instance | 计数 ≥ 1 | 观测 / WARNING | ✅ |
| 21 | 09 可用性 | AVL-08 | 拓扑冗余（TiKV 实例数） | tikv instance count | < 3 | WARNING | ✅ |
| 22 | 11 TiCDC | CDC-01 | changefeed 状态 | ticdc changefeed state | failed→WARN / 非全 W→NOTICE | WARNING / NOTICE | ✅\* |
| 23 | 11 TiCDC | CDC-02 | checkpoint/resolved 滞后峰值 | ticdc checkpoint lag | 300 / 3600s | WARNING / CRITICAL | ✅\* |

**统计（2026-11-25）**：✅ **23 项全部 native 化**，同窗口（11-18→11-25）护栏对巨石基线**零硬差异（含零软提示）**。其中 **21 项路径已被护栏实测验证**；**CDC-01/02 仅 N/A 路径被验证**（本集群无 changefeed，活跃路径已忠实搬移但未实测，见下）。

**本轮补搬（2026-11-25，TIKV-08 / CDC-01 / CDC-02）**：
- **TIKV-08**（错误信号）：**完整 native 化并实测验证**。新增 `tikv_error_signals` adapter：4 条 PromQL 计数总增量（`tikv_critical_error_total` / `tikv_channel_full_total` / `tikv_server_report_failure_msg_total` / `tikv_grpc_msg_fail_total`）+ `error_log_count`（新增 `ClinicAPI.loki_query_range` + 复刻巨石 TiKV ERROR/FATAL 日志过滤器，fail-closed→0）。本集群基线即 WARNING/5 键，native 产出**完全一致**（WARNING + 同 5 键）。日志采集通道已落在 adapter 层，不再依赖巨石。
- **CDC-01/02**（changefeed 状态/滞后）：源其实清晰（`ticdc_coordinator_changefeed_state` 按 `state` 聚合；`ticdc_dispatchermanager_checkpoint_ts_lag` / `..._resolved_ts_lag` 取峰值），已搬成 `changefeed_state_check` / `combined_scalar_threshold` adapter，CDC-02 新增 `cdc_checkpoint_lag_s` 规则（300/3600s）。**局限（如实标注）**：验证集群 `10698648422638660035` 无 changefeed，护栏只覆盖 N/A 路径；**活跃路径（WARNING/NOTICE/CRITICAL 判定）照绑定逻辑忠实搬移、单测覆盖，但未在真实带 changefeed 集群实测**——这是 `✅*` 的含义。待有带 changefeed 的 Dedicated 集群再补一次护栏即可去星。

---

## 4. 阈值来源与语义分层

| Check | rule（`rules.v1.json`） | 基础信号用（native 粗触发） | 深度（交 o11y） |
|---|---|---|---|
| TIDB-01 | `tidb_cpu_pct` warn 85 | 峰值 ≥ 85% → WARN | 持续窗口高 |
| TIKV-01 | `tikv_cpu_pct` warn 85 | 峰值 ≥ 85% → WARN | `tikv_cpu_sustained`（持续） |
| TIKV-02 | `tikv_memory_pct` 85/90 | 峰值 → WARN/CRIT | `tikv_memory_leak`（vs 7d 基线） |
| TIKV-03 | `tikv_grpc`（fail_30m 100 / P99 1s） | 失败数/P99 峰值触发 | 失败趋势相关性 |
| TIKV-04 | —（巨石内联 present/absent） | write_stall_peak > 0 → WARN | 阻塞窗口/成因 |
| TIKV-05 | —（巨石内联） | discard_ratio_peak > 0 → WARN | 限流根因 |
| TIKV-06 | `tikv_compaction_pending`（200/256/512 GiB） | peak ≥ 512 GiB → WARN | 持续积压判定 |
| TIKV-07 | `tikv_unified_read_pool`（hot 80 / peer 50） | 热点峰值触发 | rolling 热点确认 |
| TIKV-08 | —（4 条 PromQL 计数 + Loki 日志数，native） | 任一信号 > 0 → WARN | 错误分类/根因 |
| TIKV-09 | `tikv_slow_score` 100 / `tikv_raft_log_lag_p99` 5000 | 峰值触发 | 响应性根因 |
| TIKV-10 | —（巨石内联计数） | restart 计数 > 0；版本不一致 | — |
| AVL-01 | —（状态） | 产品状态异常 → WARN | — |
| AVL-02 | —（巨石内联计数，与 TIKV-10 同源） | restart 计数 > 0 | — |
| AVL-08 | —（内联 ≥3 冗余） | 实例数 < 3 → WARN | — |
| CDC-01 | —（状态） | failed→WARN / 非全 Working→NOTICE | 卡顿根因 |
| CDC-02 | `cdc_checkpoint_lag_s` 300/3600 | 滞后峰值 → WARN/CRIT | 滞后成因 |
| INF-03 | `store_capacity_usage_pct` warn 80（+rise 10） | 使用% 峰值 → WARN | 增长趋势 |
| INF-04 | `inf_iops_exceeded` warn 1.0 | 超限峰值 → WARN | throttle 根因 |

> **语义变更（有意、已在迁移计划记录）**：巨石对 TIKV-01/02/03/06/07 等做**持续窗口/基线相关**的深度判定；native 基础信号改为**峰值/计数的粗触发**，深度判定下沉到 o11y。因此同窗口下健康度可能从"深度 WARNING"降为"基础 HEALTHY"（峰值未破阈）——这是**设计取舍**，不是回归。护栏比对以**巨石同窗口基线**为准（见 `dedicated-migration-plan.md`）。

---

## 5. 深度交接（handoff → o11y）

- 每次巡检后 `scripts/handoff.py::build_handoff(normalized)` 产出 `handoff.json`：把 WARNING/CRITICAL 的 check 按组件（TIDB/TIKV/PD/TiCDC/…）路由，字段 `{check_id, component, severity, signal}`，不含原始值/实例名。
- 外部 o11y 深度分析 Skill 消费 `handoff.json`，对指定组件做窗口/根因深挖。**本 Skill 不做深度分析**，只做"采集 + 基础报告 + 交接"。
- 即使某基础信号判 HEALTHY（峰值未破粗阈），o11y 仍可按组件做深度巡检——两层互补。

---

## 6. 退役路线（23 项已全部 native → 收口 → 删巨石）

1. ✅ **23 项全部 native 化**（2026-11-25）：PromQL/Loki 已抄进 `collection.v1.json` recipe + 通用/专用 adapter，`health_inspection.NATIVE_DEDICATED_ADAPTER_CHECKS` 全覆盖（可回滚），同窗口护栏零硬差异。**唯一未实测**：CDC-01/02 活跃路径（本集群无 changefeed）——见 §3。
2. ✅ **稳妥收口已落地**（2026-11-25）：`collect_full_stack` 合并改为 **native-primary add-based**（native 对 23 项权威、稳健回退），`dedicated_binding` 降级为「SCOPE + 错误回退 + verdict 透传」（23 项推导留作可回滚 fallback）。巨石 `inspect_cluster` 仍为 verdict 底线/metadata 运行（其移除属第 3 步）。golden compare 23 项**零硬差异**。详见 [`dedicated-migration-plan.md`](dedicated-migration-plan.md) §分阶段-3。
   - ✅ **收口中暴露并已修复一处既有 gate bug（与收口无关）**：旧 `inspection_gate.derive_status` 只读顶层 `verdict`、忽略 check_results 更高严重度；当租户 SQL 等非-verdict check 超过巨石 verdict 时 `gate_consistent` 判 False、`inspect` 退非零（状态被低报隐患）。新增 `_report_status`（verdict + check_results worst-of，排除 N/A，与 engine 口径一致），+2 gate 单测。详见 [`dedicated-migration-plan.md`](dedicated-migration-plan.md) §分阶段-3。
3. **退役巨石**：确认无引用（含 `[INTERNAL]` CLI、`internal-report.md` 生成、Loki 日志扫描现已在 `ClinicAPI.loki_query_range`）后删除 `dedicated_health_inspection.py` + 绑定；metadata 改从 Clinic 详情 API 直取。**门槛**：先补 CDC-01/02 活跃路径护栏去星,再退役。

**不变量**（每步守住）：诚实 N·A、按 plan 容错沿用 shared 回退、护栏绿 + 全量单测绿 + 真实 Dedicated 抽验、每组可回滚。

验证：
```bash
python3 -m unittest discover -s tests
python3 scripts/collection_adapters.py validate
# 真实 Dedicated 联调：10698648422638660035，对比 golden 同窗口基线
```
