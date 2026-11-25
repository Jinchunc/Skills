# Canonical Data Proxy SQL and Top RU helpers (data-proxy sub-skill).
# Requires: clinic-auth.sh and cluster-metadata/scripts/clinic-cluster.sh.

clinic_dataproxy_query() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_dataproxy_query <cluster_id> <sql> [timeout_seconds]" >&2
        return 2
    fi

    local cluster_id="$1"
    local sql="$2"
    local timeout="${3:-60}"

    _ensure_clinic_auth || return 1

    local org_id encoded_cluster_id
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    local payload
    payload=$(python3 - "$sql" "$timeout" <<'PY'
import json
import sys

sql, timeout = sys.argv[1], int(sys.argv[2])
print(json.dumps({"sql": sql, "timeout": timeout}))
PY
) || return 1

    curl -s -X POST "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/sql-queries" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "$payload"
}

clinic_dataproxy_batch_query() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_dataproxy_batch_query <cluster_id> <queries_json>" >&2
        echo 'queries_json example: [{"sql":"SELECT ...","timeout":60}]' >&2
        return 2
    fi

    local cluster_id="$1"
    local queries_json="$2"
    local org_id encoded_cluster_id

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    printf '%s' "$queries_json" | curl -s -X POST \
        "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/sql-query-batches" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "Content-Type: application/json" \
        -d @-
}

clinic_dataproxy_schema() {
    if [ $# -lt 1 ]; then
        echo "usage: clinic_dataproxy_schema <cluster_id> [tables]" >&2
        return 2
    fi

    local cluster_id="$1"
    local tables="${2:-slow_query_logs}"

    _ensure_clinic_auth || return 1

    local org_id encoded_cluster_id
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/table-schemas" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "tables=${tables}"
}

clinic_dataproxy_capabilities() {
    _ensure_clinic_auth || return 1
    curl -s "${CLINIC_ENDPOINT}/data-proxy/api/v1/capabilities" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_statements_query() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_statements_query <cluster_id> <YYYY-MM-DD> [limit] [timeout_seconds]" >&2
        return 2
    fi

    local cluster_id="$1"
    local date="$2"
    local limit="${3:-100}"
    local timeout="${4:-120}"

    case "$date" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
        *)
            echo "statements date must be YYYY-MM-DD, for example 2026-07-22" >&2
            return 2
            ;;
    esac
    case "$limit" in
        ''|*[!0-9]*)
            echo "statements limit must be a non-negative integer" >&2
            return 2
            ;;
    esac

    local sql
    sql="SELECT * FROM clinic_data_proxy.statements WHERE date = '${date}' LIMIT ${limit}"
    clinic_dataproxy_query "$cluster_id" "$sql" "$timeout"
}

clinic_top_ru_summary() {
    if [ $# -lt 4 ]; then
        echo "usage: clinic_top_ru_summary <cluster_id> <start> <end> <top> [order_by] [data_source] [step] [user]" >&2
        return 2
    fi

    local cluster_id="$1"
    local start="$2"
    local end="$3"
    local top="$4"
    local order_by="${5:-}"
    local data_source="${6:-}"
    local step="${7:-}"
    local user="${8:-}"
    local user_present="false"
    [ $# -ge 8 ] && user_present="true"

    local org_id encoded_cluster_id url
    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1
    url=$(python3 - \
        "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/top-ru/summary" \
        "$start" "$end" "$top" "$order_by" "$data_source" "$step" "$user" "$user_present" <<'PY'
import sys
import urllib.parse

base, start, end, top, order_by, data_source, step, user, user_present = sys.argv[1:]
params = {"start": start, "end": end, "top": top}
for key, value in (("order_by", order_by), ("data_source", data_source), ("step", step)):
    if value != "":
        params[key] = value
if user_present == "true":
    params["user"] = user
print(base + "?" + urllib.parse.urlencode(params))
PY
) || return 1

    curl -s "$url" -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_top_ru_user_stats() {
    if [ $# -lt 3 ]; then
        echo "usage: clinic_top_ru_user_stats <cluster_id> <start> <end> [top]" >&2
        return 2
    fi

    local cluster_id="$1"
    local start="$2"
    local end="$3"
    local top="${4:-}"
    local org_id encoded_cluster_id url

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1
    url=$(python3 - \
        "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/top-ru/user-stats" \
        "$start" "$end" "$top" <<'PY'
import sys
import urllib.parse

base, start, end, top = sys.argv[1:]
params = {"start": start, "end": end}
if top != "":
    params["top"] = top
print(base + "?" + urllib.parse.urlencode(params))
PY
) || return 1

    curl -s "$url" -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_legacy_dataproxy_query() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_legacy_dataproxy_query <cluster_id> <sql> [timeout_seconds]" >&2
        return 2
    fi

    local cluster_id="$1"
    local sql="$2"
    local timeout="${3:-60}"

    _ensure_clinic_auth || return 1
    local payload
    payload=$(python3 - "$cluster_id" "$sql" "$timeout" <<'PY'
import json
import sys

cluster_id, sql, timeout = sys.argv[1], sys.argv[2], int(sys.argv[3])
print(json.dumps({"clusterId": cluster_id, "sql": sql, "timeout": timeout}))
PY
) || return 1

    curl -s -X POST "${CLINIC_ENDPOINT}/data-proxy/query" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "$payload"
}

clinic_legacy_dataproxy_batch_query() {
    if [ $# -lt 1 ]; then
        echo "usage: clinic_legacy_dataproxy_batch_query <legacy_queries_json>" >&2
        return 2
    fi

    _ensure_clinic_auth || return 1
    printf '%s' "$1" | curl -s -X POST "${CLINIC_ENDPOINT}/data-proxy/batch-query" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "Content-Type: application/json" \
        -d @-
}

clinic_legacy_dataproxy_schema() {
    local cluster_id="$1"
    local tables="${2:-slow_query_logs,topsql,log_stats}"

    _ensure_clinic_auth || return 1
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/schema" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "clusterId=${cluster_id}" \
        --data-urlencode "tables=${tables}"
}

clinic_legacy_dataproxy_info() {
    _ensure_clinic_auth || return 1
    curl -s "${CLINIC_ENDPOINT}/data-proxy/info" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

_clinic_ngm_normalize_deploy_type() {
    local deploy
    deploy=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr ' ' '_')
    case "$deploy" in
        starter|essential|shared)
            printf 'shared'
            ;;
        *)
            printf '%s' "$deploy"
            ;;
    esac
}

_clinic_slow_query_ngm_context_from_cluster_id() {
    local cluster_id="$1"

    local search_result
    search_result=$(clinic_legacy_search_cluster_by_id "$cluster_id" "true")
    local item
    item=$(echo "$search_result" | jq -c --arg id "$cluster_id" '[.items[] | select(.clusterID == $id)][0] // empty')
    if [ -z "$item" ] || [ "$item" = "null" ]; then
        echo "Cluster not found: $cluster_id" >&2
        return 1
    fi

    local clinic_org_id ngm_x_org_id project_id deploy provider region
    clinic_org_id=$(echo "$item" | jq -r '.orgID // empty')
    ngm_x_org_id=$(echo "$item" | jq -r '.tenantID // empty')
    project_id=$(echo "$item" | jq -r '.projectID // .projectId // empty')
    # NGM slow_query accepts x-deploy-type=shared for starter/essential/shared.
    # Prefer clusterDeployType/deployType; fall back to V2 only when the NGM value is absent.
    deploy=$(_clinic_ngm_normalize_deploy_type "$(echo "$item" | jq -r '.clusterDeployType // .deployType // .clusterDeployTypeV2 // .deployTypeV2 // empty')")
    provider=$(echo "$item" | jq -r '(.cloudProvider // .provider // .vendor // .clusterProviderName // empty) | ascii_downcase')
    region=$(echo "$item" | jq -r '.region // .clusterRegionName // empty')

    local needs_detail="false"
    if [ -z "$deploy" ] || [ "$deploy" = "null" ] || \
        [ -z "$ngm_x_org_id" ] || [ "$ngm_x_org_id" = "null" ] || \
        [ -z "$project_id" ] || [ "$project_id" = "null" ] || \
        [ -z "$region" ] || [ "$region" = "null" ] || \
        [ -z "$provider" ] || [ "$provider" = "null" ]; then
        needs_detail="true"
    fi
    if [ "$needs_detail" = "true" ] && { [ -z "$clinic_org_id" ] || [ "$clinic_org_id" = "null" ]; }; then
        echo "Could not resolve orgID (Clinic REST) for cluster $cluster_id" >&2
        return 1
    fi
    if [ "$needs_detail" = "true" ]; then
        local detail
        detail=$(clinic_legacy_get_cluster "$clinic_org_id" "$cluster_id")
        [ -z "$ngm_x_org_id" ] || [ "$ngm_x_org_id" = "null" ] && ngm_x_org_id=$(echo "$detail" | jq -r '.tenantID // empty')
        [ -z "$deploy" ] || [ "$deploy" = "null" ] && deploy=$(_clinic_ngm_normalize_deploy_type "$(echo "$detail" | jq -r '.clusterDeployType // .deployType // .clusterDeployTypeV2 // .deployTypeV2 // empty')")
        [ -z "$project_id" ] || [ "$project_id" = "null" ] && project_id=$(echo "$detail" | jq -r '.projectID // .projectId // empty')
        [ -z "$region" ] || [ "$region" = "null" ] && region=$(echo "$detail" | jq -r '.region // .clusterRegionName // empty')
        [ -z "$provider" ] || [ "$provider" = "null" ] && provider=$(echo "$detail" | jq -r '(.cloudProvider // .provider // .vendor // .clusterProviderName // empty) | ascii_downcase')
    fi

    if [ -z "$ngm_x_org_id" ] || [ "$ngm_x_org_id" = "null" ]; then
        echo "Could not resolve tenantID for NGM x-org-id (cluster $cluster_id)" >&2
        return 1
    fi
    if [ -z "$project_id" ] || [ "$project_id" = "null" ]; then
        echo "Could not resolve x-project-id for cluster $cluster_id" >&2
        return 1
    fi
    if [ -z "$region" ] || [ "$region" = "null" ]; then
        echo "Could not resolve x-region for cluster $cluster_id" >&2
        return 1
    fi
    if [ -z "$provider" ] || [ "$provider" = "null" ]; then
        echo "Could not resolve x-provider for cluster $cluster_id" >&2
        return 1
    fi
    if [ -z "$deploy" ] || [ "$deploy" = "null" ]; then
        echo "Could not resolve x-deploy-type (clusterDeployType) for cluster $cluster_id" >&2
        return 1
    fi

    printf '%s|%s|%s|%s|%s' "$ngm_x_org_id" "$project_id" "$deploy" "$provider" "$region"
}

clinic_ngm_slow_query_list() {
    if [ $# -lt 3 ]; then
        echo "usage: clinic_ngm_slow_query_list <cluster_id> <begin_unix_sec> <end_unix_sec> [limit] [orderBy] [desc] [fields] [text] [digest] [show_internal]" >&2
        return 2
    fi

    local cluster_id="$1"
    local begin_time="$2"
    local end_time="$3"
    local limit="${4:-100}"
    local order_by="${5:-query_time}"
    local desc="${6:-true}"
    local fields="${7:-query,timestamp,query_time,memory_max,digest,index_names}"
    local text="${8:-}"
    local digest="${9:-}"
    local show_internal="${10:-true}"

    _ensure_clinic_auth || return 1
    local ctx
    ctx=$(_clinic_slow_query_ngm_context_from_cluster_id "$cluster_id") || return 1
    local org_id project_id deploy provider region
    org_id=$(echo "$ctx" | cut -d'|' -f1)
    project_id=$(echo "$ctx" | cut -d'|' -f2)
    deploy=$(echo "$ctx" | cut -d'|' -f3)
    provider=$(echo "$ctx" | cut -d'|' -f4)
    region=$(echo "$ctx" | cut -d'|' -f5)

    curl -s -G "${CLINIC_ENDPOINT}/ngm/api/v1/slow_query/list" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "x-cluster-id: ${cluster_id}" \
        -H "x-org-id: ${org_id}" \
        -H "x-project-id: ${project_id}" \
        -H "x-deploy-type: ${deploy}" \
        -H "x-provider: ${provider}" \
        -H "x-region: ${region}" \
        --data-urlencode "begin_time=${begin_time}" \
        --data-urlencode "end_time=${end_time}" \
        --data-urlencode "fields=${fields}" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "orderBy=${order_by}" \
        --data-urlencode "desc=${desc}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "digest=${digest}" \
        --data-urlencode "show_internal=${show_internal}"
}

_clinic_log_stats_validate_date() {
    local date="$1"
    case "${date}" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9])
            return 0
            ;;
        *)
            echo "log_stats date must be YYYYMMDD, for example 20260618" >&2
            return 2
            ;;
    esac
}

clinic_log_stats_schema() {
    if [ $# -lt 1 ]; then
        echo "usage: clinic_log_stats_schema <cluster_id>" >&2
        return 2
    fi

    clinic_dataproxy_schema "$1" "log_stats"
}

clinic_log_stats_summary() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_log_stats_summary <cluster_id> <YYYYMMDD> [limit] [timeout_seconds]" >&2
        return 2
    fi

    local cluster_id="$1"
    local date="$2"
    local limit="${3:-50}"
    local timeout="${4:-120}"
    _clinic_log_stats_validate_date "$date" || return $?

    local sql
    sql=$(python3 - "$date" "$limit" <<'PY'
import sys

date, limit = sys.argv[1], int(sys.argv[2])
print(f"""SELECT
  level,
  component,
  SUM(count) AS total_count
FROM log_stats
WHERE date = {date}
GROUP BY level, component
ORDER BY total_count DESC
LIMIT {limit}""")
PY
)
    clinic_dataproxy_query "$cluster_id" "$sql" "$timeout"
}

clinic_log_stats_top() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_log_stats_top <cluster_id> <YYYYMMDD> [limit] [timeout_seconds]" >&2
        return 2
    fi

    local cluster_id="$1"
    local date="$2"
    local limit="${3:-50}"
    local timeout="${4:-120}"
    _clinic_log_stats_validate_date "$date" || return $?

    local sql
    sql=$(python3 - "$date" "$limit" <<'PY'
import sys

date, limit = sys.argv[1], int(sys.argv[2])
print(f"""SELECT
  level,
  component,
  file,
  line,
  msg_digest,
  sample,
  SUM(count) AS total_count
FROM log_stats
WHERE date = {date}
GROUP BY level, component, file, line, msg_digest, sample
ORDER BY total_count DESC
LIMIT {limit}""")
PY
)
    clinic_dataproxy_query "$cluster_id" "$sql" "$timeout"
}

clinic_log_stats_search() {
    if [ $# -lt 3 ]; then
        echo "usage: clinic_log_stats_search <cluster_id> <YYYYMMDD> <pattern> [limit] [timeout_seconds]" >&2
        return 2
    fi

    local cluster_id="$1"
    local date="$2"
    local pattern="$3"
    local limit="${4:-50}"
    local timeout="${5:-120}"
    _clinic_log_stats_validate_date "$date" || return $?

    local sql
    sql=$(python3 - "$date" "$pattern" "$limit" <<'PY'
import sys

date, pattern, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
escaped = pattern.replace("\\", "\\\\").replace("'", "''")
like = f"%{escaped}%"
print(f"""SELECT
  level,
  component,
  file,
  line,
  msg_digest,
  sample,
  SUM(count) AS total_count
FROM log_stats
WHERE date = {date}
  AND (
    sample LIKE '{like}'
    OR msg_digest LIKE '{like}'
    OR file LIKE '{like}'
    OR component LIKE '{like}'
  )
GROUP BY level, component, file, line, msg_digest, sample
ORDER BY total_count DESC
LIMIT {limit}""")
PY
)
    clinic_dataproxy_query "$cluster_id" "$sql" "$timeout"
}
