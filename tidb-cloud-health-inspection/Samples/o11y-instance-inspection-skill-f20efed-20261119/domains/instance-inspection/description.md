# Domain 3: 实例巡检 (Instance Inspection)

> Status: **Covered** (TiDB only)
> Tier: C (Engine-strong)

## Core

全栈健康检查，定期报告，4 级评分。

## Scenarios

- 自动化巡检实例配置
- 性能基线检查
- 安全策略快速扫描（深度审计 → 域7）
- 备份状态检查
- 版本漏洞/配置基线检查
- 生成巡检报告并推送整改建议

## Engine Skills

| Engine | Skill | Coverage |
|--------|-------|----------|
| TiDB | `engines/tidb/instance-inspection` (inspection checklist, 30 check items, 4-level scoring) | Full |
| MySQL | — | Not yet built |
| PostgreSQL | — | Not yet built |
| MongoDB | — | Not yet built |

## Routing Note

- 安全项作为巡检子项快速扫描 → 本域
- 独立触发的深度合规审计 → 域7 (compliance-audit)
