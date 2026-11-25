# Dedicated 采集巨石迁移计划（Phased）

> **范围与态度**：`dedicated_health_inspection.py`（~2800 行）是经真实集群验证的 Dedicated/BYOC 全栈采集器，
> 主链路 `health_inspection.collect_full_stack` 把它当**库**调用（`inspect_cluster()`），再经 `dedicated_binding`
> 映射成 checklist 结果。它**能用、已验证**。本计划的目标是把它逐块搬进与 shared 一致的 `collection_adapters`
> recipe 模型，统一两套采集引擎——**这是独立项目，有回归风险，禁止一次性大改重写**。按下述分阶段、可回滚地做。

## 验收口径：保留全部 23 项（2026-11-25 用户确认）

- **决策**：23 个 bound check **全部保留**为客户基础报告项，不删除、不裁剪。每项在本 Skill 产出**基础信号 + 粗粒度定级**，**深度判定（持续窗口/根因/相关性）交 o11y**（`handoff.json`）。
- 权威清单（类别/检查项/基础信号/指标族/阈值/等级/native 状态）见 [`dedicated-basic-report-checklist.md`](dedicated-basic-report-checklist.md)——它是本迁移的验收口径。
- 曾讨论把 TIKV-04/05 当纯深度删掉，**已否决**：巨石对两者本就产出基础 present/absent 信号，保留为 native 基础信号。

## 为什么迁移（动机，非功能需求）

- 现在有**两套采集引擎**：shared 走 `collection_adapters`（recipe 驱动、容错回退齐全），dedicated 走巨石。
- 巨石是黑盒：import 图里 lazy import、自带 CLI（已标 `[INTERNAL]`）、2800 行难维护。
- 统一后：一处加容错/一处加指标，两类 plan 都受益；调用栈透明。

## 迁移边界（巨石当前产出）

`dedicated_binding` 绑定 **23 个 check**（巨石内已有 ~122 行 PromQL，可直接搬进 recipe）：

```
AVL-01/02/08  CDC-01/02  INF-03/04/05  OVR-01/02  PD-03
TIDB-01/02    TIKV-01/02/03/04/05/06/07/08/09/10
```

每个 check 要落成：`config/collection.v1.json` 里一条 recipe（adapter + PromQL + unit + rule），
`config/rules.v1.json` 里对应阈值（若定级），复用现有通用 adapter（`promql_distribution` / `promql_ratio` /
按实例聚合）或新增一个专用 adapter。

## 护栏工具（阶段 1，✅ 已就绪 2026-11-24）

- `scripts/dedicated_golden.py`：`shape`（抽形状）/ `capture`（存基线）/ `compare`（比对，有硬差异 exit 1）。
  **形状 = 每个 check 的 {status, reason, evidence_keys}，不存原始值/实例名**（隐私安全）。
- `tests/fixtures/dedicated_golden.json`：23 个 check 的基线形状快照。
- `tests/test_dedicated_golden.py`：比对函数单测 + fixture 自洽 + 无原始值 + 覆盖 23 个 bound check。
- **迁移时怎么用**：端口某个 check 前，对**测试窗口**用现巨石跑一次存基线（`capture`）；端口后用同一窗口的新
  `normalized.json` 跟基线 `compare`——**硬差异（status / evidence_keys 变化、check 缺失）= 回归，必须修**；
  `reason` 变化是软提示（note:）。committed fixture 里的 status 是时间点快照，持久保证的是 **evidence-key 形状**；
  健康度会随周变化，所以 live 迁移比对要用**同窗口**的新巨石基线，而非直接对committed fixture 的 status。

## 分阶段（每阶段独立可发、可回滚）

1. ✅ **立护栏（已完成，零风险）**：见上"护栏工具"。没有护栏不许开动端口。
2. **按组搬**（每组一个 PR、跑护栏）：资源类先行（TIKV-01/02 CPU/内存、TIDB-01/02、INF-03/04/05、PD-03）——
   - ✅ **TIDB-01/02 已端口并护栏验证（2026-11-24，零硬差异）**：新增 `per_instance_vector` adapter + `instant_vector`
     （instant 向量 → `{instance: value}`，evidence 保持最小以对齐巨石形状）；recipe 用 `max_over_time(...[{window}:5m])`；
     `health_inspection.NATIVE_DEDICATED_ADAPTER_CHECKS` 控制"native 覆盖巨石"（可回滚）。
   - ✅ **TIKV-01/02 基础信号已 native 化并护栏验证（2026-11-24，零硬差异）**：TIKV-01 CPU% 用自包含
     `tikv_server_cpu_cores_quota`；TIKV-02 内存% 用自包含 `tikv_server_memory_quota_bytes`（`on(instance)` join，
     无需节点规格）。两者为 **observation-only**（HEALTHY present / N/A absent），evidence 键 `cpu_max_by_instance` /
     `memory_max_by_instance` 与巨石一致。**语义变更(有意)**：巨石的深度判定 `cpu_sustained_high` /
     `memory_error_windows`（持续窗口/内存错误）**不再在此产出,交 o11y**（见 handoff 契约）。
     当前 `NATIVE_DEDICATED_ADAPTER_CHECKS = (TIDB-01, TIDB-02, TIKV-01, TIKV-02)`。
     ✅ **已补客户侧粗阈值告警（2026-11-24）**：加 `tikv_cpu_pct`（warn 85，warn-only）、复用 `tikv_memory_pct`
     （warn 85 / crit 90），TIKV-01/02 recipe 由 observation 改为带阈值。**语义分层**：本 Skill 出**粗粒度触发**
     （peak≥阈值 → WARNING → 进 handoff → o11y 深挖）；**持续窗口/根因等深度判定仍交 o11y**。实测 peak 60.7%/75.6%
     低于 85% → 仍 HEALTHY、护栏零硬差异。巨石深度逻辑仍是退役目标。
   - ✅ **INF-03（磁盘使用%）、PD-03（region/leader 分布）已 native 化并护栏验证（2026-11-24，零硬差异）**：
     INF-03 自包含 `tikv_store_size_bytes used/available`（per_instance_vector，observation，深度 store 增长 → o11y）；
     PD-03 用新 `multi_instance_vector` adapter 合两个 per-instance 向量（region+leader，observation，均衡判定 → o11y）。
     当前 NATIVE 集合含 TIDB-01/02、TIKV-01/02、INF-03、PD-03（6 个）。scope 契约测试已改为只校验 shared 可选 recipe（dedicated 豁免）。
   - ✅ **INF-04/05（磁盘 IOPS/延迟）已 native 化并护栏验证（2026-11-24，零硬差异）**：把 `{tikv_disk_device_regex}`
     + `{namespace}` 作为 `CollectContext.substitutions` plumb 进 `expand`，`collect_full_stack` 从集群详情解析设备正则
     （存储类型未知时用 catch-all 默认，与巨石一致）。INF-05 用新 `multi_scalar_peak`（两条 node-disk→pod join 延迟查询
     → 两个标量峰值，观测）；INF-04 用专用 `disk_iops_check`（disk-join IOPS 峰值 + 自定义 AWS-EBS `query_exceeded_iops_by_pod`
     超限峰值 + `inf_iops_exceeded` 阈值，深度 throttle 根因 → o11y）。
     当前 NATIVE 集合 8 个：TIDB-01/02、TIKV-01/02、INF-03/04/05、PD-03。
   - ✅ **OVR-01/02（集群概览/状态）已 native 化并护栏验证（2026-11-24）**：新 `metadata_fields` adapter 从
     ctx.metadata 产出**扁平** evidence（OVR-01: provider/region/version/status；OVR-02: status），与巨石一致；
     metadata 由 report 透传进 native context（OVR 为 metadata 透传，非深度）。OVR-01/02 的 recipe 由
     `cluster_metadata`（嵌套）统一改为 `metadata_fields`（扁平），logical 路径不受影响（已跑测）。
     **当前 NATIVE 集合 10 个**：TIDB-01/02、TIKV-01/02、INF-03/04/05、PD-03、OVR-01/02。
     剩余巨石：AVL-01/02/08、CDC-01/02、TIKV-03..10（多含深度/窗口逻辑 → 基础信号 native + 深度交 o11y）。
   - ✅ **TIKV-03/05/06/07/10、AVL-01/02/08 已 native 化并护栏零硬差异（2026-11-25，同窗口 11-18→11-25）**：
     TIKV-04/09 先行（`multi_scalar_peak` 加 `warn_if_positive` 写阻塞 present/absent、`thresholds` 多阈值 OR 响应性）；
     TIKV-03 gRPC QPS 均值观测、TIKV-05 丢弃/限流峰值走 `per_instance_vector`/`multi_scalar_peak`；
     TIKV-06/07 的**空 evidence HEALTHY** 用新 `scalar_peak_probe`（present+粗阈值探针，仅破阈才带 evidence，深度 sustained/hotspot → o11y）；
     TIKV-10/AVL-02 同源重启走 `per_instance_vector` + 新规则 `restart_count`（≥1 告警）；AVL-01 读 metadata status 用新
     `product_status_check`；AVL-08 数实例用新 `instance_count_check`（窗口化 per-instance CPU 计数，避开单时点 instant 返回空）。
     **当前 NATIVE 集合 18 个**：OVR-01/02、TIDB-01/02、PD-03、INF-03/04/05、AVL-01/02/08、TIKV-01/02/03/04/05/06/07/09/10。
     **⏸ 暂缓 3 项**（仍由巨石采、仍是保留项，见 [`dedicated-basic-report-checklist.md`](dedicated-basic-report-checklist.md) §3 暂缓原因）：
     TIKV-08（日志扫描 + `alert_counter_total` 关联依赖）、CDC-01/02（changefeed 查询来源不清晰 + 验证集群无 changefeed、护栏无法验证活跃路径）。
     新增 env 开关 `NATIVE_DEDICATED_ADAPTER_CHECKS`（空=纯巨石）供护栏抓同窗口基线，不改代码。

   - ✅ **TIKV-08、CDC-01、CDC-02 已 native 化并护栏零硬差异（2026-11-25，同窗口 11-18→11-25，23/23 全覆盖）**：
     **TIKV-08**（唯一 WARNING 项，完整实测）——新 `tikv_error_signals` adapter：4 条 PromQL 计数总增量
     （`tikv_critical_error_total` / `channel_full_total` / `server_report_failure_msg_total` / `grpc_msg_fail_total`，`sum(increase(...[{window}]))`）
     + `error_log_count`。日志通道搬进 adapter 层：`ClinicAPI` 新增 `loki_query_range`（镜像巨石 `/data-proxy/loki/{cid}` 端点），
     新增 `_build_tikv_loki_filter`（复刻巨石 ERROR/FATAL include/exclude 过滤器）+ `_loki_error_log_count`（fail-closed→0，键恒在）。
     native 产出 WARNING + 同 5 键（含 error_log_count），与巨石基线一致。
     **CDC-01/02**——源其实清晰（在 `health_inspection._collect_reliability`，非巨石）：`ticdc_coordinator_changefeed_state` 按 `state` 聚合 →
     新 `changefeed_state_check`（Total/Working/Failed+Error/Stopped 判定，含 NOTICE）；`max(ticdc_dispatchermanager_checkpoint_ts_lag)` /
     `..._resolved_ts_lag` 取峰 → 新 `combined_scalar_threshold`（支持 CRITICAL），新规则 `cdc_checkpoint_lag_s`（300/3600s）。
     **局限（如实）**：验证集群无 changefeed，护栏只覆盖 N/A 路径；活跃路径忠实搬移 + 单测覆盖，但未真实集群实测
     （清单 §3 标 `✅*`）。**建议**：收口 binding 前，在带 changefeed 集群补一次 CDC 活跃路径护栏去星。
     **当前 NATIVE 集合 23 个（全覆盖）**。14 条新单测，全量 194 测试绿；`collection_adapters.py validate` → 44 recipes / 19 rules。

   - ✅ **NATIVE 集合改为数据驱动（2026-11-25）**：去掉 `health_inspection.NATIVE_DEDICATED_ADAPTER_CHECKS` 硬编码元组，
     改为在 `config/collection.v1.json` 的 recipe 顶层打 `"dedicated_native": true` 标志，`native_dedicated_checks()`
     从 recipe 读取。**加一个 native Dedicated check 不再改代码**——加 recipe + 置标志即可，与 checklist 的
     `applicability` 数据驱动选集同一思路。env 开关 `NATIVE_DEDICATED_ADAPTER_CHECKS`（空=纯巨石）仍可运行时覆盖供护栏用。
     派生集合与原 23 项完全一致,护栏零硬差异。

   - ✅ **Plan 能力集合改为数据驱动（2026-11-25）**：去掉 `dedicated_capabilities()` / `logical_capabilities()`
     硬编码函数,在 `config/checklist.v1.json` 顶层新增 `plan_capabilities` 映射(每个 Plan → 能力别名列表,
     即该 Plan 可探测的 component 家族 + 个别 per-check 授权如 `check.lst-03`)。新 `plan_capabilities(registry, plan)`
     从中读取(缺失=空=拒绝)。`collect_logical` / `collect_full_stack` / `run_gate` 三处调用改为按 plan 取。
     **调整某 Plan 的探测范围不再改代码**,与 checklist `applicability`、recipe `dedicated_native` 同为数据驱动。
     派生能力集与选集(Dedicated 86 项、Premium 45 项)与改前逐一致,护栏零硬差异。
     至此 collect_full_stack 的"硬编码"尾巴基本清空:选什么(applicability)、怎么采(recipe)、是否 native
     (dedicated_native)、能力(plan_capabilities)全在 config。剩余耦合仅巨石本体(verdict 底线/metadata),属第 4 步退役。

   这些就是 by-instance 聚合，和 shared 的 `promql_distribution` 最像；再 CDC-01/02（changefeed 状态/滞后）、
   AVL-01/02/08（可用性/重启/拓扑）、TIKV-03..10（吞吐/稳定性/错误信号/响应性等）。
   每搬一个：把巨石里的 PromQL 抄进 recipe、接通用或新 adapter、`dedicated_binding` 里该 check 改为"已原生采集、
   不再从巨石映射"，跑护栏确认无偏差。
3. **收口 binding**：当 23 个 check 全部原生采集，`dedicated_binding` 只剩 verdict 合并等薄逻辑；
   `collect_full_stack` 不再 lazy import 巨石。
   - ✅ **稳妥收口已落地（2026-11-25，用户选"稳妥收口"）**：`collect_full_stack` 的合并由
     "replace-only"（`[native.get(b.id,b) for b in bound]`）改为 **native-primary add-based**
     （`by_id={b.id:b}; by_id.update(native)`）——native 对 23 项**权威**且稳健（native 缺某项才回退巨石绑定，
     不只整轮失败才回退）。`dedicated_binding` 模块 docstring 标注降级为「SCOPE-01/04/08 源 + 错误回退 + verdict 透传」，
     23 项推导保留为**可回滚 fallback**（巨石仍为 verdict 底线/metadata 运行，绑定结果零额外成本）。
     **巨石 `inspect_cluster` 仍调用**（verdict 底线 + metadata），其移除属第 4 步退役，用户已暂缓。
     护栏：同窗口 golden compare 23 项**零硬差异**；native check_results 与收口前逐项一致（86 项，无增删）。
   - ✅ **收口中发现并已修复一处既有 gate bug（2026-11-25，与收口无关，SQL-02 实时飙升暴露）**：
     旧 `inspection_gate.derive_status` 读 `item.get("verdict") or item.get("status")`——`iter_reports` 对单集群
     payload 返回 `[payload]`，于是 gate 只认**顶层 `verdict` 字段**（巨石底线，如 "Warning"），**忽略 check_results
     里更高的严重度**。当任一非-verdict check（如租户 SQL-02 走 CRITICAL）把 `normalized["status"]` 抬到 verdict
     之上时，`gate_consistent = (gate.status==normalized.status)` 判 False，整条 `inspect` 退非零（实为"状态被低报"隐患）。
     **非本次收口引入**：收口不碰 SQL 路径，旧 replace 合并在同样 SQL-02=CRITICAL 下会同样失败；上一轮通过仅因彼时 SQL-02=HEALTHY。
     **修复**：新增 `_report_status(report)`——单个 report 的状态 = 其 verdict/status 信号与自身 `check_results`
     （**排除 N/A，与 `checklist_engine.evaluate` 一致**）的 worst-of；`derive_status` 对每个 report 用它再跨 report 取 max。
     无 `check_results` 的 payload 行为不变（多集群 reports/results 路径不受影响）。与 `collect_full_stack` 的
     `worse_status(verdict, engine)` 口径对齐。新增 2 条 gate 单测（check 超 verdict → 抬状态；N/A 不虚抬）。
4. **退役巨石**：确认无引用（含 `[INTERNAL]` CLI、`internal-report.md` 的生成，Loki 日志扫描现已在 `ClinicAPI.loki_query_range`）后删除或归档；
   `tikv_thread_pool_pressure` 等 stub 在此过程中落实（`collection_adapters.py` 已有 TODO）。

## 不变量（每阶段必须守住）

- **诚实 N/A**：搬迁不得把原本 N/A 的 check 变成假 Healthy / 0。
- **按 plan 容错**：沿用 shared 已验证的回退（le→vmrange、cluster_id 标签缺失、NGM、resource_manager）。
- **护栏绿**：golden fixture 对齐 + `python3 -m unittest discover -s tests` 全绿 + 真实 Dedicated 联调抽验。
- **可回滚**：每组一个变更；binding 里保留"回退到巨石"的开关直到该组护栏稳定。

## 验证
```bash
python3 -m unittest discover -s tests
python3 scripts/collection_adapters.py validate
# 真实 Dedicated 联调：10698648422638660035，对比 golden fixture
```
