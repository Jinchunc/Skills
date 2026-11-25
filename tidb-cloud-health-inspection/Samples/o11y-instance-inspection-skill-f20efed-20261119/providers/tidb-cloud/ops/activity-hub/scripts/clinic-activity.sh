# Activity Events helpers.
# Requires: clinic-auth.sh and cluster-metadata/scripts/clinic-cluster.sh.

clinic_get_activities() {
    if [ $# -lt 4 ]; then
        echo "usage: clinic_get_activities <dbaas_org_id> <cluster_id> <begin_unix_sec> <end_unix_sec> [limit] [cursor] [catalog] [type] [name] [severity]" >&2
        return 2
    fi

    local org_id="$1"
    local cluster_id="$2"
    local begin_ts="$3"
    local end_ts="$4"
    local limit="${5:-100}"
    local cursor="${6:-}"
    local catalog="${7:-}"
    local activity_type="${8:-}"
    local name="${9:-}"
    local severity="${10:-}"
    local encoded_cluster_id url

    _ensure_clinic_auth || return 1
    encoded_cluster_id=$(_clinic_path_segment "$cluster_id") || return 1
    url=$(python3 - \
        "${CLINIC_ENDPOINT}/data-proxy/api/v1/orgs/${org_id}/clusters/${encoded_cluster_id}/activities" \
        "$begin_ts" "$end_ts" "$limit" "$cursor" "$catalog" "$activity_type" "$name" "$severity" <<'PY'
import sys
import urllib.parse

base, begin_ts, end_ts, limit, cursor, catalog, activity_type, name, severity = sys.argv[1:]
params = {"begin_ts": begin_ts, "end_ts": end_ts, "limit": limit}
for key, value in (
    ("cursor", cursor),
    ("catalog", catalog),
    ("type", activity_type),
    ("name", name),
    ("severity", severity),
):
    if value != "":
        params[key] = value
print(base + "?" + urllib.parse.urlencode(params))
PY
) || return 1

    curl -s "$url" -H "Authorization: Bearer ${CLINIC_API_KEY}"
}

clinic_legacy_get_activities() {
    if [ $# -lt 4 ]; then
        echo "usage: clinic_legacy_get_activities <clinic_internal_org_id> <cluster_id> <begin_unix_sec> <end_unix_sec> [limit] [cursor] [catalog] [type] [name] [severity]" >&2
        return 2
    fi

    local clinic_internal_org_id="$1"
    local cluster_id="$2"
    local begin_ts="$3"
    local end_ts="$4"
    local limit="${5:-100}"
    local cursor="${6:-}"
    local catalog="${7:-}"
    local activity_type="${8:-}"
    local name="${9:-}"
    local severity="${10:-}"
    local url

    _ensure_clinic_auth || return 1
    url=$(python3 - \
        "${CLINIC_API_BASE}/activityhub/applications/${clinic_internal_org_id}/targets/${cluster_id}/activities" \
        "$begin_ts" "$end_ts" "$limit" "$catalog" "$activity_type" "$name" "$severity" <<'PY'
import sys
import urllib.parse

base, begin_ts, end_ts, limit, catalog, activity_type, name, severity = sys.argv[1:]
params = {"begin_ts": begin_ts, "end_ts": end_ts, "limit": limit}
for key, value in (
    ("catalog", catalog),
    ("type", activity_type),
    ("name", name),
    ("severity", severity),
):
    if value != "":
        params[key] = value
print(base + "?" + urllib.parse.urlencode(params))
PY
) || return 1

    if [ -n "$cursor" ]; then
        curl -s "$url" \
            -H "Authorization: Bearer ${CLINIC_API_KEY}" \
            -H "cursor: ${cursor}"
    else
        curl -s "$url" -H "Authorization: Bearer ${CLINIC_API_KEY}"
    fi
}
