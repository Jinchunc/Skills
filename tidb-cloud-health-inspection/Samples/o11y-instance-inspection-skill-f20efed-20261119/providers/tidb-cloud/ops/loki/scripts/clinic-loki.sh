# Canonical cluster and system Logs helpers.
# Requires: clinic-auth.sh and cluster-metadata/scripts/clinic-cluster.sh.

clinic_loki_query_range() {
    if [ $# -lt 4 ]; then
        echo "usage: clinic_loki_query_range <cluster_id> <logql> <start> <end> [limit] [direction]" >&2
        return 2
    fi

    local cluster_id="$1"
    local query="$2"
    local start="$3"
    local end="$4"
    local limit="${5:-100}"
    local direction="${6:-backward}"
    local org_id encoded_cluster_id

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/logs/query-range" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "direction=${direction}"
}

clinic_loki_labels() {
    local cluster_id="$1"
    local start="${2:-}"
    local end="${3:-}"
    local org_id encoded_cluster_id

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/logs/labels" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_loki_label_values() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_loki_label_values <cluster_id> <label_name> [start] [end]" >&2
        return 2
    fi

    local cluster_id="$1"
    local label_name="$2"
    local start="${3:-}"
    local end="${4:-}"
    local org_id encoded_cluster_id encoded_label_name

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1
    encoded_label_name=$(_clinic_path_segment "$label_name") || return 1

    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/logs/labels/${encoded_label_name}/values" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_loki_series() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_loki_series <cluster_id> <match[]> [start] [end]" >&2
        return 2
    fi

    local cluster_id="$1"
    local match="$2"
    local start="${3:-}"
    local end="${4:-}"
    local org_id encoded_cluster_id

    _ensure_clinic_auth || return 1
    org_id=$(_clinic_canonical_org_id_from_cluster_id "$cluster_id") || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/logs/series" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "match[]=${match}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

# Loki instant query has no canonical alias in the current API set.
clinic_legacy_loki_query() {
    local cluster_id="$1"
    local query="$2"
    local time="${3:-}"
    local limit="${4:-100}"
    local direction="${5:-backward}"

    _ensure_clinic_auth || return 1

    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/loki/${cluster_id}/api/v1/query" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "direction=${direction}"
    if [ -n "$time" ]; then
        set -- "$@" --data-urlencode "time=${time}"
    fi
    curl "$@"
}

# Historical name retained because no canonical instant-query route exists.
clinic_loki_query() {
    clinic_legacy_loki_query "$@"
}

clinic_legacy_loki_query_range() {
    local cluster_id="$1"
    local query="$2"
    local start="$3"
    local end="$4"
    local limit="${5:-100}"
    local direction="${6:-backward}"

    _ensure_clinic_auth || return 1
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/loki/${cluster_id}/api/v1/query_range" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "direction=${direction}"
}

clinic_legacy_loki_labels() {
    local cluster_id="$1"
    local start="${2:-}"
    local end="${3:-}"

    _ensure_clinic_auth || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/loki/${cluster_id}/api/v1/labels" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_legacy_loki_label_values() {
    local cluster_id="$1"
    local label_name="$2"
    local start="${3:-}"
    local end="${4:-}"

    _ensure_clinic_auth || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/loki/${cluster_id}/api/v1/label/${label_name}/values" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_legacy_loki_series() {
    local cluster_id="$1"
    local match="$2"
    local start="${3:-}"
    local end="${4:-}"

    _ensure_clinic_auth || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/loki/${cluster_id}/api/v1/series" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "match[]=${match}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_system_logs_datasources() {
    _ensure_clinic_auth || return 1
    curl -s "${CLINIC_ENDPOINT}/data-proxy/api/v1/system-log-datasources" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_system_logs_default_datasource() {
    if [ -n "${CLINIC_SYSTEM_LOGS_DATASOURCE:-}" ]; then
        printf '%s\n' "$CLINIC_SYSTEM_LOGS_DATASOURCE"
        return 0
    fi

    clinic_system_logs_datasources | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
value = (payload.get("data") or {}).get("default_datasource") or ""
if not value:
    print("canonical system logs catalog has no default datasource", file=sys.stderr)
    raise SystemExit(1)
print(value)
'
}

clinic_system_logs_datasources_table() {
    _ensure_clinic_auth || return 1

    clinic_system_logs_datasources | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
data = payload.get("data") or {}
default = data.get("default_datasource") or ""
items = data.get("datasources") or []

print("datasource_uid\tprovider\tregion\tdefault")
for item in items:
    datasource_uid = item.get("uid") or ""
    provider = item.get("provider") or ""
    region = item.get("region") or ""
    is_default = "yes" if item.get("is_default") or datasource_uid == default else ""
    print(f"{datasource_uid}\t{provider}\t{region}\t{is_default}")
'
}

clinic_system_logs_datasource_for_region() {
    local provider="$1"
    local region="$2"

    if [ -z "$provider" ] || [ -z "$region" ]; then
        echo "usage: clinic_system_logs_datasource_for_region <provider> <region>" >&2
        return 2
    fi

    _ensure_clinic_auth || return 1
    clinic_system_logs_datasources | PROVIDER="$provider" REGION="$region" python3 -c '
import json
import os
import sys

provider = os.environ["PROVIDER"].lower()
region = os.environ["REGION"].lower()
payload = json.load(sys.stdin)
items = (payload.get("data") or {}).get("datasources") or []

for item in items:
    datasource_uid = item.get("uid") or ""
    if (item.get("provider") or "").lower() == provider and (item.get("region") or "").lower() == region:
        print(datasource_uid)
        raise SystemExit(0)

print(f"no system logs datasource found for provider={provider} region={region}", file=sys.stderr)
raise SystemExit(1)
'
}

clinic_system_logs_query_range() {
    if [ $# -lt 3 ]; then
        echo "usage: clinic_system_logs_query_range <logql> <start> <end> [limit] [direction] [datasource_uid]" >&2
        return 2
    fi

    local query="$1"
    local start="$2"
    local end="$3"
    local limit="${4:-1000}"
    local direction="${5:-backward}"
    local datasource_uid="${6:-}"
    local encoded_datasource_uid

    _ensure_clinic_auth || return 1
    if [ -z "$datasource_uid" ]; then
        datasource_uid=$(clinic_system_logs_default_datasource) || return 1
    fi
    encoded_datasource_uid=$(_clinic_path_segment "$datasource_uid") || return 1

    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/system-log-datasources/${encoded_datasource_uid}/logs/query-range" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "direction=${direction}"
}

clinic_system_logs_labels() {
    local datasource_uid="${1:-}"
    local start="${2:-}"
    local end="${3:-}"
    local encoded_datasource_uid

    _ensure_clinic_auth || return 1
    if [ -z "$datasource_uid" ]; then
        datasource_uid=$(clinic_system_logs_default_datasource) || return 1
    fi
    encoded_datasource_uid=$(_clinic_path_segment "$datasource_uid") || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/system-log-datasources/${encoded_datasource_uid}/logs/labels" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_system_logs_label_values() {
    local label_name="$1"
    local start="${2:-}"
    local end="${3:-}"
    local datasource_uid="${4:-}"
    local encoded_datasource_uid encoded_label_name

    _ensure_clinic_auth || return 1
    if [ -z "$datasource_uid" ]; then
        datasource_uid=$(clinic_system_logs_default_datasource) || return 1
    fi
    encoded_datasource_uid=$(_clinic_path_segment "$datasource_uid") || return 1
    encoded_label_name=$(_clinic_path_segment "$label_name") || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/system-log-datasources/${encoded_datasource_uid}/logs/labels/${encoded_label_name}/values" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_system_logs_series() {
    local match="$1"
    local start="${2:-}"
    local end="${3:-}"
    local datasource_uid="${4:-}"
    local encoded_datasource_uid

    if [ -z "$match" ]; then
        echo "usage: clinic_system_logs_series <match[]> [start] [end] [datasource_uid]" >&2
        return 2
    fi

    _ensure_clinic_auth || return 1
    if [ -z "$datasource_uid" ]; then
        datasource_uid=$(clinic_system_logs_default_datasource) || return 1
    fi
    encoded_datasource_uid=$(_clinic_path_segment "$datasource_uid") || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/system-log-datasources/${encoded_datasource_uid}/logs/series" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "match[]=${match}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_legacy_system_logs_datasources() {
    _ensure_clinic_auth || return 1
    curl -s "${CLINIC_ENDPOINT}/data-proxy/system-logs/api/v1/datasources" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_legacy_system_logs_query_range() {
    local query="$1"
    local start="$2"
    local end="$3"
    local limit="${4:-1000}"
    local direction="${5:-backward}"
    local datasource="${6:-Loki-aws-us-west-2}"

    _ensure_clinic_auth || return 1
    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/system-logs/api/v1/query_range" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "datasource=${datasource}" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "direction=${direction}"
}

clinic_legacy_system_logs_labels() {
    local datasource="$1"
    local start="${2:-}"
    local end="${3:-}"

    _ensure_clinic_auth || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/system-logs/api/v1/labels" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "datasource=${datasource}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_legacy_system_logs_label_values() {
    local label_name="$1"
    local start="${2:-}"
    local end="${3:-}"
    local datasource="$4"

    _ensure_clinic_auth || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/system-logs/api/v1/label/${label_name}/values" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "datasource=${datasource}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}

clinic_legacy_system_logs_series() {
    local match="$1"
    local start="${2:-}"
    local end="${3:-}"
    local datasource="$4"

    _ensure_clinic_auth || return 1
    set -- -s -G "${CLINIC_ENDPOINT}/data-proxy/system-logs/api/v1/series" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "datasource=${datasource}" \
        --data-urlencode "match[]=${match}"
    if [ -n "$start" ] && [ -n "$end" ]; then
        set -- "$@" --data-urlencode "start=${start}" --data-urlencode "end=${end}"
    fi
    curl "$@"
}
