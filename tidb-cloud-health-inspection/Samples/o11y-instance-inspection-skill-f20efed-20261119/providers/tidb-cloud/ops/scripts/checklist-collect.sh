#!/usr/bin/env bash
# checklist-collect.sh — deterministic collector + 4-level scorer for the
# instance-inspection checklist (engines/tidb/instance-inspection/checklist.md).
#
# Collects the checklist items that are pure "fixed PromQL + numeric
# threshold" and scores them per the doc. Interpretation, recommendations,
# and the report narrative stay with the agent (report-template.md).
#
# Tier awareness (from resolve-cluster-routing.sh):
#   dedicated : all items on the cluster ID
#   pooled    : SQL-layer items on the logical ID; TiKV/PD/storage items on
#               the POOL ID, marked "(pool-shared)" — they aggregate every
#               keyspace and must not be attributed to this cluster alone
#   regional  : SQL-layer items only; TiKV/PD/storage items are N/A by
#               design (cluster route would return cross-tenant noise)
#
# Not collected (by design; see foot of report): C1 metadata detail (shown
# in header), P7 slow-query count (data-proxy SQL), P9 baselines, S5 GC
# safepoint (unit varies), S6 slope, A4 operator judgment, O1-O3 SQL tops,
# R6 node disk IO (DataPlane coverage varies).
#
# Usage: CLINIC_API_KEY=... bash checklist-collect.sh <CLUSTER_ID>

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
EP="${CLINIC_ENDPOINT:-https://clinic.pingcap.com}"
TOKEN="${CLINIC_API_KEY:-${CLINIC_API_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "ERROR: set CLINIC_API_KEY or CLINIC_API_TOKEN" >&2; exit 2; }
CID="${1:-}"; [ -n "$CID" ] || { echo "usage: checklist-collect.sh <CLUSTER_ID>" >&2; exit 2; }

R=$(bash "${SCRIPT_DIR}/resolve-cluster-routing.sh" "$CID") || exit 3
CLASS=$(echo "$R" | jq -r .routingClass); TIER=$(echo "$R" | jq -r .tier)
ORG=$(echo "$R" | jq -r .orgId); POOL=$(echo "$R" | jq -r '.poolId // empty')
NAME=$(echo "$R" | jq -r '.name // "?"')

api() { curl -s --max-time 30 -H "Authorization: Bearer ${TOKEN}" "$@"; }
q() { # target-cluster-id promql -> first scalar or ""
  api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/$1/metrics/query" --data-urlencode "query=$2" \
    | jq -r '.data.result[0].value[1] // empty' 2>/dev/null
}
qn() { # target promql -> result count
  api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/$1/metrics/query" --data-urlencode "query=$2" \
    | jq -r '.data.result | length' 2>/dev/null
}

# score4 <value> <t1> <t2> <t3> [asc|desc]  — asc: bigger is worse (default)
score4() {
  local v="$1" t1="$2" t2="$3" t3="$4" dir="${5:-asc}"
  [ -z "$v" ] && { echo "N/A"; return; }
  awk -v v="$v" -v a="$t1" -v b="$t2" -v c="$t3" -v d="$dir" 'BEGIN{
    if (v!=v+0 && v!="0") { print "N/A"; exit }
    if (d=="asc") { if (v<a) print "HEALTHY"; else if (v<b) print "NOTICE"; else if (v<c) print "WARNING"; else print "CRITICAL" }
    else          { if (v>a) print "HEALTHY"; else if (v>b) print "NOTICE"; else if (v>c) print "WARNING"; else print "CRITICAL" }
  }'
}
fmt() { [ -z "${1:-}" ] && echo "-" || awk -v v="$1" -v p="${2:-2}" 'BEGIN{printf "%.*f", p, v}'; }

ROWS=""
row() { # id item value score note
  ROWS="${ROWS}| $1 | $2 | $3 | **$4** | $5 |\n"
}
na_row() { row "$1" "$2" "-" "N/A" "$3"; }

# scope targets
SQLT="$CID"                      # SQL-layer target
case "$CLASS" in
  dedicated) KVT="$CID"; KVNOTE="" ;;
  pooled)    KVT="$POOL"; KVNOTE="(pool-shared — all keyspaces)" ;;
  regional)  KVT="";      KVNOTE="" ;;
esac

echo "# Inspection collection — ${NAME} (${CID})"
echo
echo "Tier: **${TIER}** · routing class: ${CLASS}${POOL:+ · pool: ${POOL}} · window: 5m rates at $(date -u '+%Y-%m-%d %H:%MZ')"
echo

## Report-template inputs (header fields + §五 非默认参数) -------------
echo "## Report-template inputs"
echo
GCL=$(q "$SQLT" 'max(tidb_tikvclient_gc_config{type="tikv_gc_life_time"})')
GCI=$(q "$SQLT" 'max(tidb_tikvclient_gc_config{type="tikv_gc_run_interval"})')
if [ -n "$GCL" ]; then
  echo "- GC life time: $(awk -v v="$GCL" 'BEGIN{ if (v>=3600) printf "%gh", v/3600; else printf "%gm", v/60 }') · GC run interval: $(awk -v v="${GCI:-0}" 'BEGIN{printf "%gm", v/60}') (from tidb_tikvclient_gc_config, max across instances)"
else
  echo "- GC life time: N/A (tidb_tikvclient_gc_config absent — idle cluster or metric not exported)"
fi
echo
echo "Key config snapshot (numeric configs exported as metrics; a FULL non-default diff still needs SHOW CONFIG / TiUP — mark the rest N/A in the report, do not infer):"
echo
echo "| Config | Value |"
echo "|---|---|"
cfgrow() { local v; v=$(q "$2" "$3"); [ -n "$v" ] && printf '| %s | %s |\n' "$1" "$4" | sed "s/VAL/$(awk -v x="$v" -v m="${5:-1}" -v p="${6:-0}" 'BEGIN{printf "%.*f", p, x/m}')/" ; }
if [ -n "$KVT" ]; then
  cfgrow "tikv block_cache_size ${KVNOTE}" "$KVT" 'max(tikv_config_rocksdb{name="block_cache_size", cf="default"})' "VAL GiB" 1073741824 1
  cfgrow "tikv disable_write_stall ${KVNOTE}" "$KVT" 'max(tikv_config_rocksdb{name="disable_write_stall", cf="default"})' "VAL"
  cfgrow "tikv level0_slowdown/stop triggers ${KVNOTE}" "$KVT" 'max(tikv_config_rocksdb{name="level0_slowdown_writes_trigger", cf="default"})' "VAL (slowdown)"
  cfgrow "tikv apply_pool_size ${KVNOTE}" "$KVT" 'max(tikv_config_raftstore{name="apply_pool_size"})' "VAL"
fi
cfgrow "tidb token-limit" "$SQLT" 'max(tidb_config_status{type="token-limit"})' "VAL"
cfgrow "tidb max_connections (0=unlimited)" "$SQLT" 'max(tidb_config_status{type="max_connections"})' "VAL"
echo

## 1. Cluster overview -------------------------------------------------
UP=$(q "$SQLT" 'min(time() - process_start_time_seconds)')
if [ -n "$UP" ]; then
  UPH=$(awk -v v="$UP" 'BEGIN{printf "%.1f", v/3600}')
  SC=$(awk -v v="$UP" 'BEGIN{ if (v<3600) print "WARNING"; else if (v<86400) print "NOTICE"; else print "HEALTHY"}')
  row C2 "Min instance uptime" "${UPH}h" "$SC" "uptime<1h=WARNING, <24h=NOTICE"
else na_row C2 "Min instance uptime" "no process metrics in scope"; fi
if [ -n "$KVT" ]; then
  NV=$(q "$KVT" 'count(count by (version) (tikv_server_info))')
  [ -n "$NV" ] && row C3 "TiKV version consistency" "${NV} distinct" "$( [ "${NV%.*}" -le 1 ] && echo HEALTHY || echo WARNING)" "$KVNOTE" \
               || na_row C3 "TiKV version consistency" "metric unavailable"
else na_row C3 "TiKV version consistency" "regional plane: pool-level only"; fi

## 2. Resource health --------------------------------------------------
CPU_T=$(q "$SQLT" 'max by () (sum by (instance) (rate(process_cpu_seconds_total{component="tidb"}[5m])))')
[ -z "$CPU_T" ] && CPU_T=$(q "$SQLT" 'max by () (sum by (instance) (rate(process_cpu_seconds_total{job=~"tidb.*"}[5m])))')
[ -n "$CPU_T" ] && row R1 "TiDB CPU (max instance, cores)" "$(fmt "$CPU_T")" "-" "utilization needs node spec; raw cores reported" \
                || na_row R1 "TiDB CPU" "no series"
if [ -n "$KVT" ]; then
  CPU_K=$(q "$KVT" 'max by () (sum by (instance) (rate(process_cpu_seconds_total{component="tikv"}[5m])))')
  [ -z "$CPU_K" ] && CPU_K=$(q "$KVT" 'max by () (sum by (instance) (rate(process_cpu_seconds_total{job=~"tikv.*"}[5m])))')
  [ -n "$CPU_K" ] && row R2 "TiKV CPU (max instance, cores)" "$(fmt "$CPU_K")" "-" "$KVNOTE" || na_row R2 "TiKV CPU" "no series $KVNOTE"
  CPU_P=$(q "$KVT" 'max by () (sum by (instance) (rate(process_cpu_seconds_total{component="pd"}[5m])))')
  [ -z "$CPU_P" ] && CPU_P=$(q "$KVT" 'max by () (sum by (instance) (rate(process_cpu_seconds_total{job=~"pd.*"}[5m])))')
  [ -n "$CPU_P" ] && row R3 "PD CPU (max instance, cores)" "$(fmt "$CPU_P")" "-" "$KVNOTE" || na_row R3 "PD CPU" "no series $KVNOTE"
  MEM=$(q "$KVT" 'max(process_resident_memory_bytes{component="tikv"})')
  [ -z "$MEM" ] && MEM=$(q "$KVT" 'max(process_resident_memory_bytes{job=~"tikv.*"})')
  [ -n "$MEM" ] && row R4 "TiKV RSS (max instance)" "$(awk -v v="$MEM" 'BEGIN{printf "%.1fGiB", v/1073741824}')" "-" "score vs limit needs node spec $KVNOTE" \
                || na_row R4 "TiKV RSS" "no series"
  DU=$(q "$KVT" 'max(tikv_store_size_bytes{type="used"} / on(instance) tikv_store_size_bytes{type="capacity"})')
  if [ -n "$DU" ]; then
    PCT=$(awk -v v="$DU" 'BEGIN{printf "%.0f", v*100}')
    row R5 "Disk used% (max instance)" "${PCT}%" "$(score4 "$PCT" 60 80 90)" "$KVNOTE"
  else na_row R5 "Disk used%" "no series"; fi
else
  na_row R2 "TiKV/PD CPU, RSS, disk" "regional plane: cluster route is cross-tenant — use serverless-parent metrics at pool level only"
fi

## 3. Performance health -----------------------------------------------
P99=$(q "$SQLT" 'histogram_quantile(0.99, sum(rate(tidb_server_handle_query_duration_seconds_bucket[5m])) by (le))')
if [ -n "$P99" ] && [ "$P99" != "NaN" ]; then
  MS=$(awk -v v="$P99" 'BEGIN{printf "%.1f", v*1000}')
  row P1 "Query P99" "${MS}ms" "$(score4 "$MS" 50 200 1000)" "workload-dependent baseline"
else na_row P1 "Query P99" "no traffic in window"; fi
P999=$(q "$SQLT" 'histogram_quantile(0.999, sum(rate(tidb_server_handle_query_duration_seconds_bucket[5m])) by (le))')
if [ -n "$P999" ] && [ "$P999" != "NaN" ]; then
  MS9=$(awk -v v="$P999" 'BEGIN{printf "%.1f", v*1000}')
  row P2 "Query P999" "${MS9}ms" "$(score4 "$MS9" 200 1000 5000)" ""
  if [ -n "${MS:-}" ]; then
    RATIO=$(awk -v a="$MS9" -v b="$MS" 'BEGIN{ if (b>0) printf "%.1f", a/b; else print ""}')
    [ -n "$RATIO" ] && row P3 "P999/P99 spread" "$RATIO" "$(score4 "$RATIO" 3 10 50)" "tail-latency shape"
  fi
fi
# P4: CV over 30min range
NOW=$(date +%s)
CV=$(api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/${SQLT}/metrics/query-range" \
  --data-urlencode 'query=sum(rate(tidb_server_query_total[5m]))' \
  --data-urlencode "start=$((NOW-1800))" --data-urlencode "end=${NOW}" --data-urlencode "step=60" \
  | jq -r '[.data.result[0].values[]?[1] | tonumber] | if length>3 then (add/length) as $m | (map(($m - .)*($m - .)) | add/length | sqrt) / (if $m>0 then $m else 1 end) else empty end' 2>/dev/null)
[ -n "$CV" ] && row P4 "QPS stability (CV, 30m)" "$(fmt "$CV")" "$(score4 "$CV" 0.2 0.5 1.0)" "" || na_row P4 "QPS stability" "insufficient points"
if [ -n "$KVT" ]; then
  WS=$(q "$KVT" 'max(sum by (instance) (rate(tikv_engine_write_stall_duration_seconds{db="kv"}[5m])))')
  [ -n "$WS" ] && row P5 "Write stall ratio (max)" "$(fmt "$WS" 4)" "$(awk -v v="$WS" 'BEGIN{if(v==0)print "HEALTHY";else if(v<=0.01)print "NOTICE";else if(v<=0.05)print "WARNING";else print "CRITICAL"}')" "$KVNOTE" \
               || na_row P5 "Write stall" "no series"
  FC=$(q "$KVT" 'sum(rate(tikv_scheduler_throttle_action_total[5m]))')
  [ -n "$FC" ] && row P6 "Flow control actions/s" "$(fmt "$FC")" "$(awk -v v="$FC" 'BEGIN{if(v==0)print "HEALTHY";else if(v<1)print "NOTICE";else if(v<10)print "WARNING";else print "CRITICAL"}')" "$KVNOTE" \
               || na_row P6 "Flow control" "no series"
fi
TSO=$(q "$SQLT" 'histogram_quantile(0.99, sum(rate(tidb_tikvclient_ts_future_wait_seconds_bucket[5m])) by (le))')
if [ -n "$TSO" ] && [ "$TSO" != "NaN" ]; then
  TMS=$(awk -v v="$TSO" 'BEGIN{printf "%.1f", v*1000}')
  row P8 "TSO wait P99" "${TMS}ms" "$(score4 "$TMS" 5 20 100)" ""
else na_row P8 "TSO wait P99" "no traffic"; fi
ERR=$(q "$SQLT" 'sum(rate(tidb_server_execute_error_total[2m])) / sum(rate(tidb_server_query_total[2m]))')
if [ -n "$ERR" ] && [ "$ERR" != "NaN" ]; then
  EP100=$(awk -v v="$ERR" 'BEGIN{printf "%.3f", v*100}')
  row P11 "Failed query ratio" "${EP100}%" "$(score4 "$EP100" 0.1 1 2)" ""
else na_row P11 "Failed query ratio" "no traffic"; fi
CONN=$(q "$SQLT" 'sum(tidb_server_connections)')
[ -n "$CONN" ] && row P13 "Connections" "${CONN%.*}" "-" "baseline-relative; report only" || na_row P13 "Connections" "no series"
CUR=$(q "$SQLT" 'sum(rate(tidb_executor_statement_total[2m]))')
BASE=$(q "$SQLT" 'sum(rate(tidb_executor_statement_total[2m] offset 1d))')
if [ -n "$CUR" ] && [ -n "$BASE" ]; then
  RB=$(awk -v c="$CUR" -v b="$BASE" 'BEGIN{ if (b>0) printf "%.2f", c/b; else print "" }')
  if [ -n "$RB" ]; then
    SC=$(awk -v r="$RB" 'BEGIN{ if (r<0.3||r>4) print "CRITICAL"; else if (r<=0.5||r>=2.5) print "WARNING"; else if (r<=0.7||r>=1.5) print "NOTICE"; else print "HEALTHY"}')
    row P14 "QPS vs 24h ago (ratio)" "$RB" "$SC" "drop<0.7 / surge>1.5 begins NOTICE"
  fi
else na_row P14 "QPS vs 24h ago" "no baseline data"; fi

## 4. Storage health ----------------------------------------------------
if [ -n "$KVT" ]; then
  RB1=$(q "$KVT" 'max(tikv_raftstore_region_count{type="region"}) / min(tikv_raftstore_region_count{type="region"})')
  [ -n "$RB1" ] && row S1 "Region balance (max/min)" "$(fmt "$RB1")" "$(score4 "$RB1" 1.2 1.5 2.0)" "$KVNOTE" || na_row S1 "Region balance" "no series"
  LB=$(q "$KVT" 'max(tikv_raftstore_region_count{type="leader"}) / min(tikv_raftstore_region_count{type="leader"})')
  [ -n "$LB" ] && row S2 "Leader balance (max/min)" "$(fmt "$LB")" "$(score4 "$LB" 1.3 1.5 2.0)" "$KVNOTE" || na_row S2 "Leader balance" "no series"
  CP=$(q "$KVT" 'max(tikv_engine_pending_compaction_bytes)')
  if [ -n "$CP" ]; then
    GB=$(awk -v v="$CP" 'BEGIN{printf "%.1f", v/1073741824}')
    row S3 "Compaction pending (max, GB)" "$GB" "$(score4 "$GB" 10 50 100)" "$KVNOTE"
  else na_row S3 "Compaction pending" "no series"; fi
  L0=$(q "$KVT" 'max(tikv_engine_num_files_at_level{db="kv", level="0"})')
  [ -n "$L0" ] && row S4 "L0 files (max instance)" "${L0%.*}" "$(score4 "$L0" 10 20 36)" "$KVNOTE" || na_row S4 "L0 files" "no series"
  SA=$(q "$KVT" 'min(sum by (store) (pd_scheduler_store_status{type="store_available"}) / sum by (store) (pd_scheduler_store_status{type="store_capacity"}))')
  if [ -n "$SA" ]; then
    SP=$(awk -v v="$SA" 'BEGIN{printf "%.0f", v*100}')
    row S7 "Store available% (min)" "${SP}%" "$(score4 "$SP" 40 30 20 desc)" "$KVNOTE"
  else na_row S7 "Store available%" "no series"; fi
  RS=$(q "$KVT" 'max(pd_scheduler_store_status{type="region_score"}) - min(pd_scheduler_store_status{type="region_score"})')
  [ -n "$RS" ] && row S8 "Region score spread" "$(fmt "$RS")" "-" "report-only: doc thresholds assume normalized scores, production returns raw scale — pending recalibration $KVNOTE" \
              || na_row S8 "Region score spread" "no series"
  PP=$(q "$KVT" 'sum(pd_regions_status{type="pending_peer_region_count"})')
  [ -n "$PP" ] && row S9 "Pending peers" "${PP%.*}" "$(score4 "$PP" 100 300 500)" "$KVNOTE" || na_row S9 "Pending peers" "no series"
else
  na_row S1-S9 "Storage health items" "regional plane: pool-level only — not attributable to this cluster"
fi

## 5. Availability risk ---------------------------------------------------
if [ -n "$KVT" ]; then
  OUT=$(q "$KVT" 'max(histogram_quantile(0.99, sum(rate(tikv_grpc_msg_duration_seconds_bucket[5m])) by (le, instance))) / avg(histogram_quantile(0.99, sum(rate(tikv_grpc_msg_duration_seconds_bucket[5m])) by (le, instance)))')
  [ -n "$OUT" ] && [ "$OUT" != "NaN" ] && row A1 "Instance latency outlier (max/avg P99)" "$(fmt "$OUT")" "$(score4 "$OUT" 1.5 2.0 3.0)" "$KVNOTE" || na_row A1 "Latency outlier" "no traffic"
  GE=$(q "$KVT" 'sum(rate(tikv_grpc_msg_fail_total[5m])) / sum(rate(tikv_grpc_msg_duration_seconds_count[5m]))')
  if [ -n "$GE" ] && [ "$GE" != "NaN" ]; then
    GP=$(awk -v v="$GE" 'BEGIN{printf "%.3f", v*100}')
    row A2 "gRPC error rate" "${GP}%" "$(score4 "$GP" 0.1 1 5)" "$KVNOTE"
  else row A2 "gRPC error rate" "0" "HEALTHY" "no fail series $KVNOTE"; fi
fi
BO=$(q "$SQLT" 'sum(rate(tidb_tikvclient_backoff_seconds_count[5m]))')
if [ -n "$BO" ]; then
  row A3 "Backoff rate (/s)" "$(fmt "$BO")" "$(score4 "$BO" 1 10 100)" ""
else row A3 "Backoff rate" "0" "HEALTHY" "no backoff series"; fi
if [ -n "$KVT" ]; then
  PLC=$(q "$KVT" 'max(changes(pd_server_etcd_state{type="term"}[1h]))')
  [ -n "$PLC" ] && row A6 "PD leader changes (1h)" "${PLC%.*}" "$(score4 "$PLC" 1 3 4)" "$KVNOTE" || na_row A6 "PD leader changes" "no series"
fi
PB=$(q "$SQLT" 'sum(rate(tidb_tikvclient_backoff_seconds_count{type=~"pd.*"}[5m]))')
if [ -n "$PB" ]; then
  row A7 "PD RPC backoff (/s)" "$(fmt "$PB")" "$(awk -v v="$PB" 'BEGIN{if(v==0)print "HEALTHY";else if(v<2)print "NOTICE";else if(v<5)print "WARNING";else print "CRITICAL"}')" ""
else row A7 "PD RPC backoff" "0" "HEALTHY" ""; fi
if [ -n "$KVT" ]; then
  CE=$(q "$KVT" 'sum(rate(tikv_coprocessor_request_error[5m])) / sum(rate(tikv_coprocessor_request_duration_seconds_count[5m]))')
  if [ -n "$CE" ] && [ "$CE" != "NaN" ]; then
    CEP=$(awk -v v="$CE" 'BEGIN{printf "%.3f", v*100}')
    row A9 "Coprocessor error ratio" "${CEP}%" "$(score4 "$CEP" 0.1 1 2)" "$KVNOTE"
  else row A9 "Coprocessor error ratio" "0" "HEALTHY" "no error series $KVNOTE"; fi
  RST=$(q "$KVT" 'max(changes(process_start_time_seconds[1h]))')
  [ -n "$RST" ] && row A10 "Restarts in 1h (max instance)" "${RST%.*}" "$(awk -v v="$RST" 'BEGIN{if(v==0)print "HEALTHY";else if(v<=2)print "NOTICE";else print "CRITICAL"}')" "$KVNOTE" || na_row A10 "Restarts" "no series"
fi

echo "| Item | Check | Value | Score | Note |"
echo "|---|---|---|---|---|"
printf '%b' "$ROWS"
echo

# summary
echo "## Summary"
printf '%b' "$ROWS" | awk -F'|' '{gsub(/[* ]/,"",$5); c[$5]++} END{printf "- HEALTHY=%d NOTICE=%d WARNING=%d CRITICAL=%d N/A=%d\n", c["HEALTHY"],c["NOTICE"],c["WARNING"],c["CRITICAL"],c["N/A"]}'
echo
case "$CLASS" in
  regional|pooled)
    echo "## A5: RCU / resource-control (delegated to rcu-check.sh)"
    echo
    bash "${SCRIPT_DIR}/rcu-check.sh" "$CID" | tail -n +2 ;;
esac
echo
echo "> Not collected by this script (agent follow-ups): P7 slow-query count (data-proxy SQL), P9/P10 baselines, S5 GC safepoint (unit varies), S6 MVCC slope, A4 scheduling judgment, O1-O3 SQL tops, R6 node disk IO. Raw '-' scores mean 'reported, needs spec/baseline to score'."
