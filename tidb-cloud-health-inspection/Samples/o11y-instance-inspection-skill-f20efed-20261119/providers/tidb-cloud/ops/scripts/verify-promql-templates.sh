#!/usr/bin/env bash
# verify-promql-templates.sh — assert that the base metric series referenced
# by the DBA docs still exist in production. Companion to
# verify-tier-routing.sh: that one checks ROUTING claims, this one checks
# METRIC-NAME claims (the class of drift that bit us with
# resource_manager_resource_unit_service_limit, removed upstream).
#
# Probes a discovered Dedicated cluster (classic metric set) and, when
# available, a Premium pool (shared-layer set) via series discovery.
# Missing metrics are reported with the docs that reference them.
#
# Usage: CLINIC_API_KEY=... bash verify-promql-templates.sh

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
EP="${CLINIC_ENDPOINT:-https://clinic.pingcap.com}"
TOKEN="${CLINIC_API_KEY:-${CLINIC_API_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "ERROR: set CLINIC_API_KEY or CLINIC_API_TOKEN" >&2; exit 2; }
NOW=$(date +%s); AGO=$((NOW-1800))
PASS=0; FAIL=0; SKIP=0

api() { curl -s --max-time 30 -H "Authorization: Bearer ${TOKEN}" "$@"; }
have() { # org cluster metric -> 0 if series exist
  local n
  n=$(api -G "${EP}/data-proxy/api/v1/orgs/$1/clusters/$2/metrics/series" \
    --data-urlencode "match[]=$3" \
    --data-urlencode "start=${AGO}" --data-urlencode "end=${NOW}" | jq '.data|length' 2>/dev/null)
  [ "${n:-0}" -gt 0 ]
}
check() { # org cluster metric refdoc
  if have "$1" "$2" "$3"; then PASS=$((PASS+1)); echo "PASS  $3"
  else FAIL=$((FAIL+1)); echo "FAIL  $3  — referenced by $4; name may have drifted upstream"; fi
}

disc() { api -G "${EP}/clinic/api/v1/dashboard/clusters" \
  --data-urlencode "deploy_type_v2=$1" --data-urlencode "cluster_status=active" --data-urlencode "limit=10" \
  | jq -c '[.items[]? | select(.clusterID|startswith("bran-")|not)][0] // empty'; }

DED=$(disc dedicated)
PREM=$(disc premium)

# Classic / SQL-layer metric families (checklist.md, metrics SUBSKILL, rcu-check.md)
if [ -n "$DED" ]; then
  DID=$(echo "$DED" | jq -r .clusterID); DORG=$(echo "$DED" | jq -r .tenantID)
  echo "== dedicated sample ${DID} =="
  for m in \
    tidb_server_query_total tidb_server_handle_query_duration_seconds_bucket \
    tidb_server_connections tidb_executor_statement_total \
    tidb_tikvclient_ts_future_wait_seconds_bucket \
    process_cpu_seconds_total process_resident_memory_bytes process_start_time_seconds \
    tikv_store_size_bytes tikv_raftstore_region_count 'tikv_raftstore_region_count{type="leader"}' \
    tikv_engine_pending_compaction_bytes tikv_engine_num_files_at_level \
    tikv_grpc_msg_duration_seconds_bucket tikv_thread_cpu_seconds_total tikv_server_info \
    pd_scheduler_store_status pd_regions_status pd_server_etcd_state pd_cluster_status; do
    check "$DORG" "$DID" "$m" "checklist.md / metrics SUBSKILL"
  done
  # Event-conditional metrics: absence on a quiet sample is not drift.
  for m in tidb_server_execute_error_total tidb_tikvclient_backoff_seconds_count; do
    if have "$DORG" "$DID" "$m"; then PASS=$((PASS+1)); echo "PASS  $m"
    else SKIP=$((SKIP+1)); echo "SKIP  $m — event-conditional, no events on this sample (not drift)"; fi
  done
else
  SKIP=$((SKIP+1)); echo "SKIP  dedicated metric set (no sample)"
fi

# Shared-layer (Premium pool) additions
if [ -n "$PREM" ]; then
  PID=$(echo "$PREM" | jq -r .clusterID); PORG=$(echo "$PREM" | jq -r .tenantID)
  POOL=$(api "${EP}/data-proxy/api/v1/clusters/${PID}" | jq -r '.diagnosticContext.parentId // empty')
  if [ -n "$POOL" ]; then
    echo "== premium logical ${PID} / pool ${POOL} =="
    check "$PORG" "$PID"  "tidb_server_query_total" "tidb-x-o11y-routing.md (keyspace_name label schema)"
    check "$PORG" "$PID"  "resource_manager_client_request_success_bucket" "rcu-check.md Branch B"
    check "$PORG" "$POOL" "tikv_thread_cpu_seconds_total" "tidb-x-o11y-routing.md (pool scope)"
    check "$PORG" "$POOL" "tikv_coprocessor_request_wait_seconds_bucket" "rcu-check.md B2 / checklist A9"
  else
    SKIP=$((SKIP+1)); echo "SKIP  premium pool set (sample has no parentId)"
  fi
else
  SKIP=$((SKIP+1)); echo "SKIP  premium metric set (no sample)"
fi

# Known-removed metrics that must STAY absent (docs say do-not-use)
if [ -n "$DED" ]; then
  if have "$DORG" "$DID" "resource_manager_resource_unit_service_limit"; then
    FAIL=$((FAIL+1)); echo "FAIL  resource_manager_resource_unit_service_limit REAPPEARED — rcu-check.md marks it removed"
  else
    PASS=$((PASS+1)); echo "PASS  resource_manager_resource_unit_service_limit still absent (as documented)"
  fi
fi

# ── Auto-scan: extract metric names from the docs and probe them ──────
# Curated lists above stay the FAIL-level authority; auto-scan catches any
# copy of a metric name anywhere in the diagnostic docs and reports absent
# ones as WARN for human triage (the scanner cannot know which metrics are
# event-conditional). metrics_1/2/3 reference dictionaries are excluded by
# default (probe them with --full).
WARN=0
ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
# Default scope: steady-state docs whose metrics should exist on any healthy
# cluster. fault-diagnosis / performance-optimization scenario docs reference
# incident-only metrics (absent on healthy samples by design) — scan them
# only with --full, and triage those WARNs accordingly.
SCAN_PATHS=(
  "$ROOT/engines/tidb/instance-inspection" "$ROOT/engines/tidb/references"
  "$ROOT/engines/tidb/knowledge/rcu-check.md" "$ROOT/engines/tidb/knowledge/product-lines"
  "$ROOT/providers/tidb-cloud/ops"
)
[ "${1:-}" = "--full" ] && SCAN_PATHS+=("$ROOT/engines/tidb/fault-diagnosis" "$ROOT/engines/tidb/performance-optimization")
NONMETRIC="tidb_cluster_id|tidb_cluster|tikv_gc_life_time|tikv_gc_run_interval|resource_manager_resource_unit_service_limit|tidb_config_status|pd_config_status|tidb_read_staleness|tidb_replica_read|tidb_side|tidb_server_memory_limit"
# Only names in real query positions (followed by { [ or ( ) — prose words,
# system variables (also tidb_-prefixed!) and METRICS_SCHEMA aliases don't match.
EXCL=(--exclude='metrics_1_core.md' --exclude='metrics_2_common.md' --exclude='metrics_3_other.md')
[ "${1:-}" = "--full" ] && EXCL=()
NAMES=$(grep -rhoE '\b(tidb|tikv|pd|tiflash|process|resource_manager|serverless_metering)_[a-z0-9_]+[\{\[\(]' \
    "${SCAN_PATHS[@]}" --include='*.md' "${EXCL[@]}" 2>/dev/null \
  | sed 's/[{\[(]$//' \
  | sort -u | grep -vE "^(${NONMETRIC})$" | head -200)
if [ -n "$DED" ] && [ -n "$NAMES" ]; then
  echo
  echo "== auto-scan: $(echo "$NAMES" | wc -l) unique metric names from docs =="
  # Samples without TiFlash make every tiflash_* name a false WARN.
  HAS_TIFLASH=false
  have "$DORG" "$DID" 'tiflash_proxy_process_cpu_seconds_total' && HAS_TIFLASH=true
  ABSENT=""; TSKIP=0
  for m in $NAMES; do
    case "$m" in tiflash_*) $HAS_TIFLASH || { TSKIP=$((TSKIP+1)); continue; } ;; esac
    if have "$DORG" "$DID" "$m"; then PASS=$((PASS+1)); continue; fi
    # histogram base name: series only exist with _bucket/_count/_sum suffixes
    case "$m" in
      *_bucket|*_count|*_sum) : ;;
      *) if have "$DORG" "$DID" "${m}_bucket"; then PASS=$((PASS+1)); continue; fi ;;
    esac
    if [ -n "${PORG:-}" ] && have "$PORG" "$PID" "$m"; then PASS=$((PASS+1)); continue; fi
    if [ -n "${POOL:-}" ] && have "$PORG" "$POOL" "$m"; then PASS=$((PASS+1)); continue; fi
    WARN=$((WARN+1)); ABSENT="${ABSENT}${m}\n"
  done
  [ "$TSKIP" -gt 0 ] && { SKIP=$((SKIP+TSKIP)); echo "SKIP  ${TSKIP} tiflash_* names (no TiFlash on probed samples)"; }
  if [ -n "$ABSENT" ]; then
    echo "WARN — absent on all probed samples (triage: event-conditional vs renamed):"
    printf '%b' "$ABSENT" | sed 's/^/  WARN  /'
  else
    echo "all scanned names present"
  fi
fi

echo
echo "== summary: PASS=$PASS FAIL=$FAIL WARN=$WARN SKIP=$SKIP =="
[ "$FAIL" -eq 0 ] || { echo "FAILs mean metric names drifted — update the referencing docs."; exit 1; }
