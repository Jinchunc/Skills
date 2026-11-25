#!/usr/bin/env bash
# loki-triage.sh — staged log triage (loki/SUBSKILL.md investigation order)
# with tier-correct selectors and pattern aggregation.
#
# Runs the documented ladder per component: panic|fatal → [error] → [warn],
# reports hit counts per stage, and aggregates the most severe non-empty
# stage into patterns (normalized line, count, first/last seen, sample) so
# the caller never has to page raw logs.
#
# Selectors come from resolve-cluster-routing.sh:
#   dedicated : {container="<comp>"}
#   pooled    : {component="<comp>", tidb_cluster_id=<logical|pool>} —
#               tidb/tiproxy scoped to the logical cluster, storage
#               components to the pool (bare container= is unsafe there)
#   regional  : serverless-parent-logs (POOL-LEVEL); if keyspace_id is
#               known it is applied as a best-effort content filter
#
# Usage:
#   CLINIC_API_KEY=... bash loki-triage.sh <CLUSTER_ID> [window_sec=7200] [components]
#   components default: "tidb,tikv,pd"

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
EP="${CLINIC_ENDPOINT:-https://clinic.pingcap.com}"
TOKEN="${CLINIC_API_KEY:-${CLINIC_API_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "ERROR: set CLINIC_API_KEY or CLINIC_API_TOKEN" >&2; exit 2; }
CID="${1:-}"; [ -n "$CID" ] || { echo "usage: loki-triage.sh <CLUSTER_ID> [window_sec] [components]" >&2; exit 2; }
WIN="${2:-7200}"; COMPS="${3:-tidb,tikv,pd}"
LIMIT=1000

R=$(bash "${SCRIPT_DIR}/resolve-cluster-routing.sh" "$CID") || exit 3
CLASS=$(echo "$R" | jq -r .routingClass)
ORG=$(echo "$R" | jq -r .orgId); POOL=$(echo "$R" | jq -r '.poolId // empty')
KSID=$(echo "$R" | jq -r '.keyspaceId // empty')
CPI=$(echo "$R" | jq -r '.controlPlaneInfo // empty')
NOW=$(date +%s); SNS="$((NOW-WIN))000000000"; ENS="${NOW}000000000"

api() { curl -s --max-time 60 -H "Authorization: Bearer ${TOKEN}" "$@"; }

qrange() { # logql -> loki response (route depends on class)
  case "$CLASS" in
    regional)
      local cpi="$CPI"; case "$cpi" in */*/*) ;; */*) cpi="${cpi}/devtier-infra" ;; esac
      api -G "${EP}/data-proxy/serverless-parent-logs/api/v1/query_range" \
        --data-urlencode "control_plane_info=${cpi}" \
        --data-urlencode "query=$1" \
        --data-urlencode "start=${SNS}" --data-urlencode "end=${ENS}" \
        --data-urlencode "limit=${LIMIT}" ;;
    *)
      api -G "${EP}/data-proxy/api/v1/orgs/${ORG}/clusters/${CID}/logs/query-range" \
        --data-urlencode "query=$1" \
        --data-urlencode "start=${SNS}" --data-urlencode "end=${ENS}" \
        --data-urlencode "limit=${LIMIT}" ;;
  esac
}

selector() { # component -> stream selector
  local c="$1"
  case "$CLASS" in
    dedicated) printf '{container="%s"}' "$c" ;;
    pooled)
      case "$c" in
        tidb|tiproxy|slowlog) printf '{component="%s", tidb_cluster_id="%s"}' "$c" "$CID" ;;
        *) printf '{component="%s", tidb_cluster_id="%s"}' "$c" "$POOL" ;;
      esac ;;
    regional)
      if [ -n "$KSID" ]; then printf '{container="%s"} |= `%s`' "$c" "$KSID"
      else printf '{container="%s"}' "$c"; fi ;;
  esac
}

# Aggregate loki result -> "count<TAB>first<TAB>last<TAB>pattern<TAB>sample"
aggregate() {
  jq -r '.data.result[]?.values[]? | "\(.[0])\t\(.[1])"' 2>/dev/null |
  awk -F'\t' '
    {
      ts=$1; line=$2
      p=line
      gsub(/[0-9]{4}[\/-][0-9]{2}[\/-][0-9]{2}[T ][0-9:.,+Z]*/,"<TS>",p)
      gsub(/[0-9a-fA-F]{8,}/,"<HEX>",p)
      gsub(/[0-9]+(\.[0-9]+)?/,"<N>",p)
      if (length(p)>220) p=substr(p,1,220)
      cnt[p]++
      if (!(p in first) || ts<first[p]) first[p]=ts
      if (!(p in last)  || ts>last[p])  last[p]=ts
      if (!(p in sample)) sample[p]=substr(line,1,220)
    }
    END { for (p in cnt) printf "%d\t%s\t%s\t%s\t%s\n", cnt[p], first[p], last[p], p, sample[p] }' |
  sort -rn | head -15
}

ns2t() { date -d "@${1:0:10}" '+%m-%d %H:%M' 2>/dev/null || echo "$1"; }

echo "# Loki triage — ${CID} (routing class: ${CLASS}; window: last $((WIN/60))min)"
[ "$CLASS" = "regional" ] && echo "> NOTE: regional plane = POOL-LEVEL logs${KSID:+, content-filtered by keyspace ${KSID} (best-effort)}."
echo

STAGES='panic|fatal;(?i)\[error\];(?i)\[warn(ing)?\]'
IFS=',' read -ra CARR <<< "$COMPS"
for c in "${CARR[@]}"; do
  SEL=$(selector "$c")
  echo "## ${c}  —  selector: \`${SEL}\`"
  best=""; bestname=""
  i=0
  for st in "panic|fatal" '(?i)\[error\]' '(?i)\[warn(ing)?\]'; do
    i=$((i+1))
    case $i in 1) name="panic/fatal";; 2) name="error";; 3) name="warn";; esac
    RESP=$(qrange "${SEL} |~ \`${st}\`")
    N=$(echo "$RESP" | jq '[.data.result[]?.values[]?]|length' 2>/dev/null || echo "ERR")
    echo "- stage ${i} (${name}): ${N:-0} lines"
    if [ -z "$best" ] && [ "${N:-0}" != "ERR" ] && [ "${N:-0}" -gt 0 ] 2>/dev/null; then
      best="$RESP"; bestname="$name"
    fi
  done
  if [ -n "$best" ]; then
    echo
    echo "Top patterns (most severe non-empty stage: ${bestname}):"
    echo
    echo "| count | first | last | sample |"
    echo "|---|---|---|---|"
    echo "$best" | aggregate | while IFS=$'\t' read -r n f l p s; do
      printf '| %s | %s | %s | %s |\n' "$n" "$(ns2t "$f")" "$(ns2t "$l")" "$(echo "$s" | sed 's/|/\\|/g')"
    done
  else
    echo "  (no panic/error/warn lines in window)"
  fi
  echo
done
echo "Raw-stream step 4 ({component/container only}) intentionally not run — do it manually only if the ladder found nothing and you still need full context."
