# O11Y Data Access Reference

> Data access layer for TiDB Cloud O11Y observability platform
> Unified access: cluster metadata, Prometheus metrics, Loki logs, S3 storage (logs/slowlogs/statements/profiles)

---

## Prerequisites

### Network Access

O11Y endpoints are behind corporate network controls. Access requires one of:
- **Local development:** Feilian VPN (Global Mode) enabled
- **Server-side:** Managed EC2 instance within the security control perimeter

Without proper network access, API calls return 403 Forbidden or HTML login pages.

### Tools

Requires the observability-client tools installed:
- `o11y-data.sh` — Shell helpers (auth, API queries, S3/Loki access)
- `o11y-inspect.py` — Full cluster inspection script
- `o11y-s3-explore.py` — S3 data explorer

Install: `git clone https://github.com/tidbcloud/observability-client.git && cd observability-client && bash install.sh`

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GITHUB_PAT` | GitHub Personal Access Token (required, scopes: read:user, read:org) |
| `CLINIC_API_TOKEN` | Clinic Internal API Key (optional, for Tier 1 cluster lookup) |

---

## Step 0: Locate Cluster & Authenticate

Given a cluster ID, you MUST resolve `provider`, `region`, and `deploy type`
before accessing any data. Follow this **three-tier lookup** — move to the
next tier only if the previous one fails. **Do NOT exhaustively search.**

### Tier 1: Clinic API (fastest — single call)

Use the Clinic Metadata API with `CLINIC_API_TOKEN` from `.env`:

```bash
source .env
curl -s --max-time 5 \
  "https://clinic.pingcap.com/api/v1/clusters/${CLUSTER_ID}" \
  -H "Authorization: Bearer ${CLINIC_API_TOKEN}"
```

Response provides `provider`, `region`, `deployType`, `tenantId`, `projectId`.
If it succeeds, skip directly to authentication.

**Clinic → O11Y field mapping** (merged from legacy/o11y-metrics-api.md §Clinic Integration):

| Clinic field | O11Y parameter | Notes |
|--------------|----------------|-------|
| `clusterProviderName` | `vendor` | `aws` / `gcp` / `alicloud` |
| `clusterRegionName` | `region` | e.g. `us-west-2` |
| `clusterID` | `applicationID` | Dedicated / resource_pool: use directly |
| `parentID` | `applicationID` | **Premium only** — use parent Resource Pool ID |
| `tenantID` | `tenantID` | — |

> The Clinic API may be restricted to internal networks. If it returns HTML,
> 403, or times out, fall through to Tier 2.

### Tier 2: Quick GS API scan (≤ 30 seconds)

Authenticate with any provider (e.g. `aws`) to get an `auth0_token`, then
search the GS API across **common production regions**.

**Short-circuit strategy — minimize wasted API calls:**
1. If user provided a vendor hint (e.g. "it's on AWS"), scan **only that vendor** → found? done. Not found? → Tier 3 immediately.
2. If no hint, scan vendors in order: `aws` → `gcp` → `alicloud`. **Stop after the first vendor that returns a result.**
3. Within each vendor, scan top-3 high-traffic regions first. Only expand to remaining regions if top-3 miss.
4. If first vendor's full scan returns nothing, **jump to Tier 3** (ask user) — do NOT scan all 3 vendors exhaustively. The user almost certainly knows which cloud they're on.

**Regions to scan per vendor** (high-traffic first):

| Vendor | Top 3 (scan first) | Remaining (scan only if top-3 miss) |
|--------|-------------------|--------------------------------------|
| `aws` | `us-east-1`, `us-west-2`, `ap-southeast-1` | `ap-northeast-1`, `eu-central-1`, `eu-west-1` |
| `gcp` | `us-central1`, `us-east1`, `asia-southeast1` | `asia-northeast1`, `europe-west1` |
| `alicloud` | `cn-beijing`, `cn-shanghai`, `ap-southeast-1` | `cn-hangzhou` |

**BizTypes to try per region**: `tidbcloud`, `prod-tidbcloud-nextgen`, `prod-devtier`

```bash
# Example: search for a cluster across one vendor/region/bizType
curl -s --max-time 5 \
  "http://www.gs.us-west-2.aws.observability.tidbcloud.com/api/v1/clusters/" \
  -G --data-urlencode "bizType=tidbcloud" \
  --data-urlencode "vendor=aws" --data-urlencode "region=us-east-1" \
  --data-urlencode "applicationID=${CLUSTER_ID}" \
  -H "Authorization: Bearer ${AUTH0_TOKEN}"
```

If found, extract `vendor`, `region`, `bizType` and proceed.

> Shell helper (merged from legacy/o11y-api.md §2): `o11y_find_cluster_record
> <vendor> <region> <clusterID>` wraps this scan — it walks the common regions
> per vendor and tries bizTypes `tidbcloud`, `prod-tidbcloud-nextgen`,
> `prod-devtier` automatically.

### Tier 3: Ask the user (immediate — do NOT keep searching)

If Tier 1 and Tier 2 both fail, **stop and ask the user immediately**:

> "无法在常见区域中找到集群 {CLUSTER_ID}。请提供以下信息：
> 1. 哪朵云？（aws / gcp / alicloud / azure）
> 2. 哪个 region？
> 3. 集群类型？（dedicated / premium / starter / essential / byoc）"

**Do NOT** attempt exhaustive searches across all regions — it wastes time and
tokens. The user almost certainly knows which cloud and region their cluster is in.

### Required Information Summary

| Parameter | Required | How to obtain |
|-----------|----------|---------------|
| `GITHUB_PAT` | **Always** | `.env`, `~/.observability/.github`, or ask user |
| `provider` | **Always** | Clinic API → GS scan → ask user. `azure` **NOT supported** — stop. |
| `region` | **Always** | Clinic API → GS scan → ask user |
| `deploy type` | **Always** | Clinic API → infer from `bizType` → ask user |
| Cluster ID | Per-query | User provides |
| Tenant ID | Per-query | From GS cluster API response |
| Project ID | Per-query | From GS cluster API response |

### BizType / Deploy Type Inference

| bizType contains | deploy type |
|------------------|-------------|
| `tidbcloud` (no `nextgen`/`devtier`) | `dedicated` |
| `tidbcloud-nextgen` | `premium` |
| `devtier` | `starter` / `essential` |

---

## Step 1: Authenticate

The auth endpoint requires a `provider` parameter. Each provider returns
different storage credentials.

```bash
# Authenticate with provider
curl -s -X POST "https://www.ds.us-west-2.aws.observability.tidbcloud.com/client/auth" \
  -H 'Content-Type: application/json' \
  -d '{"github_token": "${GITHUB_PAT}", "provider": "${PROVIDER}"}'
```

### Response by provider

| Provider | Storage credentials | Storage type |
|----------|-------------------|--------------|
| `aws` | `access_key_id`, `secret_access_key`, `session_token` | S3 |
| `gcp` | `access_token` (OAuth `ya29.*`) | GCS |
| `alicloud` | `access_key_id`, `secret_access_key`, `security_token` | OSS |

Common fields in all responses:

| Field | Description |
|-------|-------------|
| `auth0_token` | Bearer token for GS cluster API and DS metrics API |
| `token` | Bearer token for data-export pipeline APIs |
| `provider` | Echo of requested provider |
| `expiration` | Token expiry timestamp |
| `observability_global_service` | GS API base URL |

### Auth using shell helper

```bash
source skills/o11y-data/o11y-data.sh
o11y_auth aws      # Authenticate for AWS
o11y_auth gcp      # Authenticate for GCP
o11y_auth alicloud # Authenticate for AliCloud
```

### Common Pitfalls

1. **Missing provider** — auth without provider returns 403 "Failed to validate
   github org". Always pass `provider`.
2. **Transient 403** — retry 2-3 times before concluding the PAT is invalid.
3. **Azure not supported** — if user needs azure credentials, inform them and
   stop.

---

## Step 2: Determine Deploy Type & Data Strategy

Data access paths differ significantly by deploy type. **Always confirm the
deploy type first.**

### Deploy Type Quick Reference

| Deploy Type | Logs | Metrics | Slowlogs | SQL Statements | TopSQL | Profiles | Storage |
|-------------|------|---------|----------|----------------|--------|----------|---------|
| `dedicated` | Loki + S3 aggregated | Prometheus API | S3 deltalake + diagnosis/ | S3 raw files | Yes | S3 pprof | Cloud bucket |
| `premium` | Loki + S3 aggregated | Prometheus API | S3 deltalake | S3 deltalake | Yes | S3 (physical cluster) | Cloud bucket (may have `-premium` suffix) |
| `starter`/`essential` | **Loki only** | Prometheus API | S3 raw files | S3 raw files | No | No | Cloud bucket |
| `byoc` | **Loki only** | Prometheus API | No (user's bucket) | No (user's bucket) | No | No | User-owned |
| Control Plane | Loki | Prometheus API | — | — | — | — | — |

### BizType Mapping

| deployType | Base bizType | prod | staging | dev |
|------------|-------------|------|---------|-----|
| `dedicated` | `tidbcloud` | `tidbcloud` | `staging-tidbcloud` | `dev-tidbcloud` |
| `premium` / `resource_pool` | `tidbcloud-nextgen` | `prod-tidbcloud-nextgen` | `staging-tidbcloud-nextgen` | `dev-tidbcloud-nextgen` |
| `starter` / `essential` | `devtier` | `prod-devtier` | `staging-devtier` | `dev-devtier` |

---

## Data Access: Metrics (All Deploy Types)

Metrics are accessed uniformly via Prometheus API regardless of deploy type.

### Resolve Metrics Target

```bash
source skills/o11y-data/o11y-data.sh
o11y_auth aws

# Get cluster's internalReadUri -> extract metrics UUID
o11y_cluster_info aws us-east-1 "" ${CLUSTER_ID}

# Or auto-resolve with probing
o11y_resolve_metrics_targets aws us-east-1 ${CLUSTER_ID}
```

### Prometheus Query

```bash
# query_range (time-series)
curl -s "https://www.ds.${REGION}.${VENDOR}.observability.tidbcloud.com/internal/v2/metrics/${O11Y_ID}/api/v1/query_range" \
  -G --data-urlencode "query=${PROMQL}" --data-urlencode "start=${START}" --data-urlencode "end=${END}" --data-urlencode "step=${STEP}" \
  -H "Authorization: Bearer ${AUTH0_TOKEN}"

# instant query
curl -s "https://www.ds.${REGION}.${VENDOR}.observability.tidbcloud.com/internal/v2/metrics/${O11Y_ID}/api/v1/query" \
  -G --data-urlencode "query=${PROMQL}" --data-urlencode "time=${TS}" \
  -H "Authorization: Bearer ${AUTH0_TOKEN}"
```

> Path version: `/internal/v2/metrics/` is **current** — it matches the
> `internalReadUri` returned by the GS cluster API. The older
> `/internal/metrics/` form (no `v2`) is **legacy**; prefer the v2 path.

### PromQL Templates

See [promql-templates.md](../../../engines/tidb/references/promql-templates.md) for the full PromQL reference (50+ queries by category with normal ranges and anomaly interpretation). The queries are identical across Clinic and O11Y — only the endpoint and auth differ.

### Infra vs App Metrics

- `tidb_*`, `tikv_*` → cluster's **app UUID**
- `node_*`, `kube_*` → cluster's **infra UUID**
- If `node_disk_*` returns empty on app UUID, it's a wrong source, not "no data"

**Infra UUID — legacy vs Starter/Essential/Premium mode** (merged from legacy/o11y-metrics-api.md):

- **Legacy** (`tidbcloud` bizType, Dedicated): `applicationID=k8s-infra` + `projectID`.
- **Starter/Essential/Premium** (`prod-tidbcloud-nextgen`): infra is registered as
  `applicationID=dp-{dataplane-uuid}` under the same tenant/vendor/region.
  Auto-discover: list GS clusters filtered by `tenantID`, keep `dp-*` entries;
  if several, probe each with `kube_*{namespace="tidb{clusterId}"}` (~0.5s per
  probe) — the first hit is the infra UUID. Use headers `x-org-id=<tenantID>`,
  `x-biz-type=<cluster bizType>`, `x-region-id=<vendor>-<region>`.
- The `k8s-infra` + `projectID` pattern does **not** work for Starter/Essential/Premium.

---

## Data Access: Loki Logs (All Deploy Types)

Loki provides **unified real-time log access** across all deploy types and
providers. It's the **only log source for starter/essential and BYOC**.

### Endpoint

```
https://www.ds.{region}.{vendor}.observability.tidbcloud.com/loki/self-monitoring/loki/api/v1/
```

The endpoint is per-provider and per-region: e.g.
- AWS us-east-1: `https://www.ds.us-east-1.aws.observability.tidbcloud.com/loki/self-monitoring/loki/api/v1/`
- GCP us-central1: `https://www.ds.us-central1.gcp.observability.tidbcloud.com/loki/self-monitoring/loki/api/v1/`

### Available Labels

| Label | Description | Example |
|-------|-------------|---------|
| `cluster_id` | TiDB Cloud cluster ID | `10324983984131567830` |
| `container` | Log source component | `tidb`, `tikv`, `pd`, `slowlog`, `errorlog` |
| `namespace` | Kubernetes namespace | `tidb10324983984131567830`, `tidb-serverless` |
| `tenant_id` | Tenant ID | `1372813089209061633` |
| `project_id` | Project ID | `1372813089454544954` |
| `cluster_env` | Environment | `prod`, `staging`, `dev` |
| `instance` | Pod/instance name | `db-tidb-0` |
| `app` | Application name | `db-tidb-0` |
| `node` | Kubernetes node | `ip-172-21-10-174.ec2.internal` |

### Key Container Types

| Container | Content |
|-----------|---------|
| `tidb` | TiDB server logs |
| `tikv` | TiKV storage logs |
| `pd` | PD scheduler logs |
| `tiflash` | TiFlash columnar logs |
| `slowlog` | Slow query logs (standard MySQL slowlog format) |
| `errorlog` | Error-specific logs |
| `statementlog` | SQL statement summary logs |
| `ticdc` | TiCDC change data capture logs |

### Query Examples

```bash
# TiDB logs for a specific cluster (last 10 minutes)
curl -s --globoff "${LOKI_BASE}/query_range" \
  -G --data-urlencode 'query={cluster_id="10324983984131567830", container="tidb"}' \
  --data-urlencode "start=${START}.0" --data-urlencode "end=${END}.0" \
  --data-urlencode "limit=100" \
  -H "Authorization: Bearer ${AUTH0_TOKEN}"

# Slowlogs for a cluster
curl -s --globoff "${LOKI_BASE}/query_range" \
  -G --data-urlencode 'query={cluster_id="10324983984131567830", container="slowlog"}' \
  --data-urlencode "start=${START}.0" --data-urlencode "end=${END}.0" \
  -H "Authorization: Bearer ${AUTH0_TOKEN}"

# Error logs with grep filter
curl -s --globoff "${LOKI_BASE}/query_range" \
  -G --data-urlencode 'query={cluster_id="10324983984131567830", container="tidb"} |= "ERROR"' \
  --data-urlencode "start=${START}.0" --data-urlencode "end=${END}.0" \
  -H "Authorization: Bearer ${AUTH0_TOKEN}"

# List available labels
curl -s "${LOKI_BASE}/labels" -H "Authorization: Bearer ${AUTH0_TOKEN}"

# Get label values
curl -s "${LOKI_BASE}/label/container/values" -H "Authorization: Bearer ${AUTH0_TOKEN}"
```

---

## Data by Deploy Type: Dedicated

**Bucket**: `o11y-prod-shared-{region}`

### Logs (S3 aggregated + Loki)

```
diagnosis/data/{clusterId}/
├── log-stats/
│   └── 1h/
│       └── {YYYYMMDDHH}.db          # SQLite: hourly log statistics
├── merged-logs/
│   ├── {component}/{YYYYMMDDHH}.log  # Old format
│   └── {YYYYMMDDHH}/                # New format
│       ├── pd/db-pd-0.log
│       ├── tidb/db-tidb-0.log
│       ├── tikv/db-tikv-0.log
│       └── tiflash/...
```

Also available via Loki: `{cluster_id="{id}", container="tidb"}` etc.

### Slowlogs

Two sources:

1. **Delta Lake** (historical, aggregated):
   ```
   deltalake/{tenantId}/{o11yId}/slowlogs/     # Parquet + _delta_log/
   ```

2. **Aggregated stats** (recent ~1 month):
   ```
   diagnosis/data/{clusterId}/
   ├── slowlog-stats/
   │   ├── 1h/{YYYYMMDDHH}.db     # SQLite: hourly
   │   ├── 3h/ / 6h/ / 12h/ / 24h/
   └── slowlog-merged/
   ```

3. **Raw recent** (latest data, very large):
   ```
   0/{tenantId}/{projectId}/{clusterId}/logs/slowlog/
   ```
   Files named `{unix_timestamp}-{uuid}.log.gz`. **ALWAYS use timestamp prefix
   to filter when listing** — file volume is extremely large.

Also available via Loki: `{cluster_id="{id}", container="slowlog"}`

### SQL Statements

```
0/{tenantId}/{projectId}/{clusterId}/logs/statementlog/
```

Files named `{unix_timestamp}-{...}.gz`. Each instance uploads one file per 30
minutes. Use timestamp prefix filtering when listing.

File format: `timestamp;host;cluster;instance;statementlog {JSON}`

### Profiles (pprof)

```
0/{tenantId}/{projectId}/{clusterId}/profiles/
```

File naming: `{timestamp}-{component}-{profile_type}-{base64_encoded_instance_name}.log.gz`

Example: `1770508800-pd-cpu-ZGItcGQtMC5kYi1wZC1wZWVyLnRpZGIxMDMyNDk4Mzk4NDEzMTU2NzgzMC5zdmM6MjM3OQ.log.gz`

Components: `pd`, `tidb`, `tikv`, `tiflash`
Profile types: `cpu`, `heap`, `goroutine`, `mutex`, etc.
These are standard pprof format output.

---

## Data by Deploy Type: Premium

Premium has **logical clusters** (tenant-visible) and **physical clusters**
(shared pool). A physical cluster hosts multiple logical clusters. TiDB is
dedicated per logical cluster; other components (TiKV, PD) are shared.

**Bucket**: `o11y-prod-shared-{region}` or `o11y-prod-shared-{region}-premium`
(some regions use the `-premium` suffix)

### Logs (S3 aggregated + Loki)

```
diagnosis/data/{logicalClusterId}/
├── log-stats/
│   └── 1h/{YYYYMMDDHH}.db
└── merged-logs/
    └── {YYYYMMDDHH}/{component}/{instance}.log
```

Also available via Loki.

### Slowlogs (Delta Lake)

```
deltalake/{tenantId}/{o11yClusterId}/slowlogs/
```

Queried via DuckDB same as Dedicated.

### SQL Statements (Delta Lake)

```
deltalake/{tenantId}/{o11yClusterId}/persisted_statements_summary/
```

### Profiles (physical cluster)

Since profiles don't contain business data, they are stored under the **physical
(pool) cluster**:

```
0/shared_pool/{poolClusterId}/profiles/
```

Same file naming convention as Dedicated.

---

## Data by Deploy Type: Starter / Essential

Serverless clusters. **No aggregated log-stats, no TopSQL, no profiles.**

### Logs (Loki — pool-level with keyspace filtering)

Serverless clusters run in **shared pools**. In Loki, the `cluster_id` label
contains the pool name (e.g. `tidb-pool`), **NOT** the application cluster ID.
To get logs for a specific serverless cluster:

1. **Find the pool info** from the metrics labels — query any metric
   (e.g. `tidb_server_connections`) and extract:
   - `cluster` label → pool cluster name (e.g. `prod-us-west-2-f03`)
   - `keyspace_id` label → keyspace ID for this application cluster
   - `namespace` label → typically `tidb-pool` or `super-vip-tidb-pool`
2. **Query Loki** with pool-level labels + content filter on `keyspaceID`:

```bash
# TiDB logs for a serverless cluster
{namespace="tidb-pool", container="tidb", cluster="prod-us-west-2-f03"} |= "keyspaceID=4808"

# Slowlogs for a serverless cluster
{container="slowlog", cluster="prod-us-west-2-f03"} |= "Keyspace_ID: 4808"

# Statement logs
{container="statementlog", cluster="prod-us-west-2-f03"} |= "4808"
```

> **Key point**: `cluster_id` label-based filtering (used for Dedicated/Premium)
> does NOT work for Serverless. You MUST use content-level `keyspaceID` filtering.

### Slowlogs (S3 raw)

```
0/prod-devtier/serverless/{tenantId}/{projectId}/{clusterId}/logs/slowlog/
```

Files: `{unix_timestamp}-{uuid}.log.gz`

Also queryable via Loki: `{cluster_id="{id}", container="slowlog"}`

### SQL Statements (S3 raw)

```
0/prod-devtier/serverless/{tenantId}/{projectId}/{clusterId}/logs/statement/
```

Same file format as Dedicated's statementlog.

### Not Available

- Log-stats SQLite (use Loki instead)
- TopSQL
- Profiles
- Merged aggregated logs

---

## Data by Deploy Type: BYOC

BYOC clusters store data in the **user's own bucket**. O11Y only has:

### Available

- **Metrics**: via Prometheus API (same as other types)
- **Loki logs**: via Loki API (same endpoint pattern)

### Not Available (data is in user's bucket)

- S3 log-stats
- S3 slowlogs
- S3 statement logs
- S3 profiles
- Delta Lake data

---

## Control Plane Data

Each project has a control plane cluster with `applicationID=k8s-infra`
(legacy `tidbcloud` bizType only; Starter/Essential/Premium environments register infrastructure
as `dp-{dataplane-uuid}` — see "Infra vs App Metrics" above).

### Metrics

```bash
# Resolve infra metrics target
o11y_gs_query ${VENDOR} ${REGION} "bizType=tidbcloud" "applicationID=k8s-infra" "projectID=${PROJECT_ID}"
```

### Logs

Via Loki with infrastructure-related containers: `kube-state-metrics`,
`node-exporter`, `prometheus`, `cert-manager`, etc.

---

## GS Cluster Info API

```bash
curl -s "http://www.gs.us-west-2.aws.observability.tidbcloud.com/api/v1/clusters/" \
  -G --data-urlencode "bizType=${BIZ_TYPE}" --data-urlencode "vendor=${VENDOR}" \
  --data-urlencode "region=${REGION}" --data-urlencode "applicationID=${CLUSTER_ID}" \
  -H "Authorization: Bearer ${AUTH0_TOKEN}"
```

Response fields per cluster:

| Field | Description |
|-------|-------------|
| `id` | O11Y UUID (for metrics API and deltalake path) |
| `tenantID` | Tenant numeric ID |
| `projectID` | Project numeric ID |
| `applicationID` | TiDB Cloud cluster ID |
| `region` | Cluster region |
| `vendor` | Cloud vendor |
| `bizType` | Business type |
| `internalReadUri` | Metrics endpoint (contains metrics UUID) |

---

## Storage Bucket Naming

| Provider | Bucket pattern |
|----------|----------------|
| AWS | `o11y-prod-shared-{region}` |
| AWS (Premium) | `o11y-prod-shared-{region}` or `o11y-prod-shared-{region}-premium` |
| GCP | `o11y-prod-shared-{region}` |

---

## Data Format Details

### log-stats SQLite (`YYYYMMDDHH.db`)

Table: `log_stats`

| Column | Type | Description |
|--------|------|-------------|
| `component` | TEXT | `pd`, `tikv`, `tidb`, `tiflash` |
| `level` | TEXT | `ERROR`, `WARN`, `INFO`, `DEBUG` |
| `position` | TEXT | Source location `file:line` |
| `msg_digest` | TEXT | Log message digest/pattern |
| `count` | INTEGER | Occurrence count in this hour |
| `sample` | TEXT | Sample log line |

### Delta Lake Slowlogs (Parquet)

Access via DuckDB:

```sql
INSTALL httpfs; LOAD httpfs;
INSTALL delta; LOAD delta;
-- For AWS:
INSTALL aws; LOAD aws;
CREATE SECRET (TYPE s3, PROVIDER credential_chain, ENDPOINT 's3.{region}.amazonaws.com');
-- For GCP:
INSTALL gcs; LOAD gcs;

SELECT digest, first(query), avg(query_time), count(*)
FROM delta_scan('s3://o11y-prod-shared-{region}/deltalake/{tenantId}/{o11yId}/slowlogs')
WHERE time >= {start_ts} AND time < {end_ts}
GROUP BY digest ORDER BY avg(query_time) DESC LIMIT 20;
```

Key fields: `time` (unix ts), `digest`, `query`, `query_time` (seconds),
`plan_digest`, `decoded_plan`, `mem_max`, `succ`, `stats`

**DuckDB analysis scenario checklist** (merged from legacy/o11y-api.md §3):

1. **Top slow SQL** — `GROUP BY digest`, order by `MAX(query_time)` DESC
2. **Digest statistics** — total queries, `COUNT(DISTINCT digest)`, avg/max `query_time`
3. **Multi-day time-range analysis** — `WHERE date IN ('{d1}', '{d2}')` (date partition filter is mandatory: `WHERE date = 'YYYYMMDD'`)
4. **High resource consumption** — filter `mem_max > {threshold}`, rank by `MAX(mem_max)` / `MAX(disk_max)` / `MAX(CAST(total_keys AS BIGINT))`
5. **Backoff analysis (lock contention)** — filter non-empty `backoff_types`, rank by exec count

DuckDB syntax notes vs Clinic Athena SQL: table is `slowlogs`, identifiers need
no double quotes, timestamp conversion via `to_timestamp(time)`, `first(col)` /
`any_value(col)` both work.

Built-in anomaly detection (merged from legacy/o11y-api.md §3):
`app/logcli/main.py slowlog detect run --start-ts ... --end-ts ...
--comparison-start-ts ... --comparison-end-ts ...` with rules
`new_slow_query` (RuleNewSlowQuery: slow SQL absent from comparison window) and
`high_time_variability` (RuleHighTimeVariability: query-time variability spike).

### Delta Lake SQL Statements (Premium)

```sql
SELECT DIGEST, first(DIGEST_TEXT), SUM(EXEC_COUNT), AVG(AVG_LATENCY), MAX(MAX_LATENCY)
FROM delta_scan('s3://o11y-prod-shared-{region}/deltalake/{tenantId}/{o11yId}/persisted_statements_summary')
WHERE _vector_timestamp >= '{start_iso}' AND _vector_timestamp < '{end_iso}'
GROUP BY DIGEST ORDER BY AVG(AVG_LATENCY) DESC LIMIT 15;
```

### Raw Statement Log Format (Dedicated / Starter)

Files: `{timestamp}-{uuid}.log.gz`
Lines: `timestamp;host;cluster;instance;statementlog {JSON}`

JSON fields: `digest`, `stmt_type`, `schema_name`, `normalized_sql`,
`exec_count`, `sum_latency`, `max_latency`, `max_mem`

### Profile Data (pprof)

Standard Go pprof format. Download and analyze with:

```bash
# Download a profile
aws s3 cp "s3://o11y-prod-shared-{region}/0/{tenantId}/{projectId}/{clusterId}/profiles/{filename}" ./profile.log.gz
gunzip profile.log.gz
go tool pprof profile.log
```

---

## Quick: Cluster Inspection (One Command)

For "inspect/check/diagnose cluster X" requests, use the ready-made inspection
script:

```bash
python skills/o11y-data/o11y-inspect.py <CLUSTER_ID> --provider aws

# With options
python skills/o11y-data/o11y-inspect.py <CLUSTER_ID> --provider aws --hours 6 --region us-east-1

# Save report
python skills/o11y-data/o11y-inspect.py <CLUSTER_ID> --provider aws --output report.json
```

Output includes: Metrics, Log Stats, Slowlogs, SQL Statements, Anomaly Detection.

**IMPORTANT**: Always prefer this script over writing inline code for inspection
tasks.

---

## Shell Helper Reference

```bash
source skills/o11y-data/o11y-data.sh

# Cluster lookup (Tier 1 — Clinic API)
source .env
curl -s --max-time 5 \
  "https://clinic.pingcap.com/api/v1/clusters/${CLUSTER_ID}" \
  -H "Authorization: Bearer ${CLINIC_API_TOKEN}"

# Authentication (all three clouds)
o11y_auth aws                                    # Auth with AWS provider
o11y_auth gcp                                    # Auth with GCP provider
o11y_auth alicloud                               # Auth with AliCloud provider

# Cluster info (Tier 2 — GS API scan)
o11y_cluster_info aws us-west-2                  # All clusters in region
o11y_cluster_info aws us-west-2 "" ${CLUSTER_ID} # Specific cluster
o11y_cluster_info gcp us-central1 "" ${CLUSTER_ID}
o11y_cluster_info alicloud cn-beijing "" ${CLUSTER_ID}
o11y_resolve_metrics_targets aws us-east-1 ${CLUSTER_ID}

# Metrics
o11y_metrics_query "$O11Y_ID" "up" "$START" "$END" 60 us-west-2
o11y_metrics_instant_query "$O11Y_ID" 'count(node_disk_io_time_seconds_total)' "" us-east-1

# S3
o11y_s3_list "diagnosis/data/${CLUSTER_ID}/" us-west-2
o11y_s3_download "diagnosis/data/${CLUSTER_ID}/log-stats/1h/2026030500.db" ./local.db us-west-2

# Loki (Dedicated/Premium — filter by cluster_id label)
o11y_loki_query us-east-1 aws '{cluster_id="10324983984131567830", container="tidb"}' ${START} ${END} 100

# Loki (Serverless — filter by keyspaceID in content)
o11y_loki_query us-west-2 aws '{namespace="tidb-pool", container="tidb", cluster="prod-us-west-2-f03"} |= "keyspaceID=4808"' ${START} ${END} 100

# BizType resolver
o11y_resolve_biz_type "premium" "prod"   # -> prod-tidbcloud-nextgen
o11y_resolve_biz_type "dedicated" "prod" # -> tidbcloud
o11y_resolve_biz_type "starter" "prod"   # -> prod-devtier
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 403 "Failed to validate github org" | Missing `provider` parameter in auth request |
| 401 Unauthorized | Token expired — re-run `o11y_auth` |
| S3 Access Denied | STS credentials expired — re-authenticate |
| Azure provider | Not supported — inform user and stop |
| Cluster not found in quick scan | **Ask user immediately** for provider/region — do NOT exhaustively search |
| Clinic API returns HTML/403 | Outside corporate network (no VPN / not on managed EC2) — fall through to GS API scan, or enable Feilian VPN |
| Empty log-stats | Dedicated/Premium only; Starter/Essential use Loki |
| Serverless Loki returns 0 results | `cluster_id` label is pool-level; use `keyspaceID` content filter instead |
| DuckDB Delta error | Ensure correct cloud credentials are set before DuckDB |
| Logs not in diagnosis/ | Starter/Essential use `0/prod-devtier/serverless/` path |
| Missing GITHUB_PAT | Set in `.env` or `~/.observability/.github` |
| Huge file listing | Use timestamp prefix filtering for raw slowlog/statementlog |
| Premium wrong bucket | Some regions use `o11y-prod-shared-{region}-premium` |
