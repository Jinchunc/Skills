# Skills

A monorepo of Claude Code skills. Each skill lives in its own top-level
directory and is self-contained (its own `SKILL.md`, config, scripts and tests).

## Skills

| Skill | Description |
|---|---|
| [`tidb-cloud-health-inspection/`](tidb-cloud-health-inspection/) | TiDB Cloud / Dedicated / BYOC cluster health inspection — tenant-safe signal collection, basic-report checklist, and handoff to deep o11y analysis. |

## Adding a skill

Add a new top-level directory `<skill-name>/` containing the skill's own
`SKILL.md` and files, then add a row to the table above.

## Conventions

- Secrets (`.env`, API keys, credentials) are never committed — see the root
  `.gitignore`. Each skill ships a non-secret `config/env.example` where relevant.
- Run artifacts (`output/`, `*.log`) and local caches are git-ignored.
