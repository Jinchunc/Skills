# Automation and Cron Architecture

Use one deterministic Python engine for on-demand and scheduled execution. On-demand invokes it directly; Cron invokes the same engine and applies the headless gate.

Python owns discovery, Plan normalization, applicability and scope checks, collection, schema/unit normalization, data-quality guards, deterministic evaluation, state, idempotency, evidence, and report rendering.

Severity is not an LLM gate. Set `llm_required=true` only for an explicit interpretation request, conflicting evidence that cannot be resolved deterministically, contextual recommendations, or an explicitly requested narrative. This Skill does not provide incident analysis or RCA.

Default Health SQL collection produces aggregate workload summaries and anomaly signals only. It does not automatically run SQL Top-N deep dives.

Exactly one layer owns delivery:

- `python`: deterministic result can be delivered without interpretation;
- `agent`: Python explicitly deferred interpretation and emitted reasons;
- `none`: no user-visible message.

Collection failures, malformed output, missing route, unsafe scope, and permission failures are non-zero or N/A, never Healthy.
