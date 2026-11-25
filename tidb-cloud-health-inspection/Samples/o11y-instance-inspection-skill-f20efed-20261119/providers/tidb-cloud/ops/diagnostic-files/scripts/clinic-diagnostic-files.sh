# NGM diagnostic file helpers (plan_replayer, oom_record) — diagnostic-files sub-skill.
# Dedicated tier only (documented in SUBSKILL.md).
# Requires: clinic-auth.sh, clinic-cluster.sh, clinic-download-dir.sh (_clinic_default_download_dir).

CLINIC_DIAGNOSTIC_NGM_BASE="${CLINIC_ENDPOINT}/ngm/api/v1"

_clinic_diagnostic_ngm_context_from_cluster_id() {
    local cluster_id="$1"

    local search_result
    search_result=$(clinic_search_cluster_by_id "$cluster_id" "true")
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
    deploy=$(echo "$item" | jq -r '(.clusterDeployType // "") | ascii_downcase | gsub(" "; "_")')
    provider=$(echo "$item" | jq -r '(.cloudProvider // .provider // .vendor // .clusterProviderName // empty) | ascii_downcase')
    region=$(echo "$item" | jq -r '.region // .clusterRegionName // empty')

    if [ -z "$deploy" ]; then
        local detail
        detail=$(clinic_legacy_get_cluster "$clinic_org_id" "$cluster_id")
        deploy=$(echo "$detail" | jq -r '(.clusterDeployType // .deployType // "dedicated") | ascii_downcase | gsub(" "; "_")')
    fi

    if [ -z "$project_id" ] || [ "$project_id" = "null" ]; then
        local detail
        detail=$(clinic_legacy_get_cluster "$clinic_org_id" "$cluster_id")
        project_id=$(echo "$detail" | jq -r '.projectID // .projectId // empty')
    fi
    if [ -z "$region" ] || [ "$region" = "null" ]; then
        local detail
        detail=$(clinic_legacy_get_cluster "$clinic_org_id" "$cluster_id")
        region=$(echo "$detail" | jq -r '.region // empty')
    fi
    if [ -z "$provider" ] || [ "$provider" = "null" ]; then
        local detail
        detail=$(clinic_legacy_get_cluster "$clinic_org_id" "$cluster_id")
        provider=$(echo "$detail" | jq -r '(.cloudProvider // .provider // .vendor // empty) | ascii_downcase')
    fi

    if [ -z "$clinic_org_id" ] || [ "$clinic_org_id" = "null" ]; then
        echo "Could not resolve orgID (Clinic REST) for cluster $cluster_id" >&2
        return 1
    fi
    if [ -z "$ngm_x_org_id" ] || [ "$ngm_x_org_id" = "null" ]; then
        echo "Could not resolve tenantID for NGM x-org-id (cluster $cluster_id)" >&2
        return 1
    fi
    if [ -z "$project_id" ] || [ "$project_id" = "null" ]; then
        echo "Could not resolve x-project-id for cluster $cluster_id (check Get Cluster fields projectID/projectId)" >&2
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
    if [ -z "$deploy" ]; then
        echo "Could not resolve x-deploy-type (clusterDeployType) for cluster $cluster_id" >&2
        return 1
    fi

    if [ "$deploy" != "dedicated" ]; then
        echo "clinic_diagnostic_*: clusterDeployType is '${deploy}' — NGM diagnostic files are documented for dedicated only; requests may fail." >&2
    fi

    printf '%s|%s|%s|%s|%s' "$ngm_x_org_id" "$project_id" "$deploy" "$provider" "$region"
}

_clinic_diagnostic_ngm_curl_headers() {
    local cluster_id="$1"
    local org_id="$2"
    local project_id="$3"
    local deploy="$4"
    local provider="$5"
    local region="$6"

    printf '%s\n' \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "x-cluster-id: ${cluster_id}" \
        -H "x-org-id: ${org_id}" \
        -H "x-project-id: ${project_id}" \
        -H "x-deploy-type: ${deploy}" \
        -H "x-provider: ${provider}" \
        -H "x-region: ${region}"
}

clinic_diagnostic_list_plan_replayer() {
    local cluster_id="$1"
    local start_sec="$2"
    local end_sec="$3"

    _ensure_clinic_auth || return 1
    local ctx
    ctx=$(_clinic_diagnostic_ngm_context_from_cluster_id "$cluster_id") || return 1
    local org_id project_id deploy provider region
    org_id=$(echo "$ctx" | cut -d'|' -f1)
    project_id=$(echo "$ctx" | cut -d'|' -f2)
    deploy=$(echo "$ctx" | cut -d'|' -f3)
    provider=$(echo "$ctx" | cut -d'|' -f4)
    region=$(echo "$ctx" | cut -d'|' -f5)

    curl -s -G "${CLINIC_DIAGNOSTIC_NGM_BASE}/plan_replayer/list" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "x-cluster-id: ${cluster_id}" \
        -H "x-org-id: ${org_id}" \
        -H "x-project-id: ${project_id}" \
        -H "x-deploy-type: ${deploy}" \
        -H "x-provider: ${provider}" \
        -H "x-region: ${region}" \
        --data-urlencode "start=${start_sec}" \
        --data-urlencode "end=${end_sec}"
}

clinic_diagnostic_list_oom_record() {
    local cluster_id="$1"
    local start_sec="$2"
    local end_sec="$3"

    _ensure_clinic_auth || return 1
    local ctx
    ctx=$(_clinic_diagnostic_ngm_context_from_cluster_id "$cluster_id") || return 1
    local org_id project_id deploy provider region
    org_id=$(echo "$ctx" | cut -d'|' -f1)
    project_id=$(echo "$ctx" | cut -d'|' -f2)
    deploy=$(echo "$ctx" | cut -d'|' -f3)
    provider=$(echo "$ctx" | cut -d'|' -f4)
    region=$(echo "$ctx" | cut -d'|' -f5)

    curl -s -G "${CLINIC_DIAGNOSTIC_NGM_BASE}/oom_record/list" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "x-cluster-id: ${cluster_id}" \
        -H "x-org-id: ${org_id}" \
        -H "x-project-id: ${project_id}" \
        -H "x-deploy-type: ${deploy}" \
        -H "x-provider: ${provider}" \
        -H "x-region: ${region}" \
        --data-urlencode "start=${start_sec}" \
        --data-urlencode "end=${end_sec}"
}

# Download a file returned by list APIs. Both plan_replayer and oom_record artifacts use
# GET .../oom_record/files?provider=&region=&key=
# Usage: clinic_diagnostic_download_file <cluster_id> <key> [output_path]
# If output_path is omitted, writes to default download dir (see _clinic_default_download_dir) + basename(key).
clinic_diagnostic_download_file() {
    local cluster_id="$1"
    local key="$2"
    local output_path="${3:-}"

    if [ -z "$output_path" ]; then
        local base dir
        base=$(basename "$key")
        if [ -z "$base" ] || [ "$base" = "." ] || [ "$base" = ".." ]; then
            base="diagnostic-$(date +%s).dat"
        fi
        dir="$(_clinic_default_download_dir)"
        dir="${dir%/}"
        output_path="${dir}/${base}"
    fi

    _ensure_clinic_auth || return 1
    local ctx
    ctx=$(_clinic_diagnostic_ngm_context_from_cluster_id "$cluster_id") || return 1
    local org_id project_id deploy provider region
    org_id=$(echo "$ctx" | cut -d'|' -f1)
    project_id=$(echo "$ctx" | cut -d'|' -f2)
    deploy=$(echo "$ctx" | cut -d'|' -f3)
    provider=$(echo "$ctx" | cut -d'|' -f4)
    region=$(echo "$ctx" | cut -d'|' -f5)

    curl -s -L -G "${CLINIC_DIAGNOSTIC_NGM_BASE}/oom_record/files" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "x-cluster-id: ${cluster_id}" \
        -H "x-org-id: ${org_id}" \
        -H "x-project-id: ${project_id}" \
        -H "x-deploy-type: ${deploy}" \
        -H "x-provider: ${provider}" \
        -H "x-region: ${region}" \
        --data-urlencode "provider=${provider}" \
        --data-urlencode "region=${region}" \
        --data-urlencode "key=${key}" \
        -o "${output_path}" || return 1
    echo "clinic_diagnostic_download_file: wrote ${output_path}" >&2
}
