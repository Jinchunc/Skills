#!/bin/bash
# O11Y Cluster Metadata Resolver
# Usage: source resolve-cluster.sh && resolve_cluster_meta
#
# Requires these variables to be set before calling:
#   CLUSTER_ID        - TiDB Cloud cluster ID (applicationID)
#   BIZ_TYPE          - O11Y bizType (resolve in SKILL.md via o11y-auth helper)
#   O11Y_AUTH0_TOKEN  - Auth0 bearer token from o11y-auth skill
#
# After a successful call, exports:
#   BIZ_TYPE, VENDOR, REGION, ORG_ID, GS_URL

resolve_cluster_meta() {
    # ── 1. Validate required inputs ──────────────────────────────────────────
    if [ -z "$CLUSTER_ID" ]; then
        echo "ERROR: CLUSTER_ID is required" >&2
        return 1
    fi
    if [ -z "$BIZ_TYPE" ]; then
        echo "ERROR: BIZ_TYPE is required — resolve it in SKILL.md before calling resolve_cluster_meta" >&2
        return 1
    fi
    if [ -z "$O11Y_AUTH0_TOKEN" ]; then
        echo "ERROR: O11Y_AUTH0_TOKEN is required — run o11y-auth first" >&2
        return 1
    fi

    # ── 2. GS_URL (fixed for all requests) ───────────────────────────────────
    GS_URL="https://www.gs.us-west-2.aws.observability.tidbcloud.com"
    export GS_URL
    echo "GS_URL: ${GS_URL}" >&2

    # ── 3. Fetch cluster metadata via global clusters API ────────────────────
    local response
    response=$(curl -s \
        "${GS_URL}/api/v1/global/clusters?bizType=${BIZ_TYPE}&applicationID=${CLUSTER_ID}" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${O11Y_AUTH0_TOKEN}")

    local code
    code=$(echo "$response" | jq -r '.code // empty')
    if [ "$code" != "0" ]; then
        echo "ERROR: Global clusters API returned non-zero code" >&2
        echo "$response" | jq . >&2
        return 1
    fi

    local cluster_count
    cluster_count=$(echo "$response" | jq '.data.clusters | length')
    if [ "$cluster_count" -eq 0 ]; then
        echo "ERROR: No cluster found for CLUSTER_ID=${CLUSTER_ID} BIZ_TYPE=${BIZ_TYPE}" >&2
        return 1
    fi

    # Pick the first matching cluster
    local cluster
    cluster=$(echo "$response" | jq '.data.clusters[0]')

    export VENDOR
    export REGION
    export ORG_ID
    VENDOR=$(echo "$cluster" | jq -r '.vendor')
    REGION=$(echo "$cluster" | jq -r '.region')
    ORG_ID=$(echo "$cluster" | jq -r '.tenantID')

    if [ -z "$VENDOR" ] || [ "$VENDOR" = "null" ] ||
       [ -z "$REGION" ] || [ "$REGION" = "null" ] ||
       [ -z "$ORG_ID" ]  || [ "$ORG_ID"  = "null" ]; then
        echo "ERROR: Incomplete metadata from API response" >&2
        echo "$cluster" | jq . >&2
        return 1
    fi

    echo "Cluster metadata resolved:" >&2
    echo "  VENDOR=${VENDOR}" >&2
    echo "  REGION=${REGION}" >&2
    echo "  ORG_ID=${ORG_ID}" >&2
    echo "  BIZ_TYPE=${BIZ_TYPE}" >&2
    echo "  GS_URL=${GS_URL}" >&2
    echo "Ready to call monitoring pipeline APIs." >&2
}

export -f resolve_cluster_meta
