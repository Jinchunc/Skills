#!/usr/bin/env bash
# rcu-check.sh — evidence collector for RCU throttling diagnosis
# (engines/tidb/knowledge/rcu-check.md Branch A/B).
#
# Collects the deterministic metric evidence and prints a table; the
# severity verdict is a heuristic per the doc's thresholds — final judgment
# (and root-cause work) stays with the operator/agent using rcu-check.md.
#
# Usage:
#   CLINIC_API_KEY=... bash rcu-check.sh <CLUSTER_ID> [window_seconds=1800]
#
# Branch A (Essential/Starter, regional plane): provisioned capacity,
#   available RU, read/write max-per-sec, consume rate — via keyspace_id.
# Branch B (Premium, pooled): kv-request wait P99 (logical ID) and
#   coprocessor queue P99 (pool ID).
# Dedicated: exits — no RCU limits.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
EP="${CLINIC_ENDPOINT:-https://clinic.pingcap.com}"
TOKEN="${CLINIC_API_KEY:-${CLINIC_API_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "ERROR: set CLINIC_API_KEY or CLINIC_API_TOKEN" >&2; exit 2; }
CID="${1:-}"; [ -n "$CID" ] || { echo "usage: rcu-check.sh <CLUSTER_ID> [window_seconds]" >&2; exit 2; }
WIN="${2:-1800}"

R=$(bash "${SCRIPT_DIR}/resolve-cluster-routing.sh" "$CID") || exit 3
CLASS=$(echo "$R" | jq -r .routingClass); TIER=$(echo "$R" | jq -r .tier)
ORG=$(echo "$R" | jq -r .orgId); POOL=$(echo "$R" | jq -r '.poolId // empty')
KSID=$(echo "$R" | jq -r '.keyspaceId // empty')

api() { curl -s --max-time 30 -H "Authorization: Bearer ${TOKEN}" "$@"; }
iq() { # org cluster promql -> scalar or "-"
  api -G "${EP}/data-proxy/api/v1/orgs/$1/clusters/$2/metrics/query" --data-urlencode "query=$3" \
    | jq -r '.data.result[0].value[1] // "-"'
}

echo "# RCU check — ${CID} (${TIER}, routing class: ${CLASS})"
echo

case "$CLASS" in
  dedicated)
    echo "Dedicated cluster: no RCU limits — this module does not apply."; exit 0 ;;

  regional)
    if [ -z "$KSID" ]; then
      echo "keyspace_id unavailable (idle/scaled-to-zero) — RCU metrics cannot be scoped."
      echo "Fallback per rcu-check.md: infer from slow_query_logs rc_wait once traffic resumes."
      exit 0
    fi
    PROV=$(iq "$ORG" "$CID" "sum(serverless_metering_rcu_provisioned_capacity{keyspace_id=\"${KSID}\"})")
    AVAIL=$(iq "$ORG" "$CID" "min(resource_manager_resource_unit_available_ru{name=\"${KSID}\"})")
    RMAX=$(iq "$ORG" "$CID" "max(resource_manager_resource_unit_read_request_unit_max_per_sec{name=\"${KSID}\"})")
    WMAX=$(iq "$ORG" "$CID" "max(resource_manager_resource_unit_write_request_unit_max_per_sec{name=\"${KSID}\"})")
    RATE=$(iq "$ORG" "$CID" "sum(resource_manager_resource_unit_request_unit_consume_rate{name=\"${KSID}\"})")
    echo "| Evidence (keyspace ${KSID}) | Value |"
    echo "|---|---|"
    echo "| Provisioned RCU capacity | ${PROV} |"
    echo "| Available RU (min) | ${AVAIL} |"
    echo "| Read RU max/sec | ${RMAX} |"
    echo "| Write RU max/sec | ${WMAX} |"
    echo "| RU consume rate (sum) | ${RATE} |"
    echo
    if [ "$AVAIL" = "-" ] && [ "$RATE" = "-" ]; then
      echo "Verdict hint: headroom metrics absent for this keyspace mode — UNKNOWN from metrics."
      echo "Decide via slow_query_logs rc_wait (rcu-check.md Step A2); do NOT mark HEALTHY on absence."
    elif [ "$AVAIL" != "-" ] && awk "BEGIN{exit !($AVAIL <= 0)}" 2>/dev/null; then
      echo "Verdict hint: available RU ~0 → RCU-CAPPED (confirm with rc_wait in slowlog)."
    else
      echo "Verdict hint: headroom present; corroborate with rc_wait + QPS plateau before concluding."
    fi ;;

  pooled)
    NOW=$(date +%s); START=$((NOW-WIN))
    KVW=$(api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/${CID}/metrics/query" \
      --data-urlencode "query=histogram_quantile(0.99, sum(rate(resource_manager_client_request_success_bucket[5m])) by (le))" \
      | jq -r '.data.result[0].value[1] // "-"')
    COPQ="-"
    if [ -n "$POOL" ]; then
      COPQ=$(api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/${POOL}/metrics/query" \
        --data-urlencode "query=histogram_quantile(0.99, sum(rate(tikv_coprocessor_request_wait_seconds_bucket[5m])) by (le))" \
        | jq -r '.data.result[0].value[1] // "-"')
    fi
    echo "| Evidence | Scope | Value |"
    echo "|---|---|---|"
    echo "| KV request wait P99 (s) | logical cluster | ${KVW} |"
    echo "| Coprocessor request wait P99 (s) | POOL (shared, all keyspaces) | ${COPQ} |"
    echo
    if [ "$KVW" != "-" ] && awk "BEGIN{exit !($KVW > 0.05)}" 2>/dev/null; then
      echo "Verdict hint: KV request wait P99 > 50ms → severe resource-management queuing (rcu-check.md B1)."
    elif [ "$KVW" != "-" ] && awk "BEGIN{exit !($KVW > 0.01)}" 2>/dev/null; then
      echo "Verdict hint: KV request wait P99 > 10ms → resource-management queuing present."
    else
      echo "Verdict hint: no clear RM queuing at SQL layer. Note the cop-wait number is pool-wide —"
      echo "high values need keyspace attribution (TopSQL) before blaming this cluster."
    fi ;;
esac
