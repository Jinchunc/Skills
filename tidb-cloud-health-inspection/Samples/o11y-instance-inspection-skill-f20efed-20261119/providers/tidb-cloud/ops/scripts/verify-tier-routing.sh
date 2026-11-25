#!/usr/bin/env bash
# verify-tier-routing.sh — regression probes for the per-tier Clinic o11y
# routing claims documented in ../references/tidb-x-o11y-routing.md.
#
# Every check asserts one documented claim against production Clinic. Run it
# periodically (e.g. monthly) or after any platform change: a FAIL means the
# platform behavior drifted and the docs (routing reference, rcu-check.md,
# metrics/loki SUBSKILLs) need re-verification — not that the script is wrong.
#
# Usage:
#   export CLINIC_API_KEY=...            # or CLINIC_API_TOKEN
#   bash verify-tier-routing.sh
#
# Optional env:
#   CLINIC_ENDPOINT        default https://clinic.pingcap.com
#   VERIFY_PREMIUM_ID / VERIFY_ESSENTIAL_ID / VERIFY_DEDICATED_ID
#                          pin sample clusters instead of auto-discovery
#
# Sample clusters are auto-discovered per tier from /clinic/api/v1/dashboard/
# clusters (active, non-branch). Checks whose sample is idle (scale-to-zero,
# no log lines) report SKIP, not FAIL.
#
# First verified baseline: 2026-08-06.

set -u

EP="${CLINIC_ENDPOINT:-https://clinic.pingcap.com}"
TOKEN="${CLINIC_API_KEY:-${CLINIC_API_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "ERROR: set CLINIC_API_KEY or CLINIC_API_TOKEN"; exit 2; }

PASS=0; FAIL=0; SKIP=0; WARN=0
pass() { PASS=$((PASS+1)); echo "PASS  $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL  $1"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP  $1"; }
warn() { WARN=$((WARN+1)); echo "WARN  $1"; }

api()  { curl -s --max-time 30 -H "Authorization: Bearer ${TOKEN}" "$@"; }

NOW=$(date +%s); AGO15=$((NOW-900))
NS_NOW="${NOW}000000000"; NS_1H="$((NOW-3600))000000000"

series() { # org cluster match -> series JSON
  api -G "${EP}/data-proxy/api/v1/orgs/$1/clusters/$2/metrics/series" \
    --data-urlencode "match[]=$3" \
    --data-urlencode "start=${AGO15}" --data-urlencode "end=${NOW}"
}
mquery() { # org cluster promql -> instant query JSON
  api -G "${EP}/data-proxy/api/v1/orgs/$1/clusters/$2/metrics/query" \
    --data-urlencode "query=$3"
}

# ── Sample discovery ────────────────────────────────────────────────
discover() { # deploy_type_v2 jq-filter -> one dashboard record
  api -G "${EP}/clinic/api/v1/dashboard/clusters" \
    --data-urlencode "deploy_type_v2=$1" \
    --data-urlencode "cluster_status=active" \
    --data-urlencode "limit=30" |
  jq -c "[.items[]? | select(.clusterID | startswith(\"bran-\") | not) | select($2)][0] // empty"
}

pick() { # tier-name override-id deploy_type jq-filter -> record (respects override)
  local override="$2"
  if [ -n "$override" ]; then
    api -G "${EP}/clinic/api/v1/dashboard/clusters" \
      --data-urlencode "cluster_id=${override}" | jq -c '.items[0] // empty'
  else
    discover "$3" "$4"
  fi
}

PREM=$(pick premium   "${VERIFY_PREMIUM_ID:-}"   premium   '.parentID != ""')
ESS=$(pick  essential "${VERIFY_ESSENTIAL_ID:-}" essential '.parentID == ""')
DED=$(pick  dedicated "${VERIFY_DEDICATED_ID:-}" dedicated 'true')

rec() { echo "$1" | jq -r "$2"; }

echo "== samples =="
for t in PREM ESS DED; do
  v=$(eval echo "\$$t")
  if [ -n "$v" ]; then
    echo "  $t: $(rec "$v" '.clusterID') ($(rec "$v" '.clusterName'))"
  else
    echo "  $t: not found — its checks will be skipped"
  fi
done

# ── Premium: two-layer routing ──────────────────────────────────────
if [ -n "$PREM" ]; then
  P_ID=$(rec "$PREM" .clusterID); P_ORG=$(rec "$PREM" .tenantID)
  P_POOL=$(rec "$PREM" .parentID)

  META=$(api "${EP}/data-proxy/api/v1/clusters/${P_POOL}")
  DT=$(echo "$META" | jq -r '.deployType // empty')
  [ "$DT" = "nextgen-host" ] \
    && pass "premium: parentId resolves to a nextgen-host pool record" \
    || fail "premium: parentId pool deployType='$DT' (expected nextgen-host) — pool-discovery claim drifted"

  COMP=$(api "${EP}/data-proxy/api/v1/orgs/${P_ORG}/clusters/${P_ID}/components")
  TYPES=$(echo "$COMP" | jq -r 'if type=="array" then [.[].component_type]|join(",") else "ERR" end')
  case "$TYPES" in
    *TIKV*|*PD*) fail "premium: logical components contain TiKV/PD ($TYPES) — keyspace-compute-only claim drifted" ;;
    *TIDB*)      pass "premium: logical components are keyspace compute only ($TYPES)" ;;
    *)           warn "premium: logical components unexpected: $TYPES" ;;
  esac

  PCOMP=$(api "${EP}/data-proxy/api/v1/orgs/${P_ORG}/clusters/${P_POOL}/components" | jq -r 'if type=="array" then [.[].component_type]|join(",") else "ERR" end')
  case "$PCOMP" in
    *TIKV*) pass "premium: pool components expose shared TiKV/PD layer" ;;
    *)      fail "premium: pool components missing TIKV ($PCOMP)" ;;
  esac

  S=$(series "$P_ORG" "$P_ID" 'tidb_server_query_total')
  N=$(echo "$S" | jq '.data|length')
  if [ "${N:-0}" -gt 0 ]; then
    KS=$(echo "$S" | jq -r '.data[0].keyspace_name // empty')
    SP=$(echo "$S" | jq -r '.data[0].sharedpool_id // empty')
    { [ "$KS" = "$P_ID" ] && [ "$SP" = "$P_POOL" ]; } \
      && pass "premium: tidb_* series carry keyspace_name=<logical> + sharedpool_id=<pool>" \
      || fail "premium: tidb_* label schema drifted (keyspace_name=$KS sharedpool_id=$SP)"
  else
    skip "premium: no tidb_* series in window (idle sample) — label check"
  fi

  NL=$(series "$P_ORG" "$P_ID" 'tikv_thread_cpu_seconds_total' | jq '.data|length')
  [ "${NL:-0}" -eq 0 ] \
    && pass "premium: tikv_* EMPTY under logical ID (must query pool)" \
    || fail "premium: tikv_* returned $NL series under logical ID — routing claim drifted"

  NP=$(series "$P_ORG" "$P_POOL" 'tikv_thread_cpu_seconds_total' | jq '.data|length')
  [ "${NP:-0}" -gt 0 ] \
    && pass "premium: tikv_* present under pool ID ($NP series)" \
    || fail "premium: tikv_* empty under pool ID"

  LK=$(api -G "${EP}/data-proxy/api/v1/orgs/${P_ORG}/clusters/${P_ID}/logs/query-range" \
    --data-urlencode 'query={component="tidb"}' \
    --data-urlencode "start=${NS_1H}" --data-urlencode "end=${NS_NOW}" \
    --data-urlencode "limit=5")
  LN=$(echo "$LK" | jq '.data.result|length' 2>/dev/null)
  if [ "${LN:-0}" -gt 0 ]; then
    BAD=$(echo "$LK" | jq -r --arg id "$P_ID" '[.data.result[].stream.tidb_cluster_id]|map(select(. != $id))|length')
    [ "$BAD" = "0" ] \
      && pass "premium: loki {component=\"tidb\"} streams labeled tidb_cluster_id=<logical>" \
      || fail "premium: loki {component=\"tidb\"} returned foreign tidb_cluster_id — selector guidance drifted"
  else
    skip "premium: no tidb log lines in 1h window — loki label check"
  fi
else
  skip "premium checks (no sample)"
fi

# ── Essential / Starter: shared-plane hazards ───────────────────────
if [ -n "$ESS" ]; then
  E_ID=$(rec "$ESS" .clusterID); E_ORG=$(rec "$ESS" .tenantID)

  EM=$(api "${EP}/data-proxy/api/v1/clusters/${E_ID}")
  EPID=$(echo "$EM" | jq -r '.diagnosticContext.parentId // empty')
  if [ -z "$EPID" ]; then
    pass "essential: no parentID (regional multi-tenant pool, no Clinic pool record)"
  else
    warn "essential: sample carries parentId=$EPID — pool-hosted Essential observed; apply Premium routing rules to such clusters (docs already cover this)"
  fi

  ES=$(series "$E_ORG" "$E_ID" 'tidb_server_query_total')
  EN=$(echo "$ES" | jq '.data|length')
  KSID=""
  if [ "${EN:-0}" -gt 0 ]; then
    KSID=$(echo "$ES" | jq -r '.data[0].keyspace_id // empty')
    CPI=$(echo "$ES"  | jq -r '.data[0].control_plane_info // empty')
    { [ -n "$KSID" ] && [ -n "$CPI" ]; } \
      && pass "essential: tidb_* series carry keyspace_id + control_plane_info" \
      || fail "essential: tidb_* label schema drifted (keyspace_id='$KSID' control_plane_info='$CPI')"
  else
    skip "essential: no tidb_* series (scaled-to-zero sample) — label check"
  fi

  TN=$(series "$E_ORG" "$E_ID" 'tikv_thread_cpu_seconds_total' | jq '.data|length')
  if [ "${TN:-0}" -gt 100 ]; then
    pass "essential: tikv_* via cluster route is UNSCOPED pool-wide noise ($TN series) — never report as cluster data"
  elif [ "${TN:-0}" -eq 0 ]; then
    warn "essential: tikv_* via cluster route now returns 0 series — behavior changed vs 2026-08-06 baseline (was ~14k unscoped); re-verify docs"
  else
    fail "essential: tikv_* returned $TN series — scoping behavior changed; the 'cross-tenant noise' claim needs re-verification"
  fi

  EC=$(api "${EP}/data-proxy/api/v1/orgs/${E_ORG}/clusters/${E_ID}/components")
  echo "$EC" | jq -e '.code=="InvalidArgument"' >/dev/null 2>&1 \
    && pass "essential: components API unsupported (InvalidArgument), as documented" \
    || warn "essential: components API response changed: $(echo "$EC" | head -c 120)"

  PROV=$(rec "$ESS" .clusterProviderName); REG=$(rec "$ESS" .clusterRegionName)
  PROJ=$(rec "$ESS" .projectID)
  NGM=$(curl -s --max-time 30 -o /tmp/ngm_probe.$$ -w '%{http_code}' -G "${EP}/ngm/api/v1/slow_query/list" \
    -H "Authorization: Bearer ${TOKEN}" -H "x-provider: ${PROV}" -H "x-region: ${REG}" \
    -H "x-org-id: ${E_ORG}" -H "x-project-id: ${PROJ}" -H "x-cluster-id: ${E_ID}" \
    -H "x-deploy-type: shared" \
    --data-urlencode "begin_time=$((NOW-21600))" --data-urlencode "end_time=${NOW}" \
    --data-urlencode "fields=query,timestamp,query_time,digest" \
    --data-urlencode "limit=1")
  if [ "$NGM" = "200" ] && jq -e 'type=="array" or (type=="object" and has("data"))' /tmp/ngm_probe.$$ >/dev/null 2>&1; then
    pass "essential: NGM slow-query route works with x-deploy-type: shared"
  else
    fail "essential: NGM slow-query probe HTTP $NGM — routing claim drifted"
  fi
  rm -f /tmp/ngm_probe.$$

  if [ -n "$KSID" ]; then
    PC=$(mquery "$E_ORG" "$E_ID" "sum(serverless_metering_rcu_provisioned_capacity{keyspace_id=\"${KSID}\"})" | jq '.data.result|length')
    [ "${PC:-0}" -ge 1 ] \
      && pass "essential: RCU provisioned metric reachable via cluster route" \
      || warn "essential: RCU provisioned metric empty for keyspace ${KSID} (mode-dependent)"
    SL=$(series "$E_ORG" "$E_ID" 'resource_manager_resource_unit_service_limit' | jq '.data|length')
    [ "${SL:-0}" -eq 0 ] \
      && pass "essential: service_limit metric still removed (rcu-check.md claim holds)" \
      || fail "essential: resource_manager_resource_unit_service_limit REAPPEARED ($SL series) — update rcu-check.md"
  else
    skip "essential: RCU metric checks (no keyspace_id from idle sample)"
  fi
else
  skip "essential checks (no sample)"
fi

# ── Dedicated: classic scoping control group ────────────────────────
if [ -n "$DED" ]; then
  D_ID=$(rec "$DED" .clusterID); D_ORG=$(rec "$DED" .tenantID)
  DC=$(api "${EP}/data-proxy/api/v1/orgs/${D_ORG}/clusters/${D_ID}/components" | jq -r 'if type=="array" then [.[].component_type]|join(",") else "ERR" end')
  case "$DC" in
    *tikv*|*TIKV*) pass "dedicated: components return full topology ($DC)" ;;
    *)             fail "dedicated: components missing TiKV ($DC)" ;;
  esac
  DS=$(mquery "$D_ORG" "$D_ID" 'count(tikv_store_size_bytes{type="used"})' | jq -r '.data.result[0].value[1] // "0"')
  if [ "${DS%.*}" -gt 0 ] 2>/dev/null && [ "${DS%.*}" -lt 100 ] 2>/dev/null; then
    pass "dedicated: tikv_store_size_bytes cluster-scoped ($DS series)"
  else
    fail "dedicated: storage series count '$DS' outside sane per-cluster range — scoping control group broken"
  fi
else
  skip "dedicated checks (no sample)"
fi

echo
echo "== summary: PASS=$PASS FAIL=$FAIL WARN=$WARN SKIP=$SKIP =="
[ "$FAIL" -eq 0 ] || { echo "FAILures mean platform behavior drifted from documented claims — re-verify tidb-x-o11y-routing.md before trusting the docs."; exit 1; }
