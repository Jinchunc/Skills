# Plan and Data Scope

## Canonical Plan

Use one routing field: `plan`. Allowed values are Starter, Essential, Premium, Dedicated, BYOC, and Unknown. Preserve raw deployment metadata only as internal `sourceMetadata`.

## Starter, Essential, and Premium

These Plans share one logical-cluster safety boundary. Availability differs by Plan, version, cluster state, and permission; capability detection is required.

### Allowed default Health evidence

- logical-cluster metadata, status, provider, region, version, and current RCU configuration where exposed;
- tenant QPS and RU/RCU consumption;
- tenant query P99/P999;
- failed SQL count/rate and safe error categories;
- slow-log aggregate count and trend, cross-checked with true QPS;
- connections and active sessions;
- product-exposed logical storage start/end/growth;
- tenant resource-control queueing or headroom;
- authorized backup, PITR, changefeed, connectivity, encryption, or feature state;
- current and previous comparable-window values with validity metadata.

### Excluded from logical-cluster Health

- shared TiKV and PD process metrics;
- node CPU, memory, disk, network, and file descriptors;
- physical store capacity, throughput, IOPS, and latency;
- Region, Peer, leader, scheduler, Raft, compaction, L0, and physical GC evidence;
- shared physical topology and pool logs.

Do not query excluded evidence merely because a route exists. Pool evidence may be retained only in a separately authorized internal platform workflow and is never attributed to one logical cluster.

## SQL output boundary

Default Health outputs workload summaries and anomaly signals only. It excludes Top-N rows, SQL text, digest lists, and execution users.

An explicitly requested SQL component deep dive may recommend RU Top-N, slow-frequency Top-N, total-duration Top-N, average latency, execution count, processed/scanned keys, full digest, normalized SQL text, and execution users. These are optional evidence fields, not Health scoring rules. Unsupported fields are N/A; digests remain complete; sensitive text and users are redacted for the audience.

## Dedicated and BYOC

Full-stack checks may include tenant SQL, TiDB, TiKV, PD, TiFlash, TiCDC, topology, nodes, cloud storage, Regions, Peers, scheduling, and logs. Enable each rule only after validating route, ownership, labels, permissions, version, unit, and source semantics.

## Unknown and failure behavior

Unknown Plan, ownership, or scope uses default-deny. Missing route, unavailable source, permission failure, and malformed data produce non-zero failure or N/A with a reason; empty data is not zero.
