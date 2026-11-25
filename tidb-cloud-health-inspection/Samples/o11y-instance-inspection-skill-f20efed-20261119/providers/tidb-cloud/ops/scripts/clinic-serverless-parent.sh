# Serverless-parent (Starter/Essential pool-level) metrics & logs helpers.
# Requires: clinic-auth.sh.
#
# These wrap /data-proxy/serverless-parent-metrics/api/v1/* and
# /data-proxy/serverless-parent-logs/api/v1/* — the ONLY sanctioned way to
# read TiKV/PD-layer metrics and raw logs for Starter/Essential clusters
# (the cluster-scoped routes return unscoped cross-tenant data; see
# ../references/tidb-x-o11y-routing.md).
#
# control_plane_info format: projectID/vendor-region[/applicationID], e.g.
#   us-east-1-f04/aws-us-east-1/devtier-infra
# The two-segment form found on a cluster's tidb_* metric labels usually
# needs "/devtier-infra" appended; _clinic_parent_cpi does this when the
# third segment is missing. Results are POOL-LEVEL (all tenants) — never
# report them as one cluster's data.

_clinic_parent_cpi() {
    # Normalize control_plane_info: append default applicationID if absent.
    local cpi="$1"
    case "$cpi" in
        */*/*) printf '%s' "$cpi" ;;
        */*)   printf '%s/devtier-infra' "$cpi" ;;
        *)     echo "invalid control_plane_info: $cpi" >&2; return 2 ;;
    esac
}

clinic_parent_metrics_query() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_parent_metrics_query <control_plane_info> <promql> [time]" >&2
        return 2
    fi
    local cpi query="$2" time="${3:-}"
    _ensure_clinic_auth || return 1
    cpi=$(_clinic_parent_cpi "$1") || return 2
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/serverless-parent-metrics/api/v1/query" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "control_plane_info=${cpi}" \
        --data-urlencode "query=${query}"
    [ -n "$time" ] && set -- "$@" --data-urlencode "time=${time}"
    curl "$@"
}

clinic_parent_metrics_query_range() {
    if [ $# -lt 5 ]; then
        echo "usage: clinic_parent_metrics_query_range <control_plane_info> <promql> <start> <end> <step>" >&2
        return 2
    fi
    local cpi
    _ensure_clinic_auth || return 1
    cpi=$(_clinic_parent_cpi "$1") || return 2
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/serverless-parent-metrics/api/v1/query_range" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "control_plane_info=${cpi}" \
        --data-urlencode "query=$2" \
        --data-urlencode "start=$3" \
        --data-urlencode "end=$4" \
        --data-urlencode "step=$5"
}

clinic_parent_metrics_series() {
    if [ $# -lt 4 ]; then
        echo "usage: clinic_parent_metrics_series <control_plane_info> <match[]> <start> <end>" >&2
        return 2
    fi
    local cpi
    _ensure_clinic_auth || return 1
    cpi=$(_clinic_parent_cpi "$1") || return 2
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/serverless-parent-metrics/api/v1/series" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "control_plane_info=${cpi}" \
        --data-urlencode "match[]=$2" \
        --data-urlencode "start=$3" \
        --data-urlencode "end=$4"
}

clinic_parent_logs_query_range() {
    # start/end are NANOSECONDS. LogQL must contain a stream selector.
    if [ $# -lt 4 ]; then
        echo "usage: clinic_parent_logs_query_range <control_plane_info> <logql> <start_ns> <end_ns> [limit] [direction]" >&2
        return 2
    fi
    local cpi limit="${5:-100}" direction="${6:-backward}"
    _ensure_clinic_auth || return 1
    cpi=$(_clinic_parent_cpi "$1") || return 2
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/serverless-parent-logs/api/v1/query_range" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "control_plane_info=${cpi}" \
        --data-urlencode "query=$2" \
        --data-urlencode "start=$3" \
        --data-urlencode "end=$4" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "direction=${direction}"
}

clinic_parent_logs_labels() {
    if [ $# -lt 3 ]; then
        echo "usage: clinic_parent_logs_labels <control_plane_info> <start_ns> <end_ns>" >&2
        return 2
    fi
    local cpi
    _ensure_clinic_auth || return 1
    cpi=$(_clinic_parent_cpi "$1") || return 2
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/serverless-parent-logs/api/v1/labels" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "control_plane_info=${cpi}" \
        --data-urlencode "start=$2" \
        --data-urlencode "end=$3"
}

clinic_parent_logs_label_values() {
    if [ $# -lt 4 ]; then
        echo "usage: clinic_parent_logs_label_values <control_plane_info> <label> <start_ns> <end_ns>" >&2
        return 2
    fi
    local cpi
    _ensure_clinic_auth || return 1
    cpi=$(_clinic_parent_cpi "$1") || return 2
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/serverless-parent-logs/api/v1/label/$2/values" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "control_plane_info=${cpi}" \
        --data-urlencode "start=$3" \
        --data-urlencode "end=$4"
}

clinic_parent_logs_series() {
    if [ $# -lt 4 ]; then
        echo "usage: clinic_parent_logs_series <control_plane_info> <match[]> <start_ns> <end_ns>" >&2
        return 2
    fi
    local cpi
    _ensure_clinic_auth || return 1
    cpi=$(_clinic_parent_cpi "$1") || return 2
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/serverless-parent-logs/api/v1/series" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "control_plane_info=${cpi}" \
        --data-urlencode "match[]=$2" \
        --data-urlencode "start=$3" \
        --data-urlencode "end=$4"
}
