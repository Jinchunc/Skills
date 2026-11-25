# Canonical Data Proxy cluster metadata helpers.
# Requires: clinic-auth.sh (CLINIC_ENDPOINT, CLINIC_API_BASE, _ensure_clinic_auth).

_clinic_path_segment() {
    python3 - "$1" <<'PY'
import sys
import urllib.parse

print(urllib.parse.quote(sys.argv[1], safe=""))
PY
}

clinic_get_cluster_by_id() {
    local cluster_id="$1"

    if [ -z "$cluster_id" ]; then
        echo "usage: clinic_get_cluster_by_id <cluster_id>" >&2
        return 2
    fi

    _ensure_clinic_auth || return 1

    local encoded_cluster_id
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1
    curl -s "${CLINIC_ENDPOINT}/data-proxy/api/v1/clusters/${encoded_cluster_id}" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

_clinic_canonical_org_id_from_cluster_id() {
    local cluster_id="$1"
    local metadata org_id

    metadata=$(clinic_get_cluster_by_id "$cluster_id") || return 1
    org_id=$(printf '%s' "$metadata" | jq -r '.orgId // empty')
    if [ -z "$org_id" ]; then
        echo "Could not resolve DBAAS org ID for cluster ${cluster_id}" >&2
        return 1
    fi
    printf '%s' "$org_id"
}

clinic_list_clusters() {
    if [ $# -lt 1 ]; then
        echo "usage: clinic_list_clusters <dbaas_org_id> [query] [show_deleted] [page_size] [page]" >&2
        return 2
    fi

    local org_id="$1"
    local query="${2:-}"
    local show_deleted="${3:-false}"
    local page_size="${4:-50}"
    local page="${5:-1}"

    _ensure_clinic_auth || return 1

    curl -s -G "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        --data-urlencode "query=${query}" \
        --data-urlencode "showDeleted=${show_deleted}" \
        --data-urlencode "pageSize=${page_size}" \
        --data-urlencode "page=${page}"
}

clinic_get_cluster() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_get_cluster <dbaas_org_id> <cluster_id>" >&2
        return 2
    fi

    local org_id="$1"
    local cluster_id="$2"
    local encoded_cluster_id

    _ensure_clinic_auth || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    curl -s "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_get_components() {
    if [ $# -lt 2 ]; then
        echo "usage: clinic_get_components <dbaas_org_id> <cluster_id>" >&2
        return 2
    fi

    local org_id="$1"
    local cluster_id="$2"
    local encoded_cluster_id

    _ensure_clinic_auth || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1

    curl -s "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/components" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

# Compatibility name; the preferred implementation now uses canonical /components.
clinic_get_resource_pool_components() {
    clinic_get_components "$@"
}

clinic_get_topology() {
    local cluster_id="$1"
    local metadata org_id deploy_type

    if [ -z "$cluster_id" ]; then
        echo "usage: clinic_get_topology <cluster_id>" >&2
        return 2
    fi

    metadata=$(clinic_get_cluster_by_id "$cluster_id") || return 1
    org_id=$(printf '%s' "$metadata" | jq -r '.orgId // empty')
    deploy_type=$(printf '%s' "$metadata" | jq -r '
        (.deployType // .deployTypeV2 // empty)
        | ascii_downcase
        | gsub("[ -]+"; "_")
    ')
    if [ -z "$org_id" ]; then
        echo "Cluster not found: $cluster_id" >&2
        return 1
    fi

    case "$deploy_type" in
        dedicated|resource_pool|byoc_resource_pool|premium_resource_pool|premium|byoc)
            clinic_get_components "$org_id" "$cluster_id"
            ;;
        shared|starter|essential)
            echo "Topology is not supported for shared clusters (starter/essential); use cluster list/detail APIs only." >&2
            return 1
            ;;
        *)
            echo "Cluster type '$deploy_type' has no topology" >&2
            return 1
            ;;
    esac
}

# Legacy Clinic routes remain available for old consumers and NGM context discovery.
clinic_legacy_search_clusters() {
    local query="$1"
    local show_deleted="${2:-false}"
    local limit="${3:-10}"

    _ensure_clinic_auth || return 1

    curl -s "${CLINIC_API_BASE}/dashboard/clusters" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -G \
        --data-urlencode "query=${query}" \
        --data-urlencode "show_deleted=${show_deleted}" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "page=1"
}

clinic_legacy_search_cluster_by_id() {
    local cluster_id="$1"
    local show_deleted="${2:-false}"
    local limit="${3:-10}"

    _ensure_clinic_auth || return 1

    curl -s "${CLINIC_API_BASE}/dashboard/clusters" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -G \
        --data-urlencode "cluster_id=${cluster_id}" \
        --data-urlencode "show_deleted=${show_deleted}" \
        --data-urlencode "limit=${limit}" \
        --data-urlencode "page=1"
}

clinic_legacy_get_cluster() {
    local clinic_internal_org_id="$1"
    local cluster_id="$2"

    _ensure_clinic_auth || return 1

    curl -s "${CLINIC_API_BASE}/orgs/${clinic_internal_org_id}/clusters/${cluster_id}" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_legacy_get_cluster_by_id() {
    local cluster_id="$1"
    local search_result clinic_internal_org_id

    search_result=$(clinic_legacy_search_cluster_by_id "$cluster_id" "true") || return 1
    clinic_internal_org_id=$(printf '%s' "$search_result" | jq -r --arg cluster_id "$cluster_id" '[.items[] | select(.clusterID == $cluster_id)][0].orgID // empty')
    if [ -z "$clinic_internal_org_id" ]; then
        echo "Cluster not found: $cluster_id" >&2
        return 1
    fi
    clinic_legacy_get_cluster "$clinic_internal_org_id" "$cluster_id"
}

clinic_legacy_get_resource_pool_components() {
    local clinic_internal_org_id="$1"
    local cluster_id="$2"

    _ensure_clinic_auth || return 1

    curl -s "${CLINIC_API_BASE}/orgs/${clinic_internal_org_id}/clusters/${cluster_id}/resource-pool/components" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_legacy_get_topology() {
    local cluster_id="$1"
    local search_result clinic_internal_org_id deploy_type

    search_result=$(clinic_legacy_search_cluster_by_id "$cluster_id" "true") || return 1
    clinic_internal_org_id=$(printf '%s' "$search_result" | jq -r --arg cluster_id "$cluster_id" '[.items[] | select(.clusterID == $cluster_id)][0].orgID // empty')
    deploy_type=$(printf '%s' "$search_result" | jq -r --arg cluster_id "$cluster_id" '
        [.items[] | select(.clusterID == $cluster_id)][0]
        | (.clusterDeployType // .clusterDeployTypeV2 // .deployType // empty)
        | ascii_downcase
        | gsub("[ -]+"; "_")
    ')
    if [ -z "$clinic_internal_org_id" ]; then
        echo "Cluster not found: $cluster_id" >&2
        return 1
    fi

    case "$deploy_type" in
        dedicated|resource_pool|byoc_resource_pool|premium_resource_pool|premium|byoc)
            clinic_legacy_get_resource_pool_components "$clinic_internal_org_id" "$cluster_id"
            ;;
        shared|starter|essential)
            echo "Topology is not supported for shared clusters (starter/essential); use cluster list/detail APIs only." >&2
            return 1
            ;;
        *)
            echo "Cluster type '$deploy_type' has no topology" >&2
            return 1
            ;;
    esac
}

# Historical discovery names intentionally stay available. New workflows should
# use clinic_get_cluster_by_id or clinic_list_clusters.
clinic_search_clusters() {
    clinic_legacy_search_clusters "$@"
}

clinic_search_cluster_by_id() {
    clinic_legacy_search_cluster_by_id "$@"
}
