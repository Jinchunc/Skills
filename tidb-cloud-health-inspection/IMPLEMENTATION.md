# Implementation Bundle (retired)

This file is kept only as a signpost. It no longer describes the skill.

The old implementation notes here documented a dual Python + Shell-collector
architecture that has since been superseded by a single Python entry point. The
Shell collectors and the standalone TiFlash collector have been removed, so the
former contents referred to files that no longer exist.

There is now one authority for each concern:

- **How to use it** — [`SKILL.md`](SKILL.md) (what it does, how to run, troubleshooting).
- **How it is designed** — [`README.md`](README.md) (architecture, the three
  sources of truth, plan routing, evaluation, gate, reports/privacy, running &
  tests, roadmap).
- **What is checked / how / thresholds** — `config/checklist.v1.json`,
  `config/collection.v1.json`, `config/rules.v1.json`.

Nothing references this file at runtime; it can be deleted.
