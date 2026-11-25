# Core Rule Catalog

This catalog explains check intent; it is not the runtime source of truth and does not define universal thresholds. The complete per-check Plan applicability contract lives in `../config/checklist.v1.json`. `plan-checklist-matrix.md` is generated from that registry for human review.

## Default Health: all Plans when tenant-safe

### Cluster overview

- metadata and product status;
- provider, region, version, and current product-exposed scale;
- observation window and source coverage.

### SQL workload summary

- QPS Max/Avg and comparable-period change;
- RU/RCU Max/Avg, configuration/headroom, and queueing where exposed;
- P99/P999 Max/Avg with traffic/sample guards;
- failed SQL aggregate count/rate and safe categories;
- slow-log aggregate count and change, never treated as business request volume;
- connections and active sessions;
- product-exposed logical storage start/end/growth;
- anomaly signals and `deep_dive_recommended` reasons.

Default Health does not emit SQL Top-N rows, SQL text, digest lists, or execution users.

## Dedicated and BYOC full-stack additions

After route and source validation, eligible categories include:

- TiDB/TiKV/PD CPU and memory against actual limits;
- node, disk, network, and file-descriptor pressure;
- write stall, flow control, TSO/PD and TiKV gRPC latency;
- Region/leader balance, compaction, L0 files, GC, capacity, pending peers, and scheduler context;
- restart, backoff, and error ratios with reason-label validation;
- TiFlash, TiCDC, backup, PITR, and changefeed health;
- security quick scan within granted permissions.

Shared Plans must not inherit these physical-layer additions.

## SQL component deep dive recommendations

When explicitly requested and supported:

- RU Top-N;
- slow-frequency Top-N;
- total-duration Top-N;
- average latency;
- execution count;
- processed/scanned keys;
- full digest;
- normalized SQL text;
- execution users.

Top-N rank is diagnostic evidence and never determines Health by itself. Processed/scanned keys are not physical I/O. Missing fields are N/A. Apply audience-based redaction.
