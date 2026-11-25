# 待办清单（Remaining Work）

> 报告统一为**一套动态编号骨架**（shared 7 节 / dedicated 9 节 + §关键指标趋势对比图），
> 解读经 `payload["interpretations"]` 注入，交付走全局 `lark-cli`（用户身份、markdown 直接 embed 图）。
> 下面是现状与尚未完成的部分。

## A. 已完成（本轮 2026-11-24）

- **按 plan 的采集容错**（都对着参考 spec `bitunix-weekly-inspection-spec` 验证）：
  - 延迟 `histogram_quantile ... by (le)` 为空时回退 `by (vmrange)`（Essential 直方图是 vmrange 桶）；
  - 慢日志 Data Proxy 无表时回退 **NGM 抽样**（`ngm_slow_query_list`），渲染标注"（抽样）/≥"；
  - RCU `tidb_ruv2_*` 缺失时回退 `resource_manager_resource_unit_read/write`（rate+total）；
  - 存储 `tidb_cluster_id` 标签缺失时回退无过滤查询（数据源已按集群鉴权）。
- **趋势对比图**（`report_charts.py`，matplotlib PNG）：一图左=上周期、右=本周期，各自真实日期轴、共用纵轴，
  均值虚线 + 峰值标注，P999 对数 Y；采集器按 `retain_series` 保留降采样时序；matplotlib 缺失自动跳过。
- **交付改走 `lark-cli`**（见下 D，推翻旧"手写 stdlib"决定）：`publish_report.py` 读 `config/delivery.v1.json`
  落点，发布前重跑隐私校验，`docs +create --as user` 一次带图上传；per-org 容器幂等复用。
- **收敛清理**：退役 `feishu_publish.py`（+CLI+测试）；`report_to_html.py` 降级为本地预览兜底；
  清空 `output/`；加 `.gitignore`；隐私校验改为**落盘前**执行。

## B. 采集增补（已有结构，待接数据）

- **§6 备份/PITR/连接证书（AVL-04/05/06）** —— 产品态，Clinic 指标无源；当前诚实 N/A。
  CDC-03..06（processor/sink 错误）可进一步绑定 ticdc 错误指标。
- **§7 安全细项（加密/TLS/权限，AVL-07）** —— 产品态/需控制台；当前给功能特性清单。
- **OVR-05 版本一致性** —— 需 dedicated 各组件版本，当前仅集群版本在 §2。
- **§5 索引健康 / SQLD-04/05**（avg latency、exec count、全表扫描）—— 可选深挖。

## C. 报告与模型增强

- **解读去硬编码数字** —— 叙述按**每轮现写**（不复用旧 interp JSON，数字会漂）。机制已就绪（injection + render CLI）。
- **统计信息健康度全分布** —— 现仅 `[0,50)` 计数；可展开各区间。
- **remediation 状态跟踪**（跨期 Not Started/In Progress/Done）、**PDF/DOCX 多格式** —— 可选。
- ✅ **报告文档已合并**：原 4 份收敛为单一 `report-design.md`（删除 report-template*.md 三份）。

## D. 已决定/已固定

- **交付 = 全局 `lark-cli`（用户身份）** —— 推翻此前"手写 stdlib、不用 lark-cli"的决定：lark-cli 原生 markdown
  import 直接 embed 本地图、以用户权限写 Wiki/Drive，比手写导入+填图块干净。落点**可配置**（`config/delivery.v1.json`
  + `FEISHU_PARENT_TOKEN`），便于迁移环境。
- **RCU Min/Max**：Clinic `minRCU/maxRCU`；RCU 速率 Essential 回退 `resource_manager`。
- **报告保持 Markdown**（不转 DocxXML）：可读可审 + 飞书原生带图。

## E. 架构债（长期）

- **`dedicated_health_inspection.py`（~2800 行）巨石**：主链路作为库调用 `inspect_cluster()`（dedicated 路径）；
  其自带 CLI 已标注 `[INTERNAL]`。长期目标是按 `collection_adapters` 里的 TODO 把 dedicated 逻辑逐块搬进
  adapter/recipe 模型，统一两套采集引擎——是独立项目，有回归风险，非一次性清理。
  **分阶段计划见 [`dedicated-migration-plan.md`](dedicated-migration-plan.md)**（先立 golden 护栏，按组搬，可回滚）。

## 验证口径（改动后必跑）
```bash
python3 -m unittest discover -s tests          # 当前 149 项全绿
python3 scripts/collection_adapters.py validate
python3 scripts/checklist_registry.py verify-markdown
python3 scripts/publish_report.py --report-dir output/<dir> --org <org> --dry-run  # 落点预检
# 联调（需 CLINIC_API_KEY）：Premium 10260745576873721799 / Essential 10560778748362855111 / Dedicated 10698648422638660035
```

## 参考基准（真实报告）
- Premium 生产：`pingcap.feishu.cn/wiki/ZRxXw9iM6ixM1uklpEtcUdPsn9e`
- Dedicated Draft：`pingcap.feishu.cn/wiki/O3l6w6CjmiiEtAkR4XfcW6rcn9e`
- 采集口径/容错细节见 `live-validation-checklist.md` 与参考 spec `Samples/.../bitunix-weekly-inspection-spec.md`。
