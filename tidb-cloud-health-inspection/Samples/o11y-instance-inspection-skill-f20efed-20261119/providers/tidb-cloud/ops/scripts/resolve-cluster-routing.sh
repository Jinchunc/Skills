#!/usr/bin/env bash
# resolve-cluster-routing.sh — given a TiDB Cloud cluster ID, emit one JSON
# object saying WHICH ID and WHICH route every kind of o11y data lives under.
#
# This mechanically encodes references/tidb-x-o11y-routing.md so downstream
# scripts (rcu-check, loki-triage, fleet-overview, checklist-collect) and
# ad-hoc agent calls cannot mis-scope shared-tier queries.
#
# Usage:
#   CLINIC_API_KEY=... bash resolve-cluster-routing.sh <CLUSTER_ID>
#
# Output JSON fields:
#   clusterId,name,tier(deployTypeV2),deployType,orgId,projectId,provider,
#   region,recordType(logical|resource_pool|branch),poolId,keyspaceId,
#   keyspaceName,controlPlaneInfo,routes{...},ngmHeaders{...}
#
# keyspaceId/controlPlaneInfo come from the cluster's own tidb_* metric
# labels and are null for idle (scaled-to-zero) clusters.
#
# Exit codes: 0 ok, 2 usage/auth, 3 cluster not found.

set -u
EP="${CLINIC_ENDPOINT:-https://clinic.pingcap.com}"
TOKEN="${CLINIC_API_KEY:-${CLINIC_API_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo '{"error":"set CLINIC_API_KEY or CLINIC_API_TOKEN"}' >&2; exit 2; }
CID="${1:-}"; [ -n "$CID" ] || { echo '{"error":"usage: resolve-cluster-routing.sh <CLUSTER_ID>"}' >&2; exit 2; }

api() { curl -s --max-time 30 -H "Authorization: Bearer ${TOKEN}" "$@"; }

META=$(api "${EP}/data-proxy/api/v1/clusters/${CID}")
echo "$META" | jq -e '.clusterId' >/dev/null 2>&1 || { echo "{\"error\":\"cluster not found\",\"detail\":$(echo "$META" | jq -c . 2>/dev/null || echo '""')}" >&2; exit 3; }

ORG=$(echo "$META" | jq -r '.orgId')
DT=$(echo "$META" | jq -r '.deployType // ""')
DT2=$(echo "$META" | jq -r '.deployTypeV2 // ""')
PARENT=$(echo "$META" | jq -r '.diagnosticContext.parentId // ""')
PROJ=$(echo "$META" | jq -r '.diagnosticContext.projectId // ""')
PROV=$(echo "$META" | jq -r '.provider // ""')
REG=$(echo "$META" | jq -r '.region // ""')
NAME=$(echo "$META" | jq -r '.name // ""')

RECORD_TYPE="logical"; POOL=""; PARENT_IS_POOL=false
case "$CID" in bran-*) RECORD_TYPE="branch" ;; esac
if [ "$DT" = "nextgen-host" ] || echo "$DT2" | grep -q "resource pool"; then RECORD_TYPE="resource_pool"; fi

# For logical clusters with a parentId, check whether the parent is a pool
# (Premium / pool-hosted Essential) or a branch source.
if [ "$RECORD_TYPE" = "logical" ] && [ -n "$PARENT" ]; then
  PDT=$(api "${EP}/data-proxy/api/v1/clusters/${PARENT}" | jq -r '.deployType // ""')
  if [ "$PDT" = "nextgen-host" ]; then POOL="$PARENT"; PARENT_IS_POOL=true; fi
fi

# keyspace / control-plane identity from the cluster's own tidb_* series.
NOW=$(date +%s); AGO=$((NOW-900))
KSID=null; KSNAME=null; CPI=null
if [ "$RECORD_TYPE" = "logical" ]; then
  S=$(api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/${CID}/metrics/series" \
      --data-urlencode 'match[]=tidb_server_query_total' \
      --data-urlencode "start=${AGO}" --data-urlencode "end=${NOW}" | jq -c '.data[0] // {}')
  KSID=$(echo "$S"   | jq '.keyspace_id // null')
  KSNAME=$(echo "$S" | jq '.keyspace_name // null')
  CPI=$(echo "$S"    | jq '.control_plane_info // null')
fi

mk_routes() { # tier-class -> routes JSON
  local base="/data-proxy/api/v1/orgs/${ORG}/clusters/${CID}"
  case "$1" in
    dedicated)
      jq -n --arg b "$base" '{metrics_sql_layer:($b+"/metrics/*"),metrics_storage_layer:($b+"/metrics/* (cluster-scoped)"),logs:($b+"/logs/* selector {container=...}"),slow_query:("data-proxy SQL: POST "+$b+"/sql-queries"),components:($b+"/components (full topology)")}' ;;
    pooled) # premium-style: shared layer on a pool record
      jq -n --arg b "$base" --arg p "/data-proxy/api/v1/orgs/${ORG}/clusters/${POOL}" --arg cid "$CID" --arg pool "$POOL" \
        '{metrics_sql_layer:($b+"/metrics/*"),metrics_storage_layer:($p+"/metrics/*  (SHARED across keyspaces — never report as this cluster alone)"),logs:($b+"/logs/*  SQL layer selector {component=\"tidb\", tidb_cluster_id=\""+$cid+"\"}; storage selector {component=\"tikv\", tidb_cluster_id=\""+$pool+"\"} — never bare {container=\"tidb\"}"),slow_query:("data-proxy SQL: POST "+$b+"/sql-queries"),components:($b+"/components = keyspace compute (TIDB/TIPROXY); "+$p+"/components = shared TiKV/PD layer (dedupe by pool in fleet totals)")}' ;;
    regional) # essential/starter on the regional multi-tenant plane
      jq -n --arg b "$base" \
        '{metrics_sql_layer:($b+"/metrics/*"),metrics_storage_layer:"/data-proxy/serverless-parent-metrics/api/v1/* with control_plane_info — POOL-LEVEL ONLY; the cluster route returns unscoped cross-tenant tikv_*/pd_*: never use it",logs:"/data-proxy/serverless-parent-logs/api/v1/* with control_plane_info — pool-level; attribute lines via keyspace in content",slow_query:"NGM GET /ngm/api/v1/slow_query/list with x-deploy-type: shared (fields param REQUIRED)",components:"unsupported (InvalidArgument)"}' ;;
  esac
}

CLASS="dedicated"
if [ "$RECORD_TYPE" = "logical" ]; then
  case "$DT2" in
    dedicated) CLASS="dedicated" ;;
    premium|byoc) CLASS="pooled" ;;
    essential|starter) if $PARENT_IS_POOL; then CLASS="pooled"; else CLASS="regional"; fi ;;
    *) CLASS="dedicated" ;;
  esac
fi

ROUTES=$(mk_routes "$CLASS")
NGM=$(jq -n --arg p "$PROV" --arg r "$REG" --arg o "$ORG" --arg pj "$PROJ" --arg c "$CID" \
  '{ "x-provider":$p,"x-region":$r,"x-org-id":$o,"x-project-id":$pj,"x-cluster-id":$c,"x-deploy-type":"shared" }')

jq -n \
  --arg clusterId "$CID" --arg name "$NAME" --arg tier "$DT2" --arg deployType "$DT" \
  --arg orgId "$ORG" --arg projectId "$PROJ" --arg provider "$PROV" --arg region "$REG" \
  --arg recordType "$RECORD_TYPE" --arg poolId "$POOL" --arg class "$CLASS" \
  --argjson keyspaceId "$KSID" --argjson keyspaceName "$KSNAME" --argjson controlPlaneInfo "$CPI" \
  --argjson routes "$ROUTES" --argjson ngmHeaders "$NGM" \
  '{clusterId:$clusterId,name:$name,tier:$tier,deployType:$deployType,orgId:$orgId,projectId:$projectId,
    provider:$provider,region:$region,recordType:$recordType,routingClass:$class,
    poolId:(if $poolId=="" then null else $poolId end),
    keyspaceId:$keyspaceId,keyspaceName:$keyspaceName,controlPlaneInfo:$controlPlaneInfo,
    routes:$routes,
    ngmHeaders:(if $class=="regional" then $ngmHeaders else null end),
    notes:(if $recordType=="branch" then "branch cluster — parentID is the branch SOURCE cluster, not a pool"
           elif $recordType=="resource_pool" then "this is a shared resource pool, NOT a customer cluster — exclude from inventories"
           elif $class=="regional" and $controlPlaneInfo==null then "idle/scaled-to-zero sample: keyspace/control-plane labels unavailable until traffic resumes"
           else null end)}'
