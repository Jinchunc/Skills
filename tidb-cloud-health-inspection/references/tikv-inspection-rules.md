# Dedicated and BYOC Inspection Rules

## Scoring Semantics

This file uses the **sustained-window trend** interpretation: a rule warns only
when a metric stays beyond its line for a defined persistence window (e.g.,
`>= 30m`), reflecting trend and durability rather than a single instant.

The threshold numbers below are the sustained-window interpretation of the
values centralized in `../config/rules.v1.json`, which is the runtime source of
truth. Where a line references a specific rule, the number must match that rule;
the two differ only in the persistence-window semantics.

## Purpose

This file turns the Dedicated and BYOC TiKV inspection procedure and the Feishu template
into a fixed, reusable check list.

The automated report is aligned to the template sections and follows these
principles:

- TiKV metric review targets the `tikv-details` monitoring object
- node-level disk review targets `dedicated-dataplane-node-info`
- the report header should expose direct deep links for both dashboards
- disk review only covers TiKV nodes and the TiKV data disks selected by topology
- default log review is metrics-first and text-first; screenshots are out of scope
- the local template hints are persisted in `references/feishu-template-hints.md`

## Template-Aligned Sections

The generated report should keep this section order:

1. `Summary`
2. `Key Findings`
3. `Error Signals`
4. `gRPC QPS and Latency`
5. `CPU and Memory`
6. `Critical Thread Pool CPU`
7. `Disk Usage and Region Count`
8. `Disk Load`
9. `ERROR/WARN Logs`
10. `Follow-up Investigation`

Each section should contain:

- the key raw metrics
- one short English summary sentence
- explicit `Unknown` notes when the required signal is unavailable
- a top-level header summary that states whether the cluster is healthy, needs watching, or should be checked immediately

## Rule Table

| Rule ID | Area | Metric or Logic | Threshold | Severity | Source Type | Source |
|---|---|---|---|---|---|---|
| E1 | Critical Error | `increase(tikv_critical_error_total[window])` | `0` | Error | documented | TiKV inspect SOP / template |
| E2 | Server is busy | `increase(tikv_channel_full_total[window])` | `0` | Warning | documented | template / TiKV errors |
| E3 | Server is busy | `increase(tikv_scheduler_too_busy_total[window])` | `0` | Warning | documented | TiKV metrics reference |
| E4 | Server is busy | `increase(tikv_coprocessor_request_error{type="full"}[window])` | `0` | Warning | documented | TiKV metrics reference |
| E5 | Server report failures | `increase(tikv_server_report_failure_msg_total[window])` | `0` | Warning | documented | template / TiKV errors |
| G1 | gRPC message rate / QPS | `rate(tikv_grpc_msg_duration_seconds_count[window])` by `type` | observation | Observation | documented | `tikv-details` gRPC panels |
| G2 | gRPC message failed | `sum(increase(tikv_grpc_msg_fail_total[30m])) by (type)` | warn only when a 30-minute rolling window reaches a high failed-message count; current heuristic uses `>= 100` and prints the exact time window | Warning | heuristic | aligned to reviewer feedback |
| G3 | gRPC message duration P99 | `histogram_quantile(0.99, rate(tikv_grpc_msg_duration_seconds_bucket[5m]))` for `kv_prewrite` / `kv_commit` | warn only when P99 stays `>= 1000ms` for `>= 15m`; print the exact time window | Warning | heuristic | aligned to reviewer feedback |
| G4 | Average gRPC message duration | `increase(sum) / increase(count)` by `type` | observation | Observation | documented | `tikv-details` gRPC panels |
| G5 | Read-path gRPC message duration P99 | `histogram_quantile(0.99, rate(tikv_grpc_msg_duration_seconds_bucket[5m]))` for `coprocessor` / `kv_get` / `kv_batch_get` | observation only in the selected inspection window; use it to correlate read-path tails with Unified Read Pool pressure | Observation | documented+heuristic | reviewer feedback |
| B1 | TiKV CPU | `process_cpu_seconds_total / tikv_server_cpu_cores_quota` | warn only when a TiKV node stays `>= 85%` for `>= 30m`; print the exact time window (baseline `85%`, from `tikv_cpu_sustained.sustain_warn_pct` in `../config/rules.v1.json`) | Warning | heuristic | aligned to reviewer feedback |
| B2 | TiKV memory | `process_resident_memory_bytes / topology.memory_gi` | watch line `>= 85%`; mark `Error` only when a TiKV node stays `>= 90%` for `>= 30m` (baseline `85%`/`90%`, from `tikv_memory_pct` warning/critical in `../config/rules.v1.json`) | Warning/Error | heuristic | aligned to reviewer feedback |
| B3 | Memory-leak watch | compare current-window `avg_over_time(process_resident_memory_bytes / topology.memory_gi)` with the previous `7d` average by TiKV instance | mark `Warning` when the current-window average exceeds the previous-7-day average by `>= 5` percentage points, **unless** the same TiKV instance also shows strong same-window growth versus its previous-7-day baseline in Region count (`>= 100` and `>= 10%`), gRPC workload QPS (`>= 100` and `>= 20%`), or CPU (`>= 5` percentage points); treat `>= 3` percentage points as observation-only rising trend | Warning | heuristic | aligned to reviewer guidance |
| B4 | Restart | `changes(process_start_time_seconds{component="tikv"}[window])` | `0` | Warning | documented | TiKV inspect SOP |
| T1 | Thread pool CPU | grouped `tikv_thread_cpu_seconds_total` peaks vs thread-count threshold | interpret the panel per TiKV instance, not as a cluster-wide sum; prioritize `Unified read pool CPU`; warn when one TiKV node stays `>= 80%` while peers stay `<= 50%`, which implies a likely hotspot | Warning | documented+heuristic | template + SOP |
| T2 | Unified Read Pool running tasks | `tikv_unified_read_pool_running_tasks{name="unified-read-pool"}` | observation; rising running-task pressure is useful supporting evidence for localized read-pool saturation or queueing | Observation | documented+heuristic | reviewer feedback |
| T3 | Unified Read Pool time used total | `increase(multilevel_level_elapsed{name="unified-read-pool", level="total"}[window])` | observation; use it as supporting evidence together with Unified Read Pool CPU and running tasks | Observation | documented+heuristic | reviewer feedback |
| C1 | Store usage | `tikv_store_size_bytes{type="used|available"}` | do not warn on high but stable usage alone; warn only when usage reaches `>= 80%` and rises by `>= 10%` within `30m` | Warning | heuristic | aligned to reviewer feedback |
| C2 | Region / Leader balance | `tikv_raftstore_region_count{type="region|leader"}` | even distribution required | Observation | documented | TiKV inspect SOP |
| D1 | Raft Engine P99 | `tikv_raftstore_append_log_duration_seconds_bucket`, `commit_log`, `apply_log` | observation | Observation | documented | template / raft engine panels |
| D2 | Write stall | `tikv_engine_write_stall` | `0` | Warning | documented | SOP / TiKV storage docs |
| D3 | Compaction pending bytes | `tikv_engine_pending_compaction_bytes` | reference line `256 GiB` (`tikv_compaction_pending.reference_bytes` in `../config/rules.v1.json`); warn when pending bytes stay `>= 200 GiB` for `>= 30m`, or when the peak reaches `>= 512 GiB` | Warning | documented+heuristic | aligned to reviewer feedback |
| D4 | Disk util | joined `node_disk_io_time_seconds_total` | record as observation only; ignore util-only warnings in automated pass/fail | Observation | heuristic | reviewer feedback |
| D5 | Disk queue | joined `node_disk_io_time_weighted_seconds_total` | record as observation only; ignore queue-only warnings in automated pass/fail | Observation | heuristic | reviewer feedback |
| D6 | Disk read latency | joined `node_disk_read_time_seconds_total / node_disk_reads_completed_total` | `> 20ms` warn | Warning | heuristic | operational heuristic |
| D7 | Disk write latency | joined `node_disk_write_time_seconds_total / node_disk_writes_completed_total` | `> 20ms` warn | Warning | heuristic | operational heuristic |
| D8 | Disk IOPS ceiling | `aws_ebs_csi_exceeded_iops_seconds_total` joined to TiKV pods | warn only when the peak exceeded-IOPS signal reaches `>= 1.0`; ignore tiny residual values such as `0.00`, `0.03`, or `0.08` | Warning | documented+heuristic | clinic-api metrics docs + validation feedback |
| D9 | MVCC observation | `tikv_storage_mvcc_versions_bucket`, `tikv_storage_mvcc_gc_delete_versions_bucket` | observation | Observation | documented | TiKV metrics reference |
| F1 | Flow control throttle duration | `increase(tikv_scheduler_throttle_duration_seconds_sum[window])` | observation; if it rises together with write-side latency or `ServerIsBusy`, treat flow control as active and keep the metric under watch | Observation | documented+heuristic | reviewer feedback |
| F2 | Flow control discard ratio | `max_over_time((tikv_scheduler_discard_ratio)[window:5m])` | warning when the peak ratio is `> 0`; correlate it with compaction pending bytes and write pressure | Warning | documented+heuristic | reviewer feedback |
| P1 | TiKV slow score | `max_over_time((tikv_raftstore_slow_score)[window:5m])` | observation; if the score reaches `100`, treat the node as high risk and correlate with PD slow-store handling | Observation | documented+heuristic | reviewer feedback |
| R1 | Raft log lag P99 | `histogram_quantile(0.99, rate(tikv_raftstore_log_lag_bucket[5m]))` by `instance` | observation in the selected inspection window; use the TiKV alert reference line `> 5000` as a strong watch signal | Observation | documented+heuristic | reviewer feedback |
| L1 | Error / fatal logs | Loki query on `container="tikv"` only | no unexpected `ERROR` / `FATAL` lines | Warning | documented | internal report template |

## gRPC Scope

Default gRPC review focuses on these request types because they are the most
useful health signals in `tikv-details`:

- write path: `kv_prewrite`, `kv_commit`
- read path: `coprocessor`, `kv_get`, `kv_batch_get`

The report should show for each type:

- total message count within the window
- average QPS within the window
- average message duration within the window
- P99 message duration peak within the window

## Thread Pool Grouping

Use these regex groups on `tikv_thread_cpu_seconds_total{name=...}`:

| Group | Dashboard wording | Regex | Factor |
|---|---|---|---|
| `raft_store` | Raft store CPU | `^raftstore_.*$` | `0.8` |
| `async_apply` | Async apply CPU | `^apply(_low)?_\\d+$` | `0.7` |
| `raft_async_writer` | Raft async writer CPU | `^store_writer_\\d+$` | `0.8` |
| `grpc_poll` | gRPC poll CPU | `^grpc_server_\\d+$` | `0.8` |
| `scheduler_pool` | Scheduler pool CPU | `^sched_worker_(pool|priority|high)_\\d+$` | `0.7` |
| `unified_read_pool` | Unified read pool CPU | `^unified_read_pool_\\d+$` | `0.7` |

Interpretation:

- `thread_count` is the number of unique thread names matched by the regex
- `peak_cpu_cores` is the peak sum of `rate(tikv_thread_cpu_seconds_total[5m])`
- warn when `peak_cpu_cores > thread_count * factor`

## Disk Device Scope

Disk review only targets TiKV nodes in the inspected cluster namespace.

Use `components.COMPONENT_TYPE_TIKV.storageType` from Clinic cluster detail to
choose the TiKV data disks:

| TiKV storage type | Focus devices |
|---|---|
| `Plus` | `nvme2n1` |
| `Standard` | `nvme2n1`, `nvme3n1` |
| `Performance` | `nvme2n1`, `nvme3n1` |

Interpretation rules:

- `Disk I/O Util`, queue length, and wait latency use the worst selected TiKV data disk per pod
- `IOPS` and `Bandwidth` aggregate only the selected TiKV data disks per pod
- if storage type is unavailable, fall back to the broad device matcher and mark the scope as heuristic

## Log Scope

The automated log section is intentionally conservative:

- default filter scope: `tikv`
- default severity scope: `ERROR|FATAL`
- known noisy patterns are excluded by default
- exclude expected noisy line tokens such as `KV:Raft:StepPeerNotFound`, `no valid key found for split.`, `not_leader {`, `stale command`, `region_not_found {`, `epoch_not_match {`, `cdc register for a not found region`, and known CDC initializer cancellation messages like `oneshot canceled` or `scan canceled`
- exclude generic CDC initializer noise by default with the token `cdc initialize fail`; if a reviewer needs CDC-specific debugging, they should drill down manually outside the selected inspection-window log scope

`WARN` remains a template heading requirement, but the default automated report
does not attempt to summarize all `WARN` lines because the noise level is too
high for a stable inspection-window pass/fail rule. When the metrics section already shows
an abnormal signal, the reviewer can drill down from the same log scope.
