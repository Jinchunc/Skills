# Canonical cluster Metrics helpers.
# Requires: clinic-auth.sh and cluster-metadata/scripts/clinic-cluster.sh.

clinic_metrics_query() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_metrics_query <cluster_id> <promql> [time] [timeout]" >&2
        return 2
    fi

    local cluster_id="$1"
    local query="$2"
    local time="${3:-}"
    local timeout="${4:-}"
    local org_id encoded_cluster_id

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/metrics/query" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}"
    [ -n "$time" ] && set -- "$@" --data-urlencode "time=${time}"
    [ -n "$timeout" ] && set -- "$@" --data-urlencode "timeout=${timeout}"
    curl "$@"
}

clinic_metrics_query_range() {
    if [ $# -lt 5 ]; then
        echo "usage: clinic_metrics_query_range <cluster_id> <promql> <start> <end> <step> [timeout]" >&2
        return 2
    fi

    local cluster_id="$1"
    local query="$2"
    local start="$3"
    local end="$4"
    local step="$5"
    local timeout="${6:-}"
    local org_id encoded_cluster_id

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/metrics/query-range" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}" \
        --data-urlencode "step=${step}"
    [ -n "$timeout" ] && set -- "$@" --data-urlencode "timeout=${timeout}"
    curl "$@"
}

clinic_metrics_series() {
    if [ $# -lt 4 ]; then
        echo "usage: clinic_metrics_series <cluster_id> <match[]> <start> <end>" >&2
        return 2
    fi

    local cluster_id="$1"
    local match="$2"
    local start="$3"
    local end="$4"
    local org_id encoded_cluster_id

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/metrics/series" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "match[]=${match}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}"
}

clinic_legacy_metrics_query() {
    local cluster_id="$1"
    local query="$2"
    local time="${3:-}"
    local timeout="${4:-}"

    _ensure_clinic_auth || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/metrics/${cluster_id}/api/v1/query" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}"
    [ -n "$time" ] && set -- "$@" --data-urlencode "time=${time}"
    [ -n "$timeout" ] && set -- "$@" --data-urlencode "timeout=${timeout}"
    curl "$@"
}

clinic_legacy_metrics_query_range() {
    local cluster_id="$1"
    local query="$2"
    local start="$3"
    local end="$4"
    local step="$5"
    local timeout="${6:-}"

    _ensure_clinic_auth || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/metrics/${cluster_id}/api/v1/query_range" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}" \
        --data-urlencode "step=${step}"
    [ -n "$timeout" ] && set -- "$@" --data-urlencode "timeout=${timeout}"
    curl "$@"
}

clinic_legacy_metrics_series() {
    local cluster_id="$1"
    local match="$2"
    local start="$3"
    local end="$4"

    _ensure_clinic_auth || return 1
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/metrics/${cluster_id}/api/v1/series" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "match[]=${match}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}"
}
