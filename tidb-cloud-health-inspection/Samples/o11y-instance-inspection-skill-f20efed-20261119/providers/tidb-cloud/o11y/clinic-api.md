# Clinic API Reference (legacy `/clinic/api/v1` details)

> **Legacy-only reference.** The current authority is `../ops/clinic-api.md` + sub-skills;
> per-tier routing lives in `../ops/references/tidb-x-o11y-routing.md`. This file keeps ONLY
> the legacy `/clinic/api/v1` endpoint details still referenced for compatibility (cluster
> metadata search §2, legacy Activity Hub pagination). Everything else was removed in the
> 2026-08 dedup — see the mapping table at the bottom.

Auth: Bearer with an internal-user key (`CLINIC_API_TOKEN` in `.env`); details in
`../ops/clinic-api.md` §Authentication.

---

## 2. Cluster Metadata

### Search Clusters: clinic_search_cluster_by_id

```
GET /clinic/api/v1/dashboard/clusters
```

| Parameter | Type | Required | Description |
|------|------|------|------|
| `query` | string | No | Fuzzy search: tenant name/ID, cluster name/ID |
| `cluster_id` | string | No | Exact cluster ID lookup (recommended when cluster ID is known) |
| `show_deleted` | boolean | No | Include deleted clusters (default false) |
| `cluster_status` | string | No | Status filter: `active`, `deleted`, etc. |
| `deploy_type_v2` | string | No | Type filter: `premium`, `dedicated`, `starter`, `resource_pool` |
| `sort` | string | No | Sort field |
| `order` | string | No | Sort order: `asc` or `desc` |
| `limit` | int | No | Items per page (default 10) |
| `page` | int | No | Page number (default 1) |

#### Exact Cluster Lookup (Recommended)

When you already know a cluster ID, use `cluster_id` instead of `query`. Direct `cluster_id` lookup is faster than `query` search and avoids fuzzy-match ambiguity and branch-record ordering issues.

```bash
curl -s "https://clinic.pingcap.com/clinic/api/v1/dashboard/clusters" \
  -H "Authorization: Bearer ${CLINIC_API_TOKEN}" \
  -G \
  --data-urlencode "cluster_id=10314554245653732213" \
  --data-urlencode "show_deleted=true"
```

**Response fields**:

| Field | Description |
|------|------|
| `cluster_name` | Cluster display name |
| `region` | Deployment region |
| `provider` | Cloud provider (aws/gcp/azure) |
| `tier` | Product line: `dedicated` / `premium` / `starter` / `essential` |
| `status` | Cluster status |
| `org_id` | **Internal organization ID (not tenant_id!)** |
| `version` | TiDB version |
| `clusterDeployTypeV2` | Deployment type for the cluster |

**Important**: `query` is a fuzzy search; do not assume `items[0]` is the target cluster. When the cluster_id is known, prefer using the `cluster_id` parameter for exact lookup.

**Note on branch clusters**: Clinic may return branch cluster IDs in `bran-xxxx` format. If using `query`, be sure to filter with `select(.clusterID == $cluster_id)` before extracting fields.

### Get Cluster Details: clinic_get_cluster_by_id

```
GET /clinic/api/v1/orgs/{orgID}/clusters/{clusterID}
```

**Note**: `{orgID}` comes from the search API response and is an internal org ID, **not** the user's tenant_id.

### Get Resource Pool Components

```
GET /clinic/api/v1/orgs/{orgID}/clusters/{clusterID}/resource-pool/components
```

Supports `premium` and `byoc` type clusters. Returns component topology (TiDB, TiKV, PD, etc.).

| Use Case | Path | When |
|----------|------|------|
| **Current cluster** | `/clusters/{clusterID}/resource-pool/components` | Query the cluster you care about (e.g. premium tenant, byoc tenant) |
| **Resource pool** | `/clusters/{parentID}/resource-pool/components` | Query the underlying resource pool (cluster has `parentID` pointing to the pool) |

```bash
# Current cluster's components (e.g. premium cluster ID)
curl -s "https://clinic.pingcap.com/clinic/api/v1/orgs/${ORG_ID}/clusters/${CLUSTER_ID}/resource-pool/components" \
  -H "Authorization: Bearer ${CLINIC_API_TOKEN}"

# Resource pool's components (use parentID from cluster metadata)
curl -s "https://clinic.pingcap.com/clinic/api/v1/orgs/${ORG_ID}/clusters/${PARENT_ID}/resource-pool/components" \
  -H "Authorization: Bearer ${CLINIC_API_TOKEN}"
```

### ID Type Descriptions

| ID | User-visible | Description |
|----|----------|------|
| `tenantID` | Yes | User's tenant ID, the ID users reference |
| `orgID` | No | Clinic internal ID, used only in API paths |
| `clusterID` | Yes | Cluster ID, same in both user and API contexts |
| `parentID` | No | For Premium/BYOC tenant clusters, points to Resource Pool cluster |
| `clusterDeployTypeV2` | Yes | Deployment type for the cluster |

**Key point**: When users say "tenant ID", they mean `tenantID`. `orgID` is an internal ID that must be extracted from the search API before it can be used in the details API.

### Cluster Types and Topology

| Type | Deploy Type | Has Topology | How to Obtain |
|------|-------------|--------|----------|
| Dedicated | `dedicated` | Yes | `components` field in Get Cluster response |
| Resource Pool | `byoc resource pool`, `premium resource pool` | Yes | `/clusters/{clusterID}/resource-pool/components` API |
| Premium / BYOC | `premium`, `byoc` (with parentID) | Yes | `/clusters/{clusterID}/resource-pool/components` (current cluster) or `/clusters/{parentID}/resource-pool/components` (resource pool) |
| Starter/Serverless | `starter` | No | No topology (shared infrastructure) |

---

## 10. Cluster Events (Activity Hub)

Query cluster lifecycle events (backup, scaling, maintenance, changefeed, etc.).

### Endpoint

```
GET /clinic/api/v1/activityhub/applications/{application_id}/targets/{target_id}/activities
```

- **`application_id`** = Clinic internal **`orgID`** (NOT `tenantID`). Get it from cluster metadata:
  ```bash
  RESULT=$(clinic_search_cluster_by_id "${CLUSTER_ID}" "true")
  ORG_ID=$(echo "$RESULT" | jq -r ".items[] | select(.clusterID == \"${CLUSTER_ID}\") | .orgID")
  ```
- **`target_id`** = cluster ID

### Parameters

| Parameter | Location | Required | Description |
|-----------|----------|----------|-------------|
| `begin_ts` | query | Yes | Start time (Unix seconds) |
| `end_ts` | query | Yes | End time (Unix seconds) |
| `name` | query | No | Filter by event name (e.g., `backupcluster`) |
| `severity` | query | No | Minimum severity (default 0; -4 for debug) |
| `limit` | query | No | Result limit (min 20) |
| `cursor` | **header** | No | Pagination cursor from previous response |

Requires internal user auth. Response has `partial` (bool) and `cursor` (string) for pagination.

---

## Moved content — where everything else lives now

| Was here | Now lives in |
|---|---|
| Data Proxy SQL / slow query / dialects | `../ops/data-proxy/SUBSKILL.md` |
| PromQL metrics endpoints + quick reference | `../ops/metrics/SUBSKILL.md` + `../../engines/tidb/references/promql-templates.md` |
| Loki log queries + labels | `../ops/loki/SUBSKILL.md` |
| Product line capability matrix + tier limitations | `../ops/references/tidb-x-o11y-routing.md` |
| Activity Hub canonical API + event types | `../ops/activity-hub/SUBSKILL.md` |
| Troubleshooting / best practices | `../ops/clinic-api.md` |
