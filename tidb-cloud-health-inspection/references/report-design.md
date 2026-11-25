# 巡检报告统一设计（Unified Inspection Report Design）

> 本轮目标：**统一 5 个 Plan（Starter/Essential/Premium/Dedicated/BYOC）的报告结构**，并把"巡检什么、展示什么、怎么展示"从第一性原理理清。本轮**只定结构与原则，不改采集集合**；采集内容的增减另起一轮。
>
> 背景：公司当前没有统一巡检模板。本设计参考业界成熟实践（见 §9），结合 TiDB Cloud 存算分离架构与租户安全边界制定。
>
> **本文件是唯一的报告设计文档**（已合并原 `report-template*.md`；行级真相以 `customer_report.py` 为准）。
> **实现现状（2026-11-24）**：骨架为**动态编号**——shared（Starter/Essential/Premium）运行于共享资源池，省略 §可靠性 与 §配置与功能特性，共 **7 节**；Dedicated/BYOC 保留两节，共 **9 节**。两类均含 **§关键指标趋势**（current-vs-previous PNG 对比图，`report_charts.py`）。叙述层经 `payload["interpretations"]` 注入（见 SKILL.md）。交付走全局 `lark-cli`（用户身份、markdown 直接 embed 图）。

---

## 1. 为什么巡检 · 能发现什么 · 客户想看什么 · 能帮到什么

**为什么巡检（主动优于被动）**：定期巡检的成本远低于事故救火。它在问题变成故障*之前*把风险暴露出来，支撑容量规划、降低 TCO。〔Pythian、Datavail〕

**能发现什么**（巡检要覆盖的风险类别）：
- **容量 / 成本**：存储、算力（RCU）余量将尽却无预测；右规格（right-sizing）机会。
- **配置错误**：内存、索引、I/O 能力默认值落后于硬件、维护任务陈旧。
- **性能回归**：瓶颈、慢/贵 SQL、热点；以及引擎之外的瓶颈（存储/网络延迟）。
- **可靠性 / HA-DR 缺口**：备份缺失或未验证、故障转移未就绪、复制滞后。
- **安全**：越权、弱策略、缺补丁。

**客户想看什么**（决定报告形态）：
- 带**严重度**的**优先级发现**，不是原始指标堆砌。
- 具体可执行的**建议 / 下一步**。
- **趋势 / 与上周期对比**（在变好还是变坏）。
- **容量 / 成本余量**直说（"按当前增速，存储还能用约 X 个月"）。

**能帮到客户什么**：把"一堆指标"翻译成"你现在有哪些风险、按什么顺序处理、处理后下期复检验证"。

---

## 2. 设计原则

1. **两层信号模型**：`工作负载健康（RED/黄金信号）` 叠在 `资源健康（USE）` 之上；每个工作负载症状都能向下钻到某个资源原因。
2. **一条发现 = 严重度 + 量化影响 + 建议 + 状态**（借鉴 AWS Well-Architected 风险分级 + Oracle ADDM 按影响排序）；**绝不只给一个裸指标**。
3. **综合优于堆砌、趋势优于快照**：执行摘要在前，原始数据进附录，始终带上周期对比；闭环——下期复检衡量变化。
4. **一套骨架，5 Plan 同构**：结构恒定，差异只体现在"深浅"（某节能填多少），不体现在"长相"。某 Plan 不适用的节**保留并标注**（如"共享计划按设计不采物理层"），而非删除——让客户理解"为什么没有这部分"。
5. **隐私按构造**：客户报告只由"状态 + 白名单标签"渲染；原始证据、实例名、PromQL、URL 永不进客户报告（校验器兜底）。客户自有 SQL 以 **digest** 呈现。

---

## 3. 巡检什么：两层信号模型（映射到 TiDB）

### 3.1 工作负载健康 = RED / 黄金信号（面向"请求/SQL"）
- **Rate 速率**：QPS、事务数。
- **Errors 错误**：失败 SQL 率、回滚/中止事务。
- **Duration 时延**：查询延迟 P99/P999（**成功与失败分开**；用百分位不用平均）。
- （黄金信号的 Latency/Traffic/Errors 即此层的 SLO 视图，Saturation 下沉到资源层。）

### 3.2 资源健康 = USE（面向"每个资源"：TiDB/TiKV/PD/磁盘/网络/连接池）
每个资源三看：
- **Utilization 利用率**：CPU/内存/存储/IO 忙的比例。
- **Saturation 饱和**：排队/积压（IO 队列、goroutine/线程排队、Raft apply 队列、锁/latch 等待、连接池占满）。*饱和是非线性的——最后几 % 会不成比例地劣化性能。*
- **Errors 错误**：磁盘错误、OOM、失败的 Raft 消息。

### 3.3 关键点
工作负载层出症状（P99 上升）→ 到资源层定位病因（哪个资源饱和）。这正好契合 TiDB 存算分离：SQL 层（TiDB/RED）↔ 存储层（TiKV/USE）。

### 3.4 按 Plan 的可见边界
| 层 | Starter/Essential/Premium（共享，租户安全） | Dedicated/BYOC（全栈） |
|---|---|---|
| 工作负载（RED） | ✅ 租户 SQL 指标全可见 | ✅ |
| 资源（USE） | ⚠️ 仅产品暴露的逻辑量（RCU 余量、逻辑存储、连接/会话）；**物理 TiKV/PD/节点/磁盘按设计不采** | ✅ 全组件 |
| 可靠性/HA/备份 | ⚠️ 平台托管，多由产品保障 | ✅ 可见 |

---

## 4. 统一报告骨架（5 Plan 同构）

> 采用业界共识骨架（AWS WAF / Oracle ADDM / Percona / SQL Server 的共同脊柱），对齐两层信号模型。编号与节名两类报告**完全一致**；每节按 Plan 能力填，填不了的标 N/A 并注明原因。

```
报告头：<标题> — <集群>  ·  总体健康状态 / 置信度

1. 执行摘要 (Executive Summary)
     总体评级 + 按严重度统计的发现数 + Top 3–5 风险 + 头条建议（1 屏内，非技术读者可读）
2. 基础信息与基线 (Environment & Baseline)
     集群/版本/拓扑、RCU Min/Max、巡检窗口、负载概况（Top-N 概览）
3. 工作负载健康 (Workload Health — RED/黄金信号)
     QPS、查询延迟 P99/P999（成功/失败分开）、失败 SQL 率、慢日志
     —— 统一「本周期 / 上周期 / 趋势」对比表（★dedicated 也采用，不再只快照）
4. 资源健康 (Resource Health — USE)
     4.1 TiDB  4.2 TiKV  4.3 PD  4.4 基础设施（磁盘/网络）
     每项：利用率 / 饱和 / 错误 + 余量趋势
     · shared：逻辑量 + 大量 N/A（注明不采物理层）
     · dedicated：完整状态表
5. SQL 分析 (SQL Analysis)
     RU Top-N / 慢次数 Top-N / 慢总耗时 Top-N（客户自有，按 digest）
     · dedicated 另加执行计划/索引健康（绑定后）
6. 可靠性 / HA / 备份 / 容灾 (Reliability / HA / Backup / DR)
     复制/Raft 健康、故障转移就绪、备份存在性·时效·可恢复性验证、DR 缺口
     · shared：多 N/A（平台托管）；dedicated：可见
7. 配置与安全 (Configuration & Security)
     配置基线 vs 最佳实践、与工作负载不匹配的设置、权限/访问审查
     · shared：多 N/A；dedicated：完整（采集待补）
8. 发现与优先建议 (Findings & Prioritized Recommendations)
     每条发现 = 严重度 + 区域 + 量化影响 + 证据 + 建议 + 状态；按 严重度×工作量 排序
     （★LLM 解读层：narrative/根因/下一步填这里，来自 analysis_candidates）
9. 巡检覆盖 (Coverage)
     已评估 / 未评估或不可用（含每项 N/A 原因）
附录 (Appendix)
     范围与 N/A 说明、方法论（两层模型/百分位口径）、术语、快照引用
```

**与上一版 8 节骨架的关系**：这是其业界升级版——把原"组件与资源健康"按两层模型拆成 §3 工作负载 + §4 资源；把"专项与深度"显式拆出 §6 可靠性/HA 与 §7 配置/安全；§8 升级为带 Finding 模型的"发现与优先建议"。执行摘要提到最前。

---

## 5. 每个 Plan 在每节填什么（Fill Matrix）

| 节 | Starter / Essential / Premium | Dedicated / BYOC |
|---|---|---|
| 1 执行摘要 | ✅ | ✅ |
| 2 基础信息与基线 | ✅（+ RCU Min/Max） | ✅（+ 拓扑/实例数） |
| 3 工作负载健康（对比表） | ✅ 全可见 | ✅ 全可见（本轮新增对比表） |
| 4 资源健康(USE) | ⚠️ 逻辑存储/连接/会话/RCU 余量；物理层 N/A | ✅ TiDB/TiKV/PD/基础设施 全 |
| 5 SQL 分析 | ✅ RU/慢次数/慢总耗时 Top-N | ✅ + 执行计划（待绑定） |
| 6 可靠性/HA/备份/DR | ⚠️ 多 N/A（平台托管） | ⚠️→✅ 采集待补 |
| 7 配置与安全 | ⚠️ 多 N/A | ⚠️→✅ 采集待补 |
| 8 发现与优先建议 | ✅ | ✅ |
| 9 巡检覆盖 | ✅ | ✅ |
| 附录 | ✅ | ✅ |

> ⚠️→✅ 表示结构已就位、采集尚未绑定，当前渲染 N/A + 叙述槽位；属"采集增补"轮次的工作，不在本轮。

---

## 6. Finding 模型（§8 的统一结构）

每条发现统一字段（借鉴 AWS WAF + ADDM）：

| 字段 | 含义 | 来源 |
|---|---|---|
| 严重度 Severity | CRITICAL / WARNING / NOTICE（对应内部 status；HRI≈CRITICAL） | 确定性引擎 |
| 区域 Area | 组件/类别的客户安全标签 | check → label |
| 量化影响 Impact | 指标值 / 余量 / 占比（如"P99 达 X ms"、"存储余量约 Y 个月"） | 确定性证据 |
| 证据 Evidence | 客户安全的指标摘要（无原始/内部标识） | 确定性 |
| 建议 Recommendation | 下一步动作 | LLM 解读 |
| 状态 Status | 未开始/处理中/已完成/已接受风险（跨期跟踪，可选） | 人工/平台 |

要点：**严重度 + 量化影响由确定性 Python 产出**；**建议/根因叙述由 LLM 填**（analysis_candidates → §8 槽位）。排序 = 严重度 × 工作量，Critical 优先。

---

## 7. 展示与隐私规范

- **三种展示元件**：① 对比表（本周期/上周期/趋势，用于 §3 及可对比的量）；② 状态表（`检查项 | 状态`，用于 §4/§6/§7 组件）；③ 叙述槽位（§1/§8 的 LLM 文本，及未绑定节）。统一由 `_basic_info_block` / `_status_table` / 对比行渲染，避免冗余。
- **语言**：zh 默认、`--lang en` 可切；标签走 `_LABELS` 字典。组件名（产品术语）保持规范英文。
- **单位/精度**：延迟 ms、RU/s、存储 GiB、比率 %；百分位统一 P99 为主、P50/P95 基线、P999/Max 尾部。
- **N/A 口径**：缺失/不可得/不适用一律 N/A + 原因（如"共享计划不采物理层""需 Ops 平台""需直连集群"），**绝不显示为 0 或 Healthy**。
- **隐私边界**：客户报告只渲染 状态 + 白名单标签 + 客户自有 SQL digest；校验器双语兜底拦截内部 check-ID / URL / PromQL / 实例名 / 跨租户 / 物理池值。

---

## 8. 与现有实现的映射（现状 vs 待补）

| 节 | 现有来源（已联调） | 待补 |
|---|---|---|
| 1 执行摘要 | status/confidence + analysis_candidates | 头条建议叙述（LLM） |
| 2 基础信息 | 元数据 + RCU Min/Max（clinic 详情 API）+ 统计健康度（`tidb_statistics_stats_healthy`） | 拓扑/实例数（dedicated）|
| 3 工作负载 | QPS/P99/P999/失败SQL/慢日志 + 上周期对比 | **活动会话指标**（待口径）|
| 4 资源(USE) | dedicated：TiKV/TiDB/PD/磁盘 已绑定；shared：RCU 余量 | **逻辑存储聚合**（待口径）；shared 连接/会话 |
| 5 SQL 分析 | RU/慢次数/慢总耗时 Top-N | 执行计划（dedicated 绑定）|
| 6 可靠性/HA/DR | dedicated：AVL-01 状态 / AVL-02 重启 / AVL-08 拓扑冗余（实例数≥3）/ CDC-01 changefeed 状态 / CDC-02 checkpoint 滞后（ticdc 指标）已绑定并联调 | 备份/PITR/连接证书为产品态（Clinic 指标不采）→ N/A；shared 平台托管 → N/A |
| 7 配置与安全 | — | 配置/权限采集（增补轮）|
| 8 发现与建议 | analysis_candidates → 槽位 | Finding 字段完善 + 状态跟踪 |
| 9 覆盖 / 附录 | 已评估/未评估 + 范围说明 | — |

> 两处"待口径"（活动会话、逻辑存储聚合）见 [`live-validation-checklist.md`](live-validation-checklist.md)。§6/§7 的采集属"采集增补"轮次。

---

## 9. 参考（业界实践）

- **黄金信号** — Google SRE Book, Monitoring Distributed Systems: https://sre.google/sre-book/monitoring-distributed-systems/
- **USE 方法** — Brendan Gregg: https://www.brendangregg.com/usemethod.html
- **RED 方法** — Grafana/Weaveworks: https://www.weave.works/blog/the-red-method-key-metrics-for-microservices-architecture/
- **AWS Well-Architected**（风险分级 HRI/MRI + 改进计划）: https://docs.aws.amazon.com/wellarchitected/latest/userguide/
- **Oracle AWR/ADDM**（基线窗口 + 按 DB-time 影响排序的发现）: https://docs.oracle.com/en/database/oracle/oracle-database/19/tgdba/automatic-performance-diagnostics.html
- **Percona pt-mysql-summary**（环境与配置清单）: https://docs.percona.com/percona-toolkit/pt-mysql-summary.html
- **SQL Server / Azure SQL 健康评估**（按焦点域分级建议）: https://learn.microsoft.com/en-us/azure/azure-monitor/insights/sql-assessment
- **价值框架** — Pythian "Seven Reasons You Need a SQL Database Health Check"；Datavail "Why 'If It Ain't Broke' Is Bad Advice for Databases"
- **呈现实践** — GitLab postgres-checkup（紧急度分层）；Google Database Center fleet-health best practices
