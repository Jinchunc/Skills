# NGM continuous profiling helpers (continuous-profiling sub-skill).
# Requires: clinic-auth.sh, clinic-download-dir.sh (_clinic_default_download_dir), clinic-cluster.sh.

CLINIC_CONTINUOUS_PROFILING_BASE="${CLINIC_ENDPOINT}/ngm/api/v1/continuous_profiling"

_clinic_ngm_context_from_cluster_id() {
    local cluster_id="$1"

    local search_result
    search_result=$(clinic_search_cluster_by_id "$cluster_id" "true")
    local item
    item=$(echo "$search_result" | jq -c --arg id "$cluster_id" '[.items[] | select(.clusterID == $id)][0] // empty')
    if [ -z "$item" ] || [ "$item" = "null" ]; then
        echo "Cluster not found: $cluster_id" >&2
        return 1
    fi

    # clinic_org_id: internal org for GET /orgs/{orgID}/clusters/...
    # ngm_x_org_id: value for NGM header x-org-id — use tenantID (user-facing), not orgID (verified against Clinic UI)
    local clinic_org_id ngm_x_org_id project_id deploy provider region
    clinic_org_id=$(echo "$item" | jq -r '.orgID // empty')
    ngm_x_org_id=$(echo "$item" | jq -r '.tenantID // empty')
    project_id=$(echo "$item" | jq -r '.projectID // .projectId // empty')
    # NGM continuous_profiling expects x-deploy-type from clusterDeployType (not clusterDeployTypeV2).
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

    printf '%s|%s|%s|%s|%s' "$ngm_x_org_id" "$project_id" "$deploy" "$provider" "$region"
}

clinic_continuous_profiling_group_profiles() {
    local cluster_id="$1"
    local begin_time="$2"
    local end_time="$3"

    _ensure_clinic_auth || return 1
    local ctx
    ctx=$(_clinic_ngm_context_from_cluster_id "$cluster_id") || return 1
    local org_id project_id deploy provider region
    org_id=$(echo "$ctx" | cut -d'|' -f1)
    project_id=$(echo "$ctx" | cut -d'|' -f2)
    deploy=$(echo "$ctx" | cut -d'|' -f3)
    provider=$(echo "$ctx" | cut -d'|' -f4)
    region=$(echo "$ctx" | cut -d'|' -f5)

    curl -s -G "${CLINIC_CONTINUOUS_PROFILING_BASE}/group_profiles" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "x-cluster-id: ${cluster_id}" \
        -H "x-org-id: ${org_id}" \
        -H "x-project-id: ${project_id}" \
        -H "x-deploy-type: ${deploy}" \
        -H "x-provider: ${provider}" \
        -H "x-region: ${region}" \
        --data-urlencode "begin_time=${begin_time}" \
        --data-urlencode "end_time=${end_time}"
}

clinic_continuous_profiling_detail() {
    local cluster_id="$1"
    local ts="$2"

    _ensure_clinic_auth || return 1
    local ctx
    ctx=$(_clinic_ngm_context_from_cluster_id "$cluster_id") || return 1
    local org_id project_id deploy provider region
    org_id=$(echo "$ctx" | cut -d'|' -f1)
    project_id=$(echo "$ctx" | cut -d'|' -f2)
    deploy=$(echo "$ctx" | cut -d'|' -f3)
    provider=$(echo "$ctx" | cut -d'|' -f4)
    region=$(echo "$ctx" | cut -d'|' -f5)

    curl -s -G "${CLINIC_CONTINUOUS_PROFILING_BASE}/group_profile/detail" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "x-cluster-id: ${cluster_id}" \
        -H "x-org-id: ${org_id}" \
        -H "x-project-id: ${project_id}" \
        -H "x-deploy-type: ${deploy}" \
        -H "x-provider: ${provider}" \
        -H "x-region: ${region}" \
        --data-urlencode "ts=${ts}"
}

clinic_continuous_profiling_action_token() {
    local cluster_id="$1"
    local ts="$2"
    local profile_type="$3"
    local component="$4"
    local address="$5"
    local data_format="${6:-protobuf}"

    _ensure_clinic_auth || return 1
    local ctx
    ctx=$(_clinic_ngm_context_from_cluster_id "$cluster_id") || return 1
    local org_id project_id deploy provider region
    org_id=$(echo "$ctx" | cut -d'|' -f1)
    project_id=$(echo "$ctx" | cut -d'|' -f2)
    deploy=$(echo "$ctx" | cut -d'|' -f3)
    provider=$(echo "$ctx" | cut -d'|' -f4)
    region=$(echo "$ctx" | cut -d'|' -f5)

    local q="ts=${ts}&profile_type=${profile_type}&component=${component}&address=${address}&data_format=${data_format}"
    curl -s -G "${CLINIC_CONTINUOUS_PROFILING_BASE}/action_token" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -H "x-cluster-id: ${cluster_id}" \
        -H "x-org-id: ${org_id}" \
        -H "x-project-id: ${project_id}" \
        -H "x-deploy-type: ${deploy}" \
        -H "x-provider: ${provider}" \
        -H "x-region: ${region}" \
        --data-urlencode "q=${q}"
}

# Usage: clinic_continuous_profiling_download <cluster_id> <token> [output_path]
# If output_path is omitted, writes to CLINIC_DIAGNOSTIC_DOWNLOAD_DIR or $PWD as pprof-download-<unix>.bin
clinic_continuous_profiling_download() {
    local cluster_id="$1"
    local token="$2"
    local output_path="${3:-}"

    if [ -z "$output_path" ]; then
        local dir
        dir="$(_clinic_default_download_dir)"
        dir="${dir%/}"
        output_path="${dir}/pprof-download-$(date +%s).bin"
    fi

    _ensure_clinic_auth || return 1
    local ctx
    ctx=$(_clinic_ngm_context_from_cluster_id "$cluster_id") || return 1
    local org_id project_id deploy provider region
    org_id=$(echo "$ctx" | cut -d'|' -f1)
    project_id=$(echo "$ctx" | cut -d'|' -f2)
    deploy=$(echo "$ctx" | cut -d'|' -f3)
    provider=$(echo "$ctx" | cut -d'|' -f4)
    region=$(echo "$ctx" | cut -d'|' -f5)

    curl -s -L "${CLINIC_CONTINUOUS_PROFILING_BASE}/download?token=${token}" \
        -H "Authorization: Bearer ${CLINIC_API_KEY}" \
        -o "${output_path}" || return 1
    echo "clinic_continuous_profiling_download: wrote ${output_path}" >&2
}

# Usage: clinic_continuous_profiling_fetch <cluster_id> <ts> <profile_type> <component> <address> [output_path]
# If output_path is omitted, writes to CLINIC_DIAGNOSTIC_DOWNLOAD_DIR or $PWD as pprof-<ts>-<component>-<profile_type>.pb (slashes in component → _)
clinic_continuous_profiling_fetch() {
    local cluster_id="$1"
    local ts="$2"
    local profile_type="$3"
    local component="$4"
    local address="$5"
    local output_path="${6:-}"

    if [ -z "$output_path" ]; then
        local dir comp_safe
        dir="$(_clinic_default_download_dir)"
        dir="${dir%/}"
        comp_safe=$(printf '%s' "${component}-${address}" | tr '/:' '__')
        output_path="${dir}/pprof-${ts}-${comp_safe}-${profile_type}.pb"
    fi

    local token
    token=$(clinic_continuous_profiling_action_token "$cluster_id" "$ts" "$profile_type" "$component" "$address")
    if [ -z "$token" ]; then
        echo "Empty action_token response" >&2
        return 1
    fi
    token=$(printf '%s' "$token" | tr -d '\n\r')
    clinic_continuous_profiling_download "$cluster_id" "$token" "$output_path"
}
