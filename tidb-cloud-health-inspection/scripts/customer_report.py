#!/usr/bin/env python3
"""Customer-safe Markdown renderers separated from internal checklist evidence.

One entry point, `render_health_report(normalized)`, produces the customer report
for every Plan from the canonical normalized model:
- logical Plans (Starter/Essential/Premium) use the P99-first metric layout;
- full-stack Plans (Dedicated/BYOC) use a canonical status/findings layout driven
  by check_results — it never prints raw evidence, so internal identifiers
  (instance names, pods, Grafana links, PromQL) cannot leak by construction.

The dedicated collector's internal Feishu report is an internal artifact, not the
customer report.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

FORBIDDEN_CUSTOMER_TERMS = (
    "Checklist Signature",
    "Registry Version",
    "Check ID",
    "SQL-02",
    "SQL-03",
    "RCU-01",
    "RCU-05",
    "Shared Plan",
    "Shared Resource",
    "shared pool",
    # Chinese equivalents (the shared-plan report is Chinese by default); the
    # structural guards below (URL / PromQL / check-ID) stay language-agnostic.
    "校验签名",
    "注册表版本",
    "共享资源池",
    "共享池",
    "物理资源池",
)

# Structural safety net: a customer report must never contain a URL, raw PromQL,
# or infrastructure identifiers. The renderers below never emit these; this guard
# catches regressions and hand-edited input.
_URL_RE = re.compile(r"https?://")
# Any bare canonical check/category ID (scoped to our own prefixes so a customer
# cluster name like "PROD-01" is not falsely rejected).
_CHECK_ID_RE = re.compile(r"\b(AVL|CAT|CDC|INF|LST|ORG|OVR|PD|RCU|SCOPE|SQLD|SQL|TF|TIDB|TIKV)-\d{2,}\b")
_PROMQL_INFRA_HINTS = (
    "sum(",
    "rate(",
    "increase(",
    "histogram_quantile",
    "by (instance",
    "by (le)",
    "grafana",
    "namespace",
    "tidb_cluster_id",
    "process_resident_memory",
    "process_cpu_seconds",
    "-tikv-",
)

# Customer-safe titles for full-stack checks. No internal Check IDs reach output.
# Dedicated/BYOC component check labels (customer-safe category names only; no
# instance names / internal IDs / infrastructure detail). Chinese default, English
# switchable — resolved via _ded_label(cid, lang).
DEDICATED_CHECK_LABELS = {
    "TIDB-01": {"zh": "计算节点 CPU", "en": "Compute node CPU"},
    "TIDB-02": {"zh": "计算节点内存", "en": "Compute node memory"},
    "TIKV-01": {"zh": "存储节点 CPU", "en": "Storage node CPU"},
    "TIKV-02": {"zh": "存储节点内存", "en": "Storage node memory"},
    "TIKV-03": {"zh": "存储请求吞吐与延迟", "en": "Storage request throughput and latency"},
    "TIKV-04": {"zh": "存储写入稳定性", "en": "Storage write stability"},
    "TIKV-05": {"zh": "存储流控", "en": "Storage flow control"},
    "TIKV-06": {"zh": "存储 Compaction 积压", "en": "Storage compaction backlog"},
    "TIKV-07": {"zh": "读负载均衡", "en": "Read workload balance"},
    "TIKV-08": {"zh": "存储错误信号", "en": "Storage error signals"},
    "TIKV-09": {"zh": "存储响应性", "en": "Storage responsiveness"},
    "TIKV-10": {"zh": "存储节点重启", "en": "Storage node restarts"},
    "AVL-01": {"zh": "产品可用性", "en": "Product availability"},
    "AVL-02": {"zh": "组件重启", "en": "Component restarts"},
    "AVL-08": {"zh": "拓扑冗余度", "en": "Topology redundancy"},
    "CDC-01": {"zh": "Changefeed 状态", "en": "Changefeed status"},
    "CDC-02": {"zh": "Changefeed 复制延迟", "en": "Changefeed replication lag"},
    "INF-03": {"zh": "磁盘容量", "en": "Disk capacity"},
    "INF-04": {"zh": "磁盘吞吐与 IOPS", "en": "Disk throughput and IOPS"},
    "INF-05": {"zh": "磁盘延迟", "en": "Disk latency"},
    "PD-03": {"zh": "数据分布均衡度", "en": "Data distribution balance"},
    "OVR-01": {"zh": "集群概览", "en": "Cluster overview"},
    "OVR-02": {"zh": "集群状态", "en": "Cluster state"},
}


def _ded_label(cid: str, lang: str) -> str:
    """Localized dedicated component label; falls back to English then the raw id."""
    entry = DEDICATED_CHECK_LABELS.get(cid)
    if isinstance(entry, dict):
        return entry.get(lang) or entry.get("en") or cid
    return entry or cid


# Trend-comparison charts: (key, check_id, {zh,en} title, value scale, log-y).
# report_charts.py draws these; the renderer emits an image ref for each metric
# whose evidence carries a retained 'series'. Filenames are charts/<key>.png.
CHART_METRICS: list[tuple[str, str, dict[str, str], float, bool]] = [
    ("qps", "SQL-01", {"zh": "QPS 趋势对比", "en": "QPS trend"}, 1.0, False),
    ("rcu", "RCU-01", {"zh": "RCU 趋势对比", "en": "RCU trend"}, 1.0, False),
    ("p99", "SQL-02", {"zh": "P99 趋势对比", "en": "P99 trend"}, 1000.0, False),
    ("p999", "SQL-03", {"zh": "P999 趋势对比", "en": "P999 trend"}, 1000.0, True),
    ("conn", "SQL-07", {"zh": "连接数趋势对比", "en": "Connections trend"}, 1.0, False),
]


def _available_charts(results: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    """(key, title) for each chartable metric whose evidence carries a retained
    series — i.e. the ones report_charts will actually have drawn a PNG for."""
    out: list[tuple[str, dict[str, str]]] = []
    for key, cid, title, _scale, _log in CHART_METRICS:
        ev = (results.get(cid) or {}).get("evidence") or {}
        if ev.get("series"):
            out.append((key, title))
    return out


def _sec_trends(num: str, results: dict[str, Any], lang: str) -> list[str]:
    """Dedicated 'Key Metric Trends' section grouping the current-vs-previous
    comparison charts, so they are not scattered under the data tables. Emitted
    only when at least one chart is available."""
    charts = _available_charts(results)
    if not charts:
        return []
    out = [f"## {num}. {_L(lang, 'key_trends')}", "", f"_{_L(lang, 'trends_note')}_", ""]
    for key, title in charts:
        out += [f"**{title.get(lang) or title['en']}**", "",
                f"![{title.get(lang) or title['en']}](charts/{key}.png)", ""]
    return out

_SEVERITY_RECOMMENDATION = {
    "CRITICAL": "Investigate immediately.",
    "WARNING": "Keep under watch and investigate if it persists.",
    "NOTICE": "Monitor; no action required yet.",
}

LOGICAL_PLANS = {"Starter", "Essential", "Premium"}


def _results(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["check_id"]): item for item in payload.get("check_results", []) if isinstance(item, dict) and item.get("check_id")}


def _number(value: Any, digits: int = 2) -> str:
    return "N/A" if value is None else f"{float(value):,.{digits}f}"


def _distribution_row(name: str, stats: dict[str, Any] | None, unit: str, scale: float = 1.0, extra: str = "") -> str:
    if not stats:
        return f"| {name} | N/A | Evidence unavailable |"
    primary = _number(float(stats["p99"]) * scale)
    support = (
        f"P50 {_number(float(stats['p50']) * scale)} {unit}; "
        f"P95 {_number(float(stats['p95']) * scale)} {unit}; "
        f"P999 {_number(float(stats['p999']) * scale)} {unit}; "
        f"Max {_number(float(stats['max']) * scale)} {unit}"
    )
    if extra:
        support += f"; {extra}"
    return f"| {name} | **{primary} {unit}** | {support} |"


# ── shared-plan report localization + helpers ────────────────
# The shared-plan (Starter/Essential/Premium) customer report mirrors the
# production Feishu report: 基础信息 + 集群用量（本周期/上周期/趋势）+ SQL 分析
# (owner-facing Top-N) + 发现与建议 (LLM narrative slots). Default language is
# Chinese; English is switchable via payload["lang"].

_LABELS: dict[str, dict[str, str]] = {
    "title": {"zh": "TiDB Cloud 集群巡检报告", "en": "TiDB Cloud Health Inspection"},
    "overall": {"zh": "总体健康状态", "en": "Overall Health Status"},
    "confidence": {"zh": "置信度", "en": "Confidence"},
    "basic_info": {"zh": "基础信息", "en": "Basic Information"},
    "field": {"zh": "字段", "en": "Field"},
    "value": {"zh": "值", "en": "Value"},
    "period": {"zh": "巡检周期", "en": "Inspection Period"},
    "cluster_name": {"zh": "集群名称", "en": "Cluster Name"},
    "cluster_id": {"zh": "集群 ID", "en": "Cluster ID"},
    "provider_region": {"zh": "Provider / Region", "en": "Provider / Region"},
    "version": {"zh": "版本", "en": "Version"},
    "rcu": {"zh": "RCU Min / Max（当前）", "en": "RCU Min / Max (current)"},
    "stats_health": {"zh": "统计信息健康度 [0,50)", "en": "Statistics health [0,50)"},
    "report_date": {"zh": "报告日期", "en": "Report Date"},
    "usage": {"zh": "集群用量", "en": "Cluster Usage"},
    "metric": {"zh": "指标", "en": "Metric"},
    "cur": {"zh": "本周期", "en": "Current period"},
    "prev": {"zh": "上周期", "en": "Previous period"},
    "trend": {"zh": "趋势", "en": "Trend"},
    "qps": {"zh": "QPS（Max / Avg）", "en": "QPS (Max / Avg)"},
    "ru": {"zh": "RU 用量（Max / Avg，RU/s）", "en": "RU usage (Max / Avg, RU/s)"},
    "p99": {"zh": "查询延迟 P99（Max / Avg）", "en": "Query duration P99 (Max / Avg)"},
    "p999": {"zh": "查询延迟 P999（Max / Avg）", "en": "Query duration P999 (Max / Avg)"},
    "failed_sql": {"zh": "失败 SQL", "en": "Failed SQL"},
    "slow_log": {"zh": "慢日志（总条数 / 唯一条数）", "en": "Slow log (total / unique)"},
    "connections": {"zh": "连接数（Max / Avg）", "en": "Connections (Max / Avg)"},
    "sessions": {"zh": "活动连接（Max / Avg）", "en": "Active connections (Max / Avg)"},
    "storage": {"zh": "存储容量（起点 / 终点）", "en": "Storage (start / end)"},
    "sql_analysis": {"zh": "SQL 分析", "en": "SQL Analysis"},
    "ru_topn": {"zh": "消耗 RU Top 10", "en": "RU consumption Top 10"},
    "slow_count_topn": {"zh": "慢 SQL 出现次数 Top 10", "en": "Slow-SQL frequency Top 10"},
    "slow_dur_topn": {"zh": "慢 SQL 总耗时 Top 10", "en": "Slow-SQL total-duration Top 10"},
    "exec_plan_sub": {"zh": "执行计划与优化器", "en": "Execution Plan & Optimizer"},
    "plan_cache_hit": {"zh": "Plan cache 命中率", "en": "Plan cache hit rate"},
    "exec_plan_note": {
        "zh": "执行计划/索引为观测信号；低 Plan cache 命中率提示计划重编译开销，深入分析见下方解读。",
        "en": "Execution-plan/index signals are observational; a low plan-cache hit rate suggests recompilation overhead — see interpretation below.",
    },
    "rank": {"zh": "排名", "en": "Rank"},
    "digest": {"zh": "SQL Digest", "en": "SQL Digest"},
    "value_col": {"zh": "数值", "en": "Value"},
    "findings": {"zh": "发现与建议", "en": "Findings & Recommendations"},
    "key_findings": {"zh": "关键发现", "en": "Key Findings"},
    "key_risks": {"zh": "关键风险", "en": "Key Risks"},
    "recommendations": {"zh": "优先建议", "en": "Top Recommendations"},
    "llm_slot": {"zh": "（待分析解读）", "en": "(interpretation pending)"},
    "no_candidates": {"zh": "本周期无 WARNING/CRITICAL 项。", "en": "No WARNING/CRITICAL items this period."},
    "coverage": {"zh": "巡检覆盖", "en": "Inspection Coverage"},
    "assessed": {"zh": "已评估", "en": "Assessed"},
    "not_assessed": {"zh": "未评估或不可用", "en": "Not assessed or unavailable"},
    "scope_notes": {"zh": "范围与 N/A 说明", "en": "Scope and N/A Notes"},
    "scope_body": {
        "zh": "本次巡检仅使用租户范围内的逻辑集群证据；无法安全归属到本集群的基础设施层证据已被排除。缺失证据记为 N/A，不视为健康。SQL Top-N 仅为集群所有者展示其自有 SQL 的排名。",
        "en": "This inspection used tenant-scoped logical-cluster evidence only. Infrastructure-level evidence that cannot be safely attributed to this cluster was excluded. Missing evidence is reported as N/A, never Healthy. SQL Top-N shows the cluster owner's own SQL ranking only.",
    },
    "na": {"zh": "N/A", "en": "N/A"},
    "inst_unit": {"zh": "个实例", "en": "inst"},
    "slow_score": {"zh": "慢分", "en": "slow score"},
    "peak_prefix": {"zh": "峰值", "en": "Max"},
    "pending": {"zh": "源待接入", "en": "source binding pending"},
    "stats_unavailable": {"zh": "需直连集群，Clinic 不可得", "en": "needs direct cluster access; not available via Clinic"},
    "rcu_ops": {"zh": "需 Ops 平台；Clinic 仅有池级共享值，非本集群配置", "en": "needs Ops platform; Clinic only exposes a pool-level shared value"},
    "cumulative": {"zh": "累计", "en": "cumulative"},
    "ratio": {"zh": "比率", "en": "ratio"},
    "times": {"zh": "次", "en": ""},
    # ── dedicated / full-stack report sections (mirror the Draft) ──
    "ded_title": {"zh": "TiDB 集群健康检查报告", "en": "TiDB Health Check Report"},
    "area": {"zh": "检查项", "en": "Area"},
    "status_col": {"zh": "状态", "en": "Status"},
    "recommendation": {"zh": "建议", "en": "Recommendation"},
    "exec_summary": {"zh": "执行摘要", "en": "Executive Summary"},
    "overall_health": {"zh": "总体健康状态", "en": "Overall Health Status"},
    "cluster_overview_sec": {"zh": "集群概览与基线", "en": "Cluster Overview & Baseline"},
    "architecture_obs": {"zh": "架构观察", "en": "Architecture Observations"},
    "capacity_resource": {"zh": "容量与资源利用", "en": "Capacity & Resource Utilization"},
    "tikv_resource": {"zh": "TiKV 资源", "en": "TiKV Resource Usage"},
    "tidb_resource": {"zh": "TiDB 资源", "en": "TiDB Resource Usage"},
    "pd_resource": {"zh": "PD 资源与调度", "en": "PD Resource & Scheduling"},
    "infra": {"zh": "基础设施", "en": "Infrastructure"},
    "capacity_sub": {"zh": "存储与容量", "en": "Storage & Capacity"},
    "performance_workload": {"zh": "性能与负载分析", "en": "Performance & Workload Analysis"},
    "sql_exec_plan": {"zh": "SQL 与执行计划健康度", "en": "SQL & Execution Plan Health"},
    "stats_analyze": {"zh": "统计信息与 Analyze 状态", "en": "Statistics & Analyze Status"},
    "index_distribution": {"zh": "索引与数据分布", "en": "Index & Data Distribution"},
    "txn_locking": {"zh": "事务与锁", "en": "Transaction & Locking"},
    "replication_ha": {"zh": "复制、高可用与容错", "en": "Replication, HA & Fault Tolerance"},
    "backup_dr": {"zh": "备份、恢复与容灾", "en": "Backup, Restore & DR"},
    "config_best_practices": {"zh": "配置与最佳实践复核", "en": "Configuration & Best Practices"},
    "observability_ops": {"zh": "可观测性与运维", "en": "Observability & Operations"},
    "checklist_coverage": {"zh": "巡检覆盖", "en": "Checklist Coverage"},
    "component_overview": {"zh": "组件健康总览", "en": "Component Health Overview"},
    "action_plan": {"zh": "行动计划与建议", "en": "Action Plan & Recommendations"},
    "appendix_scope": {"zh": "附录：范围与 N/A 说明", "en": "Appendix: Scope and N/A Notes"},
    "pending_bind": {"zh": "本轮未绑定确定性证据，待解读。", "en": "No deterministic evidence bound this round; interpretation pending."},
    "ded_scope_body": {
        "zh": "本次巡检使用本 Dedicated/BYOC 集群授权范围内的全栈组件与基础设施证据。缺失或未绑定证据记为 N/A，不视为健康。",
        "en": "This inspection used full-stack component and infrastructure evidence authorized for this dedicated cluster. Missing or unbound evidence is N/A, never Healthy.",
    },
    # ── unified 9-section skeleton (shared by all plans) ──
    "env_baseline": {"zh": "基础信息与基线", "en": "Environment & Baseline"},
    "workload_health": {"zh": "工作负载健康", "en": "Workload Health"},
    "resource_health": {"zh": "资源健康", "en": "Resource Health"},
    "key_trends": {"zh": "关键指标趋势", "en": "Key Metric Trends"},
    "trends_note": {"zh": "每张图左为上周期、右为本周期，各自真实日期轴；虚线为该周期均值，标注点为峰值；左右共用纵轴便于对比。",
                     "en": "Each chart: left = previous period, right = current period, each on its own date axis; dashed = that period's mean, marker = its peak; shared y-axis for comparison."},
    "reliability_dr": {"zh": "可靠性 / HA / 备份 / 容灾", "en": "Reliability / HA / Backup / DR"},
    "reliability_shared_note": {
        "zh": "共享计划的高可用与备份/容灾由平台托管，不在本巡检范围内呈现。",
        "en": "HA and backup/DR are platform-managed for shared plans and are not surfaced by this inspection.",
    },
    "reliability_backup_note": {
        "zh": "备份策略/最近备份、PITR 恢复窗口、连接证书为产品态，Clinic 指标不采集 → N/A。",
        "en": "Backup policy/last backup, PITR window and connectivity/cert state are product state not exposed via Clinic metrics → N/A.",
    },
    "findings_reco": {"zh": "发现与优先建议", "en": "Findings & Prioritized Recommendations"},
    "impact_col": {"zh": "量化影响", "en": "Quantified impact"},
    "reco_critical": {"zh": "立即排查", "en": "Investigate immediately"},
    "reco_warning": {"zh": "持续观察，若持续则排查", "en": "Keep under watch; investigate if it persists"},
    "reco_notice": {"zh": "监控，暂无需处理", "en": "Monitor; no action required yet"},
    "reco_review": {"zh": "复核", "en": "Review"},
    "config_security": {"zh": "配置与功能特性", "en": "Configuration & Features"},
    "feature_inventory": {"zh": "功能特性清单", "en": "Feature inventory"},
    "security_sub": {"zh": "安全", "en": "Security"},
    "feature": {"zh": "特性", "en": "Feature"},
    "enabled": {"zh": "启用", "en": "Enabled"},
    "disabled": {"zh": "禁用", "en": "Disabled"},
    "security_note": {
        "zh": "加密/TLS/证书/权限为平台托管或产品态，Clinic 指标不采集 → 需在控制台/Ops 平台核对。",
        "en": "Encryption/TLS/cert/privileges are platform-managed or product state, not exposed via Clinic → verify in console/Ops.",
    },
    "shared_physical_note": {
        "zh": "共享计划（Starter/Essential/Premium）按设计不采集物理层(TiKV/PD/节点/磁盘)证据；此处仅呈现租户安全的逻辑资源。",
        "en": "Shared plans (Starter/Essential/Premium) do not collect physical-layer (TiKV/PD/node/disk) evidence by design; only tenant-safe logical resources are shown here.",
    },
    "workload_pending": {
        "zh": "该计划的工作负载指标采集尚未接入本路径，暂为 N/A。",
        "en": "Workload metrics collection is not yet wired for this path; shown as N/A.",
    },
    "section_pending": {"zh": "采集尚未接入，暂为 N/A / 待解读。", "en": "Collection not yet wired; N/A / interpretation pending."},
}


def _L(lang: str, key: str) -> str:
    entry = _LABELS.get(key, {})
    return entry.get(lang) or entry.get("en") or key


def _interp(payload: dict[str, Any], key: str, lang: str) -> str:
    """Resolve an LLM-written interpretation slot by key. The deterministic
    renderer owns scoring; narrative is injected here via payload['interpretations'].
    A value may be a plain string or a {'zh':.., 'en':..} mapping. Missing/empty →
    the honest "interpretation pending" placeholder (never fabricated prose)."""
    blob = (payload.get("interpretations") or {}).get(key)
    if isinstance(blob, dict):
        blob = blob.get(lang) or blob.get("zh") or blob.get("en")
    if isinstance(blob, str) and blob.strip():
        return blob.strip()
    return _L(lang, "llm_slot")


def _ev(results: dict[str, Any], check_id: str) -> dict[str, Any]:
    return (results.get(check_id) or {}).get("evidence") or {}


def _stats(results: dict[str, Any], check_id: str, keys: tuple[str, ...]) -> dict[str, Any] | None:
    evidence = _ev(results, check_id)
    for key in keys:
        candidate = evidence.get(key)
        if isinstance(candidate, dict):
            return candidate
    return None


def _maxavg(stats: dict[str, Any] | None) -> tuple[float | None, float | None]:
    if not isinstance(stats, dict):
        return None, None
    return stats.get("max"), stats.get("avg")


def _pair(mx: float | None, av: float | None, unit: str, scale: float = 1.0) -> str:
    if mx is None and av is None:
        return "N/A"

    def one(v: float | None) -> str:
        return "N/A" if v is None else _number(float(v) * scale)

    suffix = f" {unit}" if unit else ""
    return f"{one(mx)} / {one(av)}{suffix}"


def _pct(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev in (None, 0):
        return None
    return (float(cur) - float(prev)) / abs(float(prev)) * 100.0


def _trend_maxavg(cur: tuple[float | None, float | None], prev: tuple[float | None, float | None]) -> str:
    parts = []
    for label, c, p in (("Max", cur[0], prev[0]), ("Avg", cur[1], prev[1])):
        delta = _pct(c, p)
        if delta is not None:
            parts.append(f"{label} {delta:+.2f}%")
    return "；".join(parts) if parts else "N/A"


def _usage_row(label: str, cur_results: dict[str, Any], prev_results: dict[str, Any],
               check_id: str, keys: tuple[str, ...], unit: str, scale: float = 1.0) -> str:
    cur = _maxavg(_stats(cur_results, check_id, keys))
    prev = _maxavg(_stats(prev_results, check_id, keys))
    return f"| {label} | {_pair(*cur, unit, scale)} | {_pair(*prev, unit, scale)} | {_trend_maxavg(cur, prev)} |"


_HEX_RE = re.compile(r"[^0-9a-fA-F]")


def _safe_digest(row: dict[str, Any]) -> str:
    """Owner-facing but infrastructure-safe: emit the SQL digest (an opaque hex
    identifier), never raw SQL text (which could contain tokens the structural
    validator treats as infrastructure hints)."""
    for key in ("digest", "sql_digest", "plan_digest"):
        value = row.get(key)
        if value:
            cleaned = _HEX_RE.sub("", str(value))[:32]
            if cleaned:
                return cleaned
    return "N/A"


def _safe_metric(row: dict[str, Any], mode: str) -> str:
    key_sets = {
        "ru": ("total_ru", "ru", "ru_sum", "sum_ru"),
        "slow_count": ("count", "exec_count", "cnt", "frequency"),
        "slow_duration": ("total_query_time", "sum_latency", "total_time", "sum_query_time", "query_time"),
    }
    for key in key_sets.get(mode, ()):
        if row.get(key) is not None:
            try:
                return _number(float(row[key]))
            except (TypeError, ValueError):
                return str(row[key])
    return "N/A"


def _topn_section(results: dict[str, Any], check_id: str, mode: str, lang: str, heading: str, metric_label: str) -> list[str]:
    out = [f"### {heading}", ""]
    row_result = results.get(check_id) or {}
    rows = _ev(results, check_id).get("rows") or []
    if row_result.get("status") == "N/A" or not rows:
        out.extend([f"_{_L(lang, 'na')}_", ""])
        return out
    out.append(f"| {_L(lang, 'rank')} | {_L(lang, 'digest')} | {metric_label} |")
    out.append("|---:|---|---:|")
    for index, row in enumerate(rows, 1):
        if isinstance(row, dict):
            out.append(f"| {index} | `{_safe_digest(row)}` | {_safe_metric(row, mode)} |")
    out.append("")
    return out


def _report_header(payload: dict[str, Any], lang: str, title_key: str) -> list[str]:
    """Report title + overall-status / confidence line (shared by all plans)."""
    status = payload.get("status") or "N/A"
    confidence = payload.get("confidence") or "LOW"
    return [
        f"# {_L(lang, title_key)} — {payload.get('cluster_name') or payload.get('cluster_id')}",
        "",
        f"> **{_L(lang, 'overall')}: {status}** · **{_L(lang, 'confidence')}: {confidence}**",
        "",
    ]


def _basic_info_table(payload: dict[str, Any], lang: str, extra_rows: list[str] | None = None) -> list[str]:
    """The 基础信息 table rows (no section heading). ``extra_rows`` are plan-specific
    rows (e.g. the logical RCU / stats-health rows) inserted before the report date."""
    metadata = payload.get("metadata") or {}
    window = payload.get("window") or {}
    na = _L(lang, "na")
    period = na if not window.get("start") else f"{window.get('start')} — {window.get('end')}"
    rows = [
        f"| {_L(lang, 'field')} | {_L(lang, 'value')} |",
        "|---|---|",
        f"| {_L(lang, 'cluster_name')} | {payload.get('cluster_name') or na} |",
        f"| {_L(lang, 'cluster_id')} | {payload.get('cluster_id') or na} |",
        f"| {_L(lang, 'provider_region')} | {metadata.get('provider') or na} / {metadata.get('region') or na} |",
        f"| {_L(lang, 'version')} | {metadata.get('version') or na} |",
        f"| {_L(lang, 'period')} | {period} |",
    ]
    rows += list(extra_rows or [])
    rows += [f"| {_L(lang, 'report_date')} | {datetime.now(timezone.utc).date().isoformat()} |", ""]
    return rows


def _status_table(rows: list[tuple[str, str]], lang: str) -> list[str]:
    """A | Area | Status | table from (label, status) pairs; '_N/A_' when empty."""
    if not rows:
        return [f"_{_L(lang, 'na')}_", ""]
    out = [f"| {_L(lang, 'area')} | {_L(lang, 'status_col')} |", "|---|---|"]
    out += [f"| {label} | {status} |" for label, status in rows]
    out.append("")
    return out


_COMPARE_HEADER = ("metric", "cur", "prev", "trend")


def _compare_header(lang: str) -> list[str]:
    return [
        f"| {_L(lang, 'metric')} | {_L(lang, 'cur')} | {_L(lang, 'prev')} | {_L(lang, 'trend')} |",
        "|---|---|---|---|",
    ]


def _sec_exec_summary(num: str, payload: dict[str, Any], lang: str) -> list[str]:
    """§1 Executive Summary: one LLM-written summary paragraph + the deterministic
    key-findings list (overall status is already on the report header)."""
    candidates = payload.get("analysis_candidates") or []
    out = [
        f"## {num}. {_L(lang, 'exec_summary')}",
        "",
        _interp(payload, "exec_summary", lang),  # one-paragraph prose summary (not a list item)
        "",
        f"**{_L(lang, 'key_findings')}**",
        "",
    ]
    if candidates:
        for item in candidates:
            out.append(f"- **{item.get('status', 'N/A')}** — {_finding_area(str(item.get('check_id')), lang)}")
    else:
        out.append(f"- {_L(lang, 'no_candidates')}")
    out.append("")
    return out


def _day(iso: Any) -> str:
    """Date part (YYYY-MM-DD) of an ISO timestamp; '?' when absent/malformed."""
    s = str(iso or "")
    return s[:10] if len(s) >= 10 else "?"


def _periods_note(payload: dict[str, Any], lang: str) -> str:
    """Declare the concrete date ranges that the 本周期 / 上周期 columns compare,
    so the comparison table's first use is self-explanatory (UTC)."""
    cur_w = payload.get("window") or {}
    prev_w = payload.get("previous_window") or {}
    cur_r = f"{_day(cur_w.get('start'))} ~ {_day(cur_w.get('end'))}"
    if prev_w.get("start") or prev_w.get("end"):
        prev_r = f"{_day(prev_w.get('start'))} ~ {_day(prev_w.get('end'))}"
    else:
        prev_r = _L(lang, "na")
    if lang == "zh":
        return f"_本周期：{cur_r} UTC；上周期：{prev_r} UTC_"
    return f"_Current period: {cur_r} UTC; previous period: {prev_r} UTC_"


def _sec_workload(num: str, lang: str, cur: dict[str, Any], prev: dict[str, Any], is_logical: bool,
                  payload: dict[str, Any]) -> list[str]:
    """§3 Workload Health (RED): comparison table. Dedicated has no tenant SQL
    workload bound on its path yet → N/A rows + a pending note (structure kept)."""
    out = [f"## {num}. {_L(lang, 'workload_health')}", "", _periods_note(payload, lang), ""]
    out += _compare_header(lang)
    out += [
        _usage_row(_L(lang, "qps"), cur, prev, "SQL-01", ("statistics",), "queries/s"),
        _usage_row(_L(lang, "ru"), cur, prev, "RCU-01", ("statistics_ru_per_second",), "RU/s"),
        _usage_row(_L(lang, "p99"), cur, prev, "SQL-02", ("statistics_seconds",), "ms", 1000.0),
        _usage_row(_L(lang, "p999"), cur, prev, "SQL-03", ("statistics_seconds",), "ms", 1000.0),
        _failed_sql_row(_L(lang, "failed_sql"), cur, prev, lang),
        _slow_log_row(_L(lang, "slow_log"), cur, prev, lang),
        "",
    ]
    return out




def _sec_resources(num: str, lang: str, cur: dict[str, Any], prev: dict[str, Any], is_logical: bool,
                   metadata: dict[str, Any]) -> list[str]:
    """§4 Resource Health (USE), 4.1 TiDB / 4.2 TiKV / 4.3 PD / 4.4 Infra. Shared
    plans show tenant-safe logical resources and N/A the physical layer by design;
    dedicated shows the bound component status tables."""
    out = [f"## {num}. {_L(lang, 'resource_health')}", ""]
    if is_logical:
        # Shared plans only surface what they actually have: TiDB-layer connections
        # + logical storage. Physical TiKV/PD/disk are not collected by design and
        # are simply omitted (not shown as empty N/A sections).
        out += [f"### {num}.1 {_L(lang, 'tidb_resource')}", ""]
        out += _compare_header(lang)
        out += [
            _usage_row(_L(lang, "connections"), cur, prev, "SQL-07", ("statistics",), ""),
            _usage_row(_L(lang, "sessions"), cur, prev, "SQL-08", ("statistics",), ""),
            "",
        ]
        out += [f"### {num}.2 {_L(lang, 'capacity_sub')}", ""]
        out += _compare_header(lang)
        out += [_storage_row(_L(lang, "storage"), cur, prev, lang), ""]
    else:
        out += [f"### {num}.1 {_L(lang, 'tidb_resource')}", ""] + _ded_table(cur, ("TIDB-01", "TIDB-02"), lang)
        out += [f"### {num}.2 {_L(lang, 'tikv_resource')}", ""] + _ded_table(cur, _DED_TIKV, lang)
        out += [f"### {num}.3 {_L(lang, 'pd_resource')}", ""] + _ded_table(cur, _DED_PD, lang)
        out += [f"### {num}.4 {_L(lang, 'infra')}", ""] + _ded_table(cur, _DED_INFRA, lang)
    return out


def _sec_sql(num: str, lang: str, cur: dict[str, Any], is_logical: bool, metadata: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """§5 SQL Analysis: owner-facing Top-N (all plans) + execution-plan health
    (dedicated: plan-cache hit rate, observational)."""
    out = [f"## {num}. {_L(lang, 'sql_analysis')}", ""]
    out += _topn_section(cur, "SQLD-01", "ru", lang, f"{num}.1 {_L(lang, 'ru_topn')}", "RU")
    out += _topn_section(cur, "SQLD-02", "slow_count", lang, f"{num}.2 {_L(lang, 'slow_count_topn')}", _L(lang, "value_col"))
    out += _topn_section(cur, "SQLD-03", "slow_duration", lang, f"{num}.3 {_L(lang, 'slow_dur_topn')}", "ms")
    if not is_logical:
        hit = metadata.get("plan_cache_hit_pct")
        out += [f"### {num}.4 {_L(lang, 'exec_plan_sub')}", ""]
        if isinstance(hit, (int, float)):
            out += [f"| {_L(lang, 'metric')} | {_L(lang, 'value')} |", "|---|---|",
                    f"| {_L(lang, 'plan_cache_hit')} | {_number(hit)}% |", ""]
        else:
            out += [f"_{_L(lang, 'na')}_", ""]
        out += [f"_{_L(lang, 'exec_plan_note')}_", "", _interp(payload, "exec_plan", lang), ""]
    return out


def _sec_config_security(num: str, lang: str, metadata: dict[str, Any]) -> list[str]:
    """§7 Configuration: authorized feature inventory (featureGates). Security
    (encryption/TLS/cert/privilege) is not exposed via Clinic, so it is not
    inspected here — the section surfaces only what is observable."""
    out = [f"## {num}. {_L(lang, 'config_security')}", ""]
    gates = metadata.get("feature_gates")
    if isinstance(gates, dict) and gates:
        on, off = _L(lang, "enabled"), _L(lang, "disabled")
        out += [f"| {_L(lang, 'feature')} | {_L(lang, 'status_col')} |", "|---|---|"]
        out += [f"| {name} | {on if gates[name] else off} |" for name in sorted(gates)]
    else:
        out.append(f"_{_L(lang, 'na')}_")
    out.append("")
    return out


def _sec_reliability(num: str, lang: str, cur: dict[str, Any]) -> list[str]:
    """§6 Reliability / HA / Backup / DR. Shared plans: platform-managed → note.
    Dedicated: availability / topology / changefeed status table; backup/PITR/cert
    are product state not in Clinic metrics (noted)."""
    out = [f"## {num}. {_L(lang, 'reliability_dr')}", ""]
    out += _ded_table(cur, _DED_RELIABILITY, lang)
    out += [f"_{_L(lang, 'reliability_backup_note')}_", ""]
    return out


def render_health_report(payload: dict[str, Any]) -> str:
    """Single unified customer report for every plan (one 9-section skeleton).

    Structure is identical across plans; each section fills what the plan's
    capability allows and renders N/A (with a reason) otherwise. Built only from
    canonical statuses + whitelisted labels + owner SQL digests, so raw evidence /
    internal identifiers / PromQL cannot leak (validated at the end)."""
    lang = "zh" if str(payload.get("lang") or "zh").lower() == "zh" else "en"
    is_logical = str(payload.get("plan")) in LOGICAL_PLANS
    cur = _results(payload)
    prev = payload.get("previous_period") or {}
    metadata = payload.get("metadata") or {}
    na = _L(lang, "na")

    # §2 plan-specific baseline rows (shared plans: RCU + stats health).
    extra_rows: list[str] = []
    if is_logical:
        extra_rows = [
            f"| {_L(lang, 'rcu')} | " + (
                f"{_number(metadata.get('rcu_min'), 0)} / {_number(metadata.get('rcu_max'), 0)} RCU"
                if (metadata.get("rcu_min") is not None or metadata.get("rcu_max") is not None)
                else f"{na}（{_L(lang, 'rcu_ops')}）"
            ) + " |",
            f"| {_L(lang, 'stats_health')} | " + (
                f"{_number(metadata.get('stats_unhealthy_count'), 0)} {'张表' if lang == 'zh' else 'tables'}"
                if metadata.get("stats_unhealthy_count") is not None
                else f"{na}（{_L(lang, 'stats_unavailable')}）"
            ) + " |",
        ]

    _counter = [0]

    def n() -> str:
        _counter[0] += 1
        return str(_counter[0])

    lines = _report_header(payload, lang, "title" if is_logical else "ded_title")
    lines += _sec_exec_summary(n(), payload, lang)
    lines += [f"## {n()}. {_L(lang, 'env_baseline')}", ""] + _basic_info_table(payload, lang, extra_rows)
    lines += _sec_workload(n(), lang, cur, prev, is_logical, payload)
    lines += _sec_resources(n(), lang, cur, prev, is_logical, metadata)
    trends = _sec_trends("", cur, lang)
    if trends:
        trends[0] = f"## {n()}. {_L(lang, 'key_trends')}"  # number only when present
        lines += trends
    lines += _sec_sql(n(), lang, cur, is_logical, metadata, payload)
    # §6 Reliability / §7 Configuration are platform-managed for shared resource-pool
    # plans → not inspected or shown. Dedicated/BYOC only.
    if not is_logical:
        lines += _sec_reliability(n(), lang, cur)
        lines += _sec_config_security(n(), lang, metadata)
    lines += _sec_findings(n(), payload, lang)
    lines += _sec_coverage(n(), cur, lang, is_logical)

    report = "\n".join(lines)
    validate_customer_report(report)
    return report


def _failed_sql_row(label: str, cur: dict[str, Any], prev: dict[str, Any], lang: str) -> str:
    def cell(results: dict[str, Any]) -> str:
        ev = _ev(results, "SQL-05")
        count = ev.get("estimated_error_count")
        ratio = ev.get("window_average_ratio_percent")
        if count is None and ratio is None:
            return "N/A"
        return f"{_L(lang, 'cumulative')} {_number(count, 0)} {_L(lang, 'times')}；{_L(lang, 'ratio')} {_number(ratio, 4)}%".strip()

    c = _ev(cur, "SQL-05").get("estimated_error_count")
    p = _ev(prev, "SQL-05").get("estimated_error_count")
    trend = _pct(c, p)
    trend_text = f"{trend:+.2f}%" if trend is not None else "N/A"
    return f"| {label} | {cell(cur)} | {cell(prev)} | {trend_text} |"


def _slow_log_row(label: str, cur: dict[str, Any], prev: dict[str, Any], lang: str) -> str:
    """One row for slow-log volume: total entries + distinct digests (from SQL-06).
    Cell = "total / unique"; trend tracks total count."""
    unit = "条" if lang == "zh" else ""

    def cell(results: dict[str, Any]) -> str:
        ev = _ev(results, "SQL-06")
        total, uniq = ev.get("window_total"), ev.get("unique_digests")
        if total is None and uniq is None:
            return "N/A"
        t = _number(total, 0) if total is not None else "N/A"
        u = _number(uniq, 0) if uniq is not None else "N/A"
        # NGM bounded sample (Essential/shared without Data Proxy): mark honestly,
        # and show "≥" when the sample was capped (count is a lower bound).
        suffix = ""
        if ev.get("sampled"):
            tag = "抽样" if lang == "zh" else "sampled"
            suffix = f"（{tag}）" if lang == "zh" else f" ({tag})"
            if ev.get("sample_capped") and total is not None:
                t = "≥" + t
        return f"{t} / {u} {unit}".rstrip() + suffix

    c = _ev(cur, "SQL-06").get("window_total")
    p = _ev(prev, "SQL-06").get("window_total")
    trend = _pct(c, p)
    trend_text = f"{trend:+.2f}%" if trend is not None else "N/A"
    return f"| {label} | {cell(cur)} | {cell(prev)} | {trend_text} |"


def _storage_row(label: str, cur: dict[str, Any], prev: dict[str, Any], lang: str) -> str:
    """Logical storage over the window from LST-01 (keyspace used, replica-collapsed).
    本周期 = 起点/终点; 趋势 = within-window growth %. 上周期 = previous window's end."""
    ev = _ev(cur, "LST-01")
    start, end = ev.get("start"), ev.get("end")

    def gib(value: Any) -> str:
        return "N/A" if value is None else f"{_number(float(value) / (1024 ** 3))} GiB"

    if start is None and end is None:
        return f"| {label} | N/A | N/A | N/A |"
    cur_cell = f"{gib(start)} / {gib(end)}"
    prev_end = _ev(prev, "LST-01").get("end")
    prev_cell = "N/A" if prev_end is None else gib(prev_end)
    growth = ev.get("growth_percent")
    trend = f"{float(growth):+.2f}%" if isinstance(growth, (int, float)) else "N/A"
    return f"| {label} | {cur_cell} | {prev_cell} | {trend} |"


def _finding_area(check_id: str, lang: str) -> str:
    """Customer-safe area label for a finding (never the internal check ID)."""
    if check_id in DEDICATED_CHECK_LABELS:
        return _ded_label(check_id, lang)
    label = _LOGICAL_COVERAGE_LABELS.get(check_id)
    if label:
        return label.get(lang) or label["en"]
    return _L(lang, "area")


def _finding_impact(result: dict[str, Any], lang: str) -> str:
    """A customer-safe quantified impact from the check's evidence — a number with a
    unit, never raw PromQL / IDs. Borrowed from ADDM's 'quantify the impact' model."""
    ev = result.get("evidence") or {}
    specific = (
        ("window_average_ratio_percent", lambda v: f"{_number(v, 4)}%"),
        ("slow_score_peak", lambda v: f"{_L(lang, 'slow_score')} {_number(v, 0)}"),
        ("raft_log_lag_p99_peak", lambda v: f"{_number(v, 0)} ms"),
        ("cdc_checkpoint_lag_max", lambda v: f"{_number(v)} s"),
        ("tikv_instance_count", lambda v: f"{_number(v, 0)} {_L(lang, 'inst_unit')}"),
        ("window_total", lambda v: _number(v, 0)),
    )
    for key, fmt in specific:
        if isinstance(ev.get(key), (int, float)):
            return fmt(ev[key])
    # Latency / throughput distributions carry a unit → "峰值 X ms" / "Max X ms".
    for key, unit in (("statistics_seconds", " ms"), ("statistics", ""), ("statistics_ru_per_second", " RU/s")):
        stat = ev.get(key)
        if isinstance(stat, dict) and isinstance(stat.get("max"), (int, float)):
            scale = 1000.0 if key == "statistics_seconds" else 1.0
            return f"{_L(lang, 'peak_prefix')} {_number(float(stat['max']) * scale)}{unit}"
    # Generic fallback: the scaled rule value.
    if isinstance(ev.get("rule_value"), (int, float)):
        return _number(ev["rule_value"])
    return "—"


def _finding_reco(sev: str, lang: str) -> str:
    return {
        "CRITICAL": _L(lang, "reco_critical"),
        "WARNING": _L(lang, "reco_warning"),
        "NOTICE": _L(lang, "reco_notice"),
    }.get(sev, _L(lang, "reco_review"))


def _sec_findings(num: str, payload: dict[str, Any], lang: str) -> list[str]:
    """§8 Findings & Prioritized Recommendations. Each finding = deterministic
    severity + area + quantified impact + a severity-based recommendation (ranked
    most-severe first); deeper root-cause / next-step narrative is the LLM slot."""
    candidates = payload.get("analysis_candidates") or []
    cur = _results(payload)
    rank = {"CRITICAL": 3, "WARNING": 2, "NOTICE": 1}
    out = [f"## {num}. {_L(lang, 'findings_reco')}", ""]
    if candidates:
        ordered = sorted(candidates, key=lambda c: -rank.get(str(c.get("status", "N/A")), 0))
        out += [f"| {_L(lang, 'status_col')} | {_L(lang, 'area')} | {_L(lang, 'impact_col')} | {_L(lang, 'recommendation')} |",
                "|---|---|---|---|"]
        for c in ordered:
            cid = str(c.get("check_id"))
            sev = str(c.get("status", "N/A"))
            out.append(f"| {sev} | {_finding_area(cid, lang)} | {_finding_impact(cur.get(cid) or {}, lang)} | {_finding_reco(sev, lang)} |")
    else:
        out.append(f"- {_L(lang, 'no_candidates')}")
    out += ["", f"### {_L(lang, 'key_risks')}", "", _interp(payload, "key_risks", lang), "",
            f"### {_L(lang, 'recommendations')}", "", _interp(payload, "recommendations", lang), ""]
    return out


_LOGICAL_COVERAGE_LABELS = {
    "SQL-01": {"zh": "SQL 吞吐（QPS）", "en": "SQL throughput (QPS)"},
    "RCU-01": {"zh": "RU 用量", "en": "RU consumption"},
    "SQL-02": {"zh": "查询延迟 P99", "en": "Query latency P99"},
    "SQL-03": {"zh": "查询延迟 P999", "en": "Query latency P999"},
    "SQL-05": {"zh": "失败 SQL 比率", "en": "SQL failure ratio"},
    "SQL-06": {"zh": "慢日志（总条数 / 唯一条数）", "en": "Slow log (total / unique)"},
    "SQL-10": {"zh": "SQL 综合评估", "en": "SQL overall"},
    "RCU-05": {"zh": "资源管控请求等待", "en": "Resource-control request wait"},
    "SQL-07": {"zh": "连接数", "en": "Connections"},
    "SQL-08": {"zh": "活动连接", "en": "Active connections"},
    "LST-01": {"zh": "存储用量", "en": "Storage usage"},
    "SQLD-01": {"zh": "RU Top-N", "en": "RU Top-N"},
    "SQLD-02": {"zh": "慢 SQL 次数 Top-N", "en": "Slow-SQL frequency Top-N"},
    "SQLD-03": {"zh": "慢 SQL 总耗时 Top-N", "en": "Slow-SQL total-duration Top-N"},
}


def _sec_coverage(num: str, results: dict[str, Any], lang: str, is_logical: bool) -> list[str]:
    """§9 Coverage (assessed / not assessed) + appendix scope note."""
    na = _L(lang, "na")
    if is_logical:
        pairs = [
            (label.get(lang) or label["en"], (results.get(cid) or {}).get("status", "N/A"))
            for cid, label in _LOGICAL_COVERAGE_LABELS.items()
        ]
    else:
        pairs = [
            (_ded_label(cid, lang), str((r or {}).get("status", "N/A")))
            for cid, r in results.items() if cid in DEDICATED_CHECK_LABELS
        ]
    assessed = sorted({name for name, st in pairs if st != "N/A"})
    missing = sorted({name for name, st in pairs if st == "N/A"})
    out = [f"## {num}. {_L(lang, 'coverage')}", "", f"### {_L(lang, 'assessed')}", ""]
    out += [f"- {item}" for item in assessed] or [f"- {na}"]
    out += ["", f"### {_L(lang, 'not_assessed')}", ""]
    out += [f"- {item}" for item in missing] or [f"- {na}"]
    out.append("")
    return out


# Draft section → canonical check IDs (only structurally-bound checks map today;
# unmapped sections render a narrative slot until their collectors are bound).
_DED_TIKV = ("TIKV-01", "TIKV-02", "TIKV-03", "TIKV-04", "TIKV-05", "TIKV-06", "TIKV-07", "TIKV-08", "TIKV-09", "TIKV-10")
_DED_OVERVIEW = ("OVR-01", "OVR-02")
_DED_PD = ("PD-03",)
_DED_INFRA = ("INF-03", "INF-04", "INF-05")
_DED_HA = ("AVL-02",)
_DED_RELIABILITY = ("AVL-01", "AVL-02", "AVL-08", "CDC-01", "CDC-02")


def _check_value(result: dict[str, Any], lang: str) -> str:
    """A customer-safe measured value for a bound full-stack check — so §4 shows
    data, not just a status. Never raw IDs/PromQL."""
    ev = result.get("evidence") or {}
    states = ev.get("changefeed_states")
    if isinstance(states, dict) and states:
        return f"{int(states.get('Working', 0) or 0)}/{int(states.get('Total', 0) or 0)}"
    for key in ("cpu_max_by_instance", "memory_max_by_instance", "tidb_cpu_max_by_instance", "store_usage_by_instance"):
        m = ev.get(key)
        vals = [float(v) for v in m.values() if isinstance(v, (int, float))] if isinstance(m, dict) else []
        if vals:
            return f"{_number(max(vals))}%"
    rss = ev.get("tidb_memory_max_by_instance")
    rss_vals = [float(v) for v in rss.values() if isinstance(v, (int, float))] if isinstance(rss, dict) else []
    if rss_vals:
        return f"{_number(max(rss_vals) / (1024 ** 3))} GiB"
    for key in ("grpc_qps_avg_by_instance", "region_by_instance", "restarts_by_instance"):
        m = ev.get(key)
        vals = [float(v) for v in m.values() if isinstance(v, (int, float))] if isinstance(m, dict) else []
        if vals:
            return _number(max(vals))
    peaks = (
        ("tikv_instance_count", lambda v: f"{_number(v, 0)} {_L(lang, 'inst_unit')}"),
        ("slow_score_peak", lambda v: f"{_L(lang, 'slow_score')} {_number(v, 0)}"),
        ("raft_log_lag_p99_peak", lambda v: f"{_number(v, 0)} ms"),
        ("cdc_checkpoint_lag_max", lambda v: f"{_number(v)} s"),
        ("disk_read_latency_peak", lambda v: f"{_number(float(v) * 1000)} ms"),
        ("iops_exceed_peak", lambda v: _number(v)),
        ("write_stall_peak", lambda v: _number(v)),
        ("store_usage_fast_growth", lambda v: str(v)),
    )
    for key, fmt in peaks:
        if isinstance(ev.get(key), (int, float)):
            return fmt(ev[key])
    return _finding_impact(result, lang)


def _ded_table(results: dict[str, Any], ids: tuple[str, ...], lang: str) -> list[str]:
    """§4 component table: area + status + measured value. If no check in the group
    is assessed, render a narrative slot instead of an empty table."""
    rank = {"CRITICAL": 3, "WARNING": 2, "NOTICE": 1, "HEALTHY": 0}
    rows = []
    for cid in ids:
        if cid not in DEDICATED_CHECK_LABELS:
            continue
        result = results.get(cid) or {}
        status = str(result.get("status", "N/A"))
        if status == "N/A":
            continue
        rows.append((_ded_label(cid, lang), status, _check_value(result, lang)))
    if not rows:
        return [f"_{_L(lang, 'pending_bind')}_", "", f"- {_L(lang, 'llm_slot')}", ""]
    rows.sort(key=lambda r: (-rank.get(r[1], 0), r[0]))
    out = [f"| {_L(lang, 'area')} | {_L(lang, 'value_col')} | {_L(lang, 'status_col')} |", "|---|---|---|"]
    out += [f"| {label} | {value} | {status} |" for label, status, value in rows]
    out.append("")
    return out




def validate_customer_report(report: str) -> None:
    leaked = [token for token in FORBIDDEN_CUSTOMER_TERMS if token in report]
    if leaked:
        raise ValueError(f"customer report leaked internal terminology: {leaked}")
    ids = _CHECK_ID_RE.findall(report)
    if ids:
        raise ValueError(f"customer report leaked internal check IDs: {sorted(set(ids))}")
    if _URL_RE.search(report):
        raise ValueError("customer report leaked a URL")
    lowered = report.lower()
    hints = [hint for hint in _PROMQL_INFRA_HINTS if hint in lowered]
    if hints:
        raise ValueError(f"customer report leaked internal query/infrastructure detail: {hints}")


def _cli_render(argv: list[str]) -> int:
    """Re-render a saved payload (no re-collection), optionally merging an
    interpretations sidecar into the LLM narrative slots. Usage:
        customer_report.py render <normalized.json> [interpretations.json] [--lang zh|en]
    The sidecar is a JSON object keyed by slot (exec_summary / key_risks /
    recommendations / exec_plan); each value is a string or {"zh":..,"en":..}."""
    import argparse
    parser = argparse.ArgumentParser(prog="customer_report.py render")
    parser.add_argument("payload")
    parser.add_argument("interpretations", nargs="?")
    parser.add_argument("--lang", choices=("zh", "en"))
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args(argv)
    with open(args.payload, encoding="utf-8") as fh:
        payload = json.load(fh)
    if args.lang:
        payload["lang"] = args.lang
    if args.interpretations:
        with open(args.interpretations, encoding="utf-8") as fh:
            merged = dict(payload.get("interpretations") or {})
            merged.update(json.load(fh))
            payload["interpretations"] = merged
    report = render_health_report(payload)
    if not args.no_validate:
        validate_customer_report(report)
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "render":
        raise SystemExit(_cli_render(sys.argv[2:]))
    sys.stderr.write("usage: customer_report.py render <normalized.json> [interpretations.json] [--lang zh|en]\n")
    raise SystemExit(2)
