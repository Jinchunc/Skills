#!/usr/bin/env bash
# fleet-overview.sh — deterministic collector for the org fleet overview
# playbook (engines/tidb/instance-inspection/playbooks/org-fleet-overview.md
# Steps 1–2). Enumerates all clusters in one org, applies the tier-correct
# collection rules, and emits a markdown inventory for the agent to interpret
# (Step 3 report writing / anomaly judgment stays with the agent).
#
# Encodes the rules that are easy to get wrong by hand:
#   - excludes resource-pool records and bran-* branches from the inventory
#   - SQL-layer metrics only for QPS/P99 on all tiers
#   - TiDB CPU uses component="tidb" on Essential/Starter (job regex fails)
#   - storage only for Dedicated (shared tiers: N/A, never pool sums)
#   - Premium pool node counts collected ONCE per pool, labeled shared
#   - per-cluster failure isolation; 0 SQL-layer series => idle, not error
#
# Usage:
#   CLINIC_API_KEY=... bash fleet-overview.sh <DBAAS_ORG_ID> [--no-metrics]

set -u
EP="${CLINIC_ENDPOINT:-https://clinic.pingcap.com}"
TOKEN="${CLINIC_API_KEY:-${CLINIC_API_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "ERROR: set CLINIC_API_KEY or CLINIC_API_TOKEN" >&2; exit 2; }
ORG="${1:-}"; [ -n "$ORG" ] || { echo "usage: fleet-overview.sh <DBAAS_ORG_ID> [--no-metrics]" >&2; exit 2; }
METRICS=true; [ "${2:-}" = "--no-metrics" ] && METRICS=false

api() { curl -s --max-time 30 -H "Authorization: Bearer ${TOKEN}" "$@"; }
iq() { # cluster promql -> scalar|"" ; empty result => ""
  api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/$1/metrics/query" --data-urlencode "query=$2" \
    | jq -r '.data.result[0].value[1] // empty' 2>/dev/null
}

# ── Step 1: enumerate (paginate until empty) ────────────────────────
ALL="[]"; PAGE=1
while :; do
  RESP=$(api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters" \
    --data-urlencode "pageSize=50" --data-urlencode "page=${PAGE}" \
    --data-urlencode "showDeleted=false")
  N=$(echo "$RESP" | jq '.items|length' 2>/dev/null) || { echo "ERROR enumerating org ${ORG}: $(echo "$RESP"|head -c 200)" >&2; exit 3; }
  [ "$N" = "0" ] && break
  ALL=$(jq -n --argjson a "$ALL" --argjson b "$(echo "$RESP" | jq '.items')" '$a + $b')
  PAGE=$((PAGE+1)); sleep 0.3
done

TOTAL=$(echo "$ALL" | jq 'length')
POOLS=$(echo "$ALL" | jq '[.[] | select(.deployType=="nextgen-host" or ((.deployTypeV2 // "") | test("resource pool")))]')
BRANCHES=$(echo "$ALL" | jq '[.[] | select(.clusterId | startswith("bran-"))]')
CLUSTERS=$(echo "$ALL" | jq '[.[] | select((.clusterId | startswith("bran-")) | not)
                                   | select(.deployType!="nextgen-host")
                                   | select((.deployTypeV2 // "") | test("resource pool") | not)]')
NC=$(echo "$CLUSTERS" | jq 'length')

echo "# Fleet inventory — org ${ORG}"
echo
echo "**Records found:** ${TOTAL} total → **${NC} clusters** (excluded: $(echo "$POOLS"|jq length) resource-pool records, $(echo "$BRANCHES"|jq length) branches)"
echo
echo "## Fleet statistics"
echo "- Tiers: $(echo "$CLUSTERS" | jq -r '[.[].deployTypeV2] | group_by(.) | map("\(.[0]): \(length)") | join(", ")')"
echo "- Regions: $(echo "$CLUSTERS" | jq -r '[.[] | "\(.provider // "?")/\(.region // "?")"] | group_by(.) | map("\(.[0]): \(length)") | join(", ")')"
echo "- Versions: $(echo "$CLUSTERS" | jq -r '[.[].version // "?"] | group_by(.) | map("\(.[0]): \(length)") | join(", ")')"
echo

# ── Step 2: per-cluster collection (failure-isolated) ───────────────
echo "## Cluster inventory"
echo
echo "| Cluster | ID | Tier | Region | Version | QPS | P99 (ms) | TiDB CPU | Storage | Status |"
echo "|---|---|---|---|---|---|---|---|---|---|"

declare -A SEEN_POOLS
POOL_LINES=""
echo "$CLUSTERS" | jq -c '.[]' | while read -r C; do
  ID=$(echo "$C" | jq -r .clusterId); NAME=$(echo "$C" | jq -r '.name // "?"')
  T=$(echo "$C" | jq -r '.deployTypeV2 // "?"'); REGV=$(echo "$C" | jq -r '"\(.provider // "?")/\(.region // "?")"')
  VER=$(echo "$C" | jq -r '.version // "?"'); ST=$(echo "$C" | jq -r '.status // "?"')
  QPS="N/A"; P99="N/A"; CPU="N/A"; STOR="N/A"
  if $METRICS && [ "$ST" = "active" ]; then
    Q=$(iq "$ID" 'sum(rate(tidb_server_query_total[5m]))')
    if [ -z "$Q" ]; then
      case "$T" in starter|essential) ST="idle (scaled to zero)";; *) ST="active (no SQL metrics)";; esac
    else
      QPS=$(printf '%.1f' "$Q" 2>/dev/null || echo "$Q")
      P=$(iq "$ID" 'histogram_quantile(0.99, sum(rate(tidb_server_handle_query_duration_seconds_bucket[5m])) by (le))')
      [ -n "$P" ] && [ "$P" != "NaN" ] && P99=$(awk "BEGIN{printf \"%.1f\", $P*1000}" 2>/dev/null || echo "$P")
      case "$T" in
        starter|essential) CEXPR='sum(rate(process_cpu_seconds_total{component="tidb"}[5m]))' ;;
        *)                 CEXPR='sum(rate(process_cpu_seconds_total{job=~"tidb.*"}[5m]))' ;;
      esac
      CC=$(iq "$ID" "$CEXPR"); [ -n "$CC" ] && CPU=$(printf '%.2f' "$CC" 2>/dev/null || echo "$CC")
    fi
    if [ "$T" = "dedicated" ]; then
      SU=$(iq "$ID" 'sum(tikv_store_size_bytes{type="used"})')
      SCAP=$(iq "$ID" 'sum(tikv_store_size_bytes{type="capacity"})')
      if [ -n "$SU" ] && [ -n "$SCAP" ]; then
        STOR=$(awk "BEGIN{printf \"%.0f%%\", $SU/$SCAP*100}" 2>/dev/null || echo "N/A")
      fi
    else
      STOR="N/A (shared)"
    fi
    sleep 0.3
  fi
  printf '| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n' \
    "$NAME" "$ID" "$T" "$REGV" "$VER" "$QPS" "$P99" "$CPU" "$STOR" "$ST"
done

# ── Shared pools: node counts once per pool ─────────────────────────
PN=$(echo "$POOLS" | jq 'length')
if [ "$PN" -gt 0 ]; then
  echo
  echo "## Shared resource pools (nodes counted ONCE — shared across all hosted clusters)"
  echo
  echo "| Pool | ID | TiKV | PD | TiFlash compute | Hosted clusters (this org) |"
  echo "|---|---|---|---|---|---|"
  echo "$POOLS" | jq -c '.[]' | while read -r P; do
    PID=$(echo "$P" | jq -r .clusterId); PNAME=$(echo "$P" | jq -r '.name // "?"')
    COMP=$(api "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/${PID}/components")
    TK=$(echo "$COMP" | jq '[.[]? | select(.component_type=="TIKV")   | .node_count] | add // "?"' 2>/dev/null)
    PD=$(echo "$COMP" | jq '[.[]? | select(.component_type=="PD")     | .node_count] | add // "?"' 2>/dev/null)
    TF=$(echo "$COMP" | jq '[.[]? | select(.component_type|startswith("TIFLASH")) | .node_count] | add // "?"' 2>/dev/null)
    HOSTED=$(echo "$CLUSTERS" | jq -r --arg p "$PID" '[.[] | select(.diagnosticContext.parentId==$p) | .clusterId] | length')
    printf '| %s | %s | %s | %s | %s | %s |\n' "$PNAME" "$PID" "$TK" "$PD" "$TF" "$HOSTED"
    sleep 0.3
  done
fi

echo
echo "> Interpretation reminders: every cluster above must appear exactly once; idle = scaled-to-zero, not an outage;"
echo "> shared-tier storage is intentionally N/A (pool bytes are multi-tenant); drill into one cluster with checklist-collect.sh."
