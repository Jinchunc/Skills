#!/usr/bin/env python3
"""Clinic API Python helper — zero external dependencies (stdlib only).

Usage:
    from clinic_api import ClinicAPI
    api = ClinicAPI()                       # reads .env automatically
    api.query_range("CLUSTER_ID", "sum(rate(tidb_server_query_total[1m]))", start, end)
    api.query_sql("CLUSTER_ID", "SELECT ... FROM clinic_data_proxy.slow_query_logs WHERE date='20260405'")
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ENV_ENDPOINTS = {
    "dev": "https://dev-clinic.pingcap.com",
    "staging": "https://staging-clinic.pingcap.com",
    "prod": "https://clinic.pingcap.com",
}


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE, no quotes handling needed)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if k and k not in os.environ:
            os.environ[k] = v


class ClinicAPI:
    """Thin wrapper around Clinic Data Proxy HTTP APIs."""

    def __init__(self, env_file: str = ".env"):
        _load_dotenv(env_file)
        clinic_env = os.environ.get("CLINIC_ENV", "prod")
        self.endpoint = os.environ.get(
            "CLINIC_ENDPOINT", _ENV_ENDPOINTS.get(clinic_env, _ENV_ENDPOINTS["prod"])
        )
        self.api_base = f"{self.endpoint}/clinic/api/v1"
        self.api_key = os.environ.get("CLINIC_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("CLINIC_API_KEY not set. Check .env or environment.")
        self._canonical_org_ids: dict[str, str] = {}

    # ── helpers ──────────────────────────────────────────────

    def _get(
        self,
        path: str,
        params: dict | None = None,
        extra_headers: dict | None = None,
    ) -> dict:
        qs = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.endpoint}{path}" + (f"?{qs}" if qs else "")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, headers=headers)
        return self._do(req)

    def _api_get(self, path: str, params: dict | None = None) -> dict:
        qs = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.api_base}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}"})
        return self._do(req)

    def _post_json(self, path: str, body: object) -> dict | list:
        url = f"{self.endpoint}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        return self._do(req)

    @staticmethod
    def _do(req: urllib.request.Request) -> dict | list:
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            return {"error": f"HTTP {e.code}: {body[:500]}"}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _path_segment(value: str) -> str:
        return urllib.parse.quote(str(value), safe="")

    def get_cluster_by_id(self, cluster_id: str) -> dict:
        """Canonical exact metadata → GET /data-proxy/api/v1/clusters/{cluster_id}."""
        return self._get(
            f"/data-proxy/api/v1/clusters/{self._path_segment(cluster_id)}"
        )

    def _canonical_org_id(self, cluster_id: str) -> str:
        cluster_id = str(cluster_id)
        cached = self._canonical_org_ids.get(cluster_id)
        if cached:
            return cached
        metadata = self.get_cluster_by_id(cluster_id)
        org_id = metadata.get("orgId") if isinstance(metadata, dict) else None
        if not org_id:
            raise RuntimeError(f"Could not resolve DBAAS org ID for cluster {cluster_id}")
        self._canonical_org_ids[cluster_id] = str(org_id)
        return str(org_id)

    def _canonical_cluster_path(self, cluster_id: str, suffix: str = "") -> str:
        org_id = self._canonical_org_id(cluster_id)
        path = (
            f"/data-proxy/api/v1/orgs/{org_id}/clusters/"
            f"{self._path_segment(cluster_id)}"
        )
        return f"{path}/{suffix.lstrip('/')}" if suffix else path

    # ── Prometheus metrics ───────────────────────────────────

    def query_range(
        self,
        cluster_id: str,
        query: str,
        start: int | str,
        end: int | str,
        step: str = "60",
    ) -> dict:
        """PromQL range query through the canonical org/cluster-scoped API."""
        return self._get(
            self._canonical_cluster_path(cluster_id, "metrics/query-range"),
            {"query": query, "start": start, "end": end, "step": step},
        )

    def query_instant(
        self,
        cluster_id: str,
        query: str,
        time: int | str | None = None,
    ) -> dict:
        """PromQL instant query through the canonical org/cluster-scoped API."""
        params: dict = {"query": query}
        if time is not None:
            params["time"] = time
        return self._get(self._canonical_cluster_path(cluster_id, "metrics/query"), params)

    def query_series(
        self,
        cluster_id: str,
        match: str,
        start: int | str,
        end: int | str,
    ) -> dict:
        """Series discovery through the canonical org/cluster-scoped API."""
        return self._get(
            self._canonical_cluster_path(cluster_id, "metrics/series"),
            {"match[]": match, "start": start, "end": end},
        )

    def loki_query_range(
        self,
        cluster_id: str,
        query: str,
        start_ns: int,
        end_ns: int,
        limit: int = 200,
    ) -> dict:
        """Loki log range query through the data-proxy loki endpoint — mirrors the
        dedicated monolith's InspectionClient.loki_query_range (non-canonical,
        cluster-scoped loki path). ``start_ns``/``end_ns`` are epoch nanoseconds.
        Fail-closed: returns a dict with an ``error`` key on failure (via _do)."""
        return self._get(
            f"/data-proxy/loki/{self._path_segment(cluster_id)}/api/v1/query_range",
            {
                "query": query,
                "start": start_ns,
                "end": end_ns,
                "limit": limit,
                "direction": "backward",
            },
        )

    def legacy_query_range(
        self,
        cluster_id: str,
        query: str,
        start: int | str,
        end: int | str,
        step: str = "60",
    ) -> dict:
        """Compatibility call to legacy cluster-only metrics query_range."""
        return self._get(
            f"/data-proxy/metrics/{cluster_id}/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )

    def legacy_query_instant(
        self,
        cluster_id: str,
        query: str,
        time: int | str | None = None,
    ) -> dict:
        """Compatibility call to legacy cluster-only metrics query."""
        params: dict = {"query": query}
        if time is not None:
            params["time"] = time
        return self._get(f"/data-proxy/metrics/{cluster_id}/api/v1/query", params)

    def legacy_query_series(
        self,
        cluster_id: str,
        match: str,
        start: int | str,
        end: int | str,
    ) -> dict:
        """Compatibility call to legacy cluster-only metrics series."""
        return self._get(
            f"/data-proxy/metrics/{cluster_id}/api/v1/series",
            {"match[]": match, "start": start, "end": end},
        )

    def build_node_to_pod_map(
        self,
        cluster_id: str,
        namespace: str,
        pod_pattern: str,
        start: int | str,
        end: int | str,
    ) -> dict:
        """Discover `node -> pod` mapping from kube_pod_info via /series."""
        result = self.query_series(
            cluster_id,
            f'kube_pod_info{{namespace="{namespace}", pod=~"{pod_pattern}"}}',
            start,
            end,
        )
        if result.get("status") != "success":
            return result

        mapping: dict[str, str] = {}
        for series in result.get("data", []):
            node = series.get("node")
            pod = series.get("pod")
            if node and pod:
                mapping[node] = pod

        return {
            "status": "success",
            "data": {
                "resultType": "map",
                "result": [{"node": node, "pod": pod} for node, pod in sorted(mapping.items())],
            },
        }

    def query_range_join_instance_to_pod(
        self,
        cluster_id: str,
        query: str,
        namespace: str,
        pod_pattern: str,
        start: int | str,
        end: int | str,
        step: str = "60",
        instance_label: str = "instance",
        pod_label: str = "pod",
        source_pod_label: str = "source_pod",
    ) -> dict:
        """Run a range query, then attach pod labels client-side via `instance -> node -> pod`.

        This is intended for infrastructure metrics where a server-side PromQL join against
        `kube_pod_info` may return empty through Clinic Data Proxy even though both sides
        individually have data.
        """
        mapping_result = self.build_node_to_pod_map(
            cluster_id, namespace, pod_pattern, start, end
        )
        if mapping_result.get("status") != "success":
            return mapping_result

        node_to_pod = {
            item["node"]: item["pod"]
            for item in mapping_result.get("data", {}).get("result", [])
            if item.get("node") and item.get("pod")
        }

        raw = self.query_range(cluster_id, query, start, end, step=step)
        if raw.get("status") != "success":
            return raw

        joined = []
        for series in raw.get("data", {}).get("result", []):
            metric = dict(series.get("metric", {}))
            instance = metric.get(instance_label)
            pod = node_to_pod.get(instance)
            if not pod:
                continue

            if pod_label in metric and metric[pod_label] != pod:
                metric[source_pod_label] = metric[pod_label]
            metric[pod_label] = pod

            joined.append({"metric": metric, "values": series.get("values", [])})

        return {
            "status": "success",
            "data": {
                "resultType": raw.get("data", {}).get("resultType", "matrix"),
                "result": joined,
            },
        }

    def query_exceeded_iops_by_pod(
        self,
        cluster_id: str,
        component: str,
        start: int | str,
        end: int | str,
        step: str = "60",
        rate_window: str = "2m",
        instance_filter: str = ".*",
        namespace: str | None = None,
    ) -> dict:
        """Client-side joined `aws_ebs_csi_exceeded_iops_seconds_total` by storage pod.

        `component` must be `tikv` or `tiflash`.
        Use the same `rate_window` as the dashboard's `$__rate_interval` when comparing results.
        """
        patterns = {"tikv": ".*tikv.*", "tiflash": ".*tiflash.*"}
        if component not in patterns:
            return {"error": f"unsupported component: {component}"}

        resolved_namespace = namespace or f"tidb{cluster_id}"
        query = (
            f'rate(aws_ebs_csi_exceeded_iops_seconds_total'
            f'{{instance=~"{instance_filter}"}}[{rate_window}])'
        )
        return self.query_range_join_instance_to_pod(
            cluster_id,
            query,
            resolved_namespace,
            patterns[component],
            start,
            end,
            step=step,
        )

    def query_node_disk_iops_by_pod(
        self,
        cluster_id: str,
        component: str,
        start: int | str,
        end: int | str,
        step: str = "60",
        rate_window: str = "1m",
        instance_filter: str = ".*",
        namespace: str | None = None,
    ) -> dict:
        """Client-side joined node disk IOPS by storage pod."""
        patterns = {"tikv": ".*tikv.*", "tiflash": ".*tiflash.*"}
        if component not in patterns:
            return {"error": f"unsupported component: {component}"}

        resolved_namespace = namespace or f"tidb{cluster_id}"
        query = (
            f'('
            f'rate(node_disk_reads_completed_total{{instance=~"{instance_filter}"}}[{rate_window}]) + '
            f'rate(node_disk_writes_completed_total{{instance=~"{instance_filter}"}}[{rate_window}])'
            f')'
        )
        return self.query_range_join_instance_to_pod(
            cluster_id,
            query,
            resolved_namespace,
            patterns[component],
            start,
            end,
            step=step,
        )

    # ── Data Proxy SQL (slow query / TopSQL / LogStats) ──────

    def query_sql(
        self,
        cluster_id: str,
        sql: str,
        timeout: int = 60,
    ) -> dict:
        """SQL query through canonical POST .../sql-queries.

        Note: ``rows`` in the response may be ``None`` when no rows match.
        This method normalises it to ``[]`` so callers can safely iterate.
        All row values are **strings** regardless of column type.
        """
        result = self._post_json(
            self._canonical_cluster_path(cluster_id, "sql-queries"),
            {"sql": sql, "timeout": timeout},
        )
        if isinstance(result, dict) and result.get("rows") is None:
            result["rows"] = []
        return result

    def query_sql_batch(self, cluster_id: str, queries: list[dict]) -> dict | list:
        """Run up to 100 same-cluster canonical SQL queries in request order."""
        return self._post_json(
            self._canonical_cluster_path(cluster_id, "sql-query-batches"),
            queries,
        )

    def legacy_query_sql(
        self,
        cluster_id: str,
        sql: str,
        timeout: int = 60,
    ) -> dict:
        """Compatibility call to POST /data-proxy/query."""
        result = self._post_json(
            "/data-proxy/query",
            {"sql": sql, "clusterId": cluster_id, "timeout": timeout},
        )
        if isinstance(result, dict) and result.get("rows") is None:
            result["rows"] = []
        return result

    # ── Schema ───────────────────────────────────────────────

    def get_schema(self, cluster_id: str, tables: str = "slow_query_logs") -> dict:
        """Table schemas through canonical GET .../table-schemas."""
        return self._get(
            self._canonical_cluster_path(cluster_id, "table-schemas"),
            {"tables": tables},
        )

    def get_capabilities(self) -> dict:
        """Service capabilities → GET /data-proxy/api/v1/capabilities."""
        return self._get("/data-proxy/api/v1/capabilities")

    def legacy_get_schema(
        self,
        cluster_id: str,
        tables: str = "slow_query_logs,topsql",
    ) -> dict:
        """Compatibility call to GET /data-proxy/schema."""
        return self._get("/data-proxy/schema", {"tables": tables, "clusterId": cluster_id})

    # ── NGM slow query (starter / essential / shared) ────────

    def search_cluster_by_id(
        self,
        cluster_id: str,
        show_deleted: bool = True,
        limit: int = 10,
    ) -> dict:
        """Legacy Dashboard lookup used only for NGM context discovery."""
        return self._api_get(
            "/dashboard/clusters",
            {
                "cluster_id": cluster_id,
                "show_deleted": str(show_deleted).lower(),
                "limit": limit,
                "page": 1,
            },
        )

    def get_cluster(self, org_id: str, cluster_id: str) -> dict:
        """Canonical cluster detail; org_id is the DBAAS/TiDB Cloud org ID."""
        return self._get(
            f"/data-proxy/api/v1/orgs/{org_id}/clusters/{self._path_segment(cluster_id)}"
        )

    def list_clusters(
        self,
        org_id: str,
        query: str = "",
        show_deleted: bool = False,
        page_size: int = 50,
        page: int = 1,
    ) -> dict:
        """Canonical org-scoped cluster list."""
        return self._get(
            f"/data-proxy/api/v1/orgs/{org_id}/clusters",
            {
                "query": query,
                "showDeleted": str(show_deleted).lower(),
                "pageSize": page_size,
                "page": page,
            },
        )

    def get_components(self, org_id: str, cluster_id: str) -> dict | list:
        """Canonical topology/components for supported non-shared tiers."""
        return self._get(
            f"/data-proxy/api/v1/orgs/{org_id}/clusters/"
            f"{self._path_segment(cluster_id)}/components"
        )

    def get_activities(
        self,
        org_id: str,
        cluster_id: str,
        begin_ts: int | str,
        end_ts: int | str,
        limit: int = 100,
        cursor: str = "",
        catalog: str = "",
        activity_type: str = "",
        name: str = "",
        severity: int | None = None,
    ) -> dict:
        """Canonical Activity Events; pagination cursor is a query parameter."""
        params: dict = {"begin_ts": begin_ts, "end_ts": end_ts, "limit": limit}
        optional = {
            "cursor": cursor,
            "catalog": catalog,
            "type": activity_type,
            "name": name,
        }
        params.update({key: value for key, value in optional.items() if value != ""})
        if severity is not None:
            params["severity"] = severity
        return self._get(
            f"/data-proxy/api/v1/orgs/{org_id}/clusters/"
            f"{self._path_segment(cluster_id)}/activities",
            params,
        )

    def top_ru_summary(
        self,
        cluster_id: str,
        start: int | str,
        end: int | str,
        top: int,
        order_by: str | None = None,
        data_source: str | None = None,
        step: str | None = None,
        user: str | None = None,
    ) -> dict:
        """Canonical Top RU summary for supported Premium/Essential clusters."""
        params: dict = {"start": start, "end": end, "top": top}
        for key, value in (
            ("order_by", order_by),
            ("data_source", data_source),
            ("step", step),
            ("user", user),
        ):
            if value is not None:
                params[key] = value
        return self._get(self._canonical_cluster_path(cluster_id, "top-ru/summary"), params)

    def top_ru_user_stats(
        self,
        cluster_id: str,
        start: int | str,
        end: int | str,
        top: int | None = None,
    ) -> dict:
        """Canonical Top RU user statistics."""
        params: dict = {"start": start, "end": end}
        if top is not None:
            params["top"] = top
        return self._get(
            self._canonical_cluster_path(cluster_id, "top-ru/user-stats"),
            params,
        )

    def legacy_get_cluster(self, clinic_internal_org_id: str, cluster_id: str) -> dict:
        """Compatibility call to Clinic detail using its internal org primary key."""
        return self._api_get(
            f"/orgs/{clinic_internal_org_id}/clusters/{cluster_id}"
        )

    legacy_search_cluster_by_id = search_cluster_by_id

    @staticmethod
    def _normalize_ngm_deploy_type(deploy_type: str) -> str:
        value = deploy_type.lower().replace(" ", "_")
        if value in {"starter", "essential", "shared"}:
            return "shared"
        return value

    def _ngm_context_from_cluster_id(self, cluster_id: str) -> dict:
        result = self.search_cluster_by_id(cluster_id, show_deleted=True)
        items = result.get("items") or []
        item = next((x for x in items if str(x.get("clusterID")) == str(cluster_id)), None)
        if not item:
            raise RuntimeError(f"Cluster not found: {cluster_id}")

        def pick(*names: str) -> str:
            for name in names:
                value = item.get(name)
                if value not in (None, "", "null"):
                    return str(value)
            return ""

        org_id = pick("orgID")
        detail = None
        fallback_keys = (
            ("tenantID",),
            ("clusterDeployType",),
            ("projectID", "projectId"),
            ("cloudProvider", "provider", "vendor", "clusterProviderName"),
            ("region", "clusterRegionName"),
        )
        if org_id and any(not pick(*names) for names in fallback_keys):
            detail = self.legacy_get_cluster(org_id, cluster_id)

        def pick_detail(*names: str) -> str:
            nonlocal detail
            value = pick(*names)
            if value:
                return value
            if not detail:
                return ""
            for name in names:
                detail_value = detail.get(name)
                if detail_value not in (None, "", "null"):
                    return str(detail_value)
            return ""

        context = {
            "x-cluster-id": cluster_id,
            "x-org-id": pick_detail("tenantID"),
            "x-project-id": pick_detail("projectID", "projectId"),
            "x-deploy-type": self._normalize_ngm_deploy_type(
                pick_detail("clusterDeployType", "deployType")
                or pick_detail("clusterDeployTypeV2", "deployTypeV2")
            ),
            "x-provider": pick_detail("cloudProvider", "provider", "vendor", "clusterProviderName").lower(),
            "x-region": pick_detail("region", "clusterRegionName"),
        }
        missing = [name for name, value in context.items() if not value]
        if missing:
            raise RuntimeError(
                f"Could not resolve NGM headers for cluster {cluster_id}: {', '.join(missing)}"
            )
        return context

    def ngm_slow_query_list(
        self,
        cluster_id: str,
        begin_time: int | str,
        end_time: int | str,
        limit: int = 100,
        order_by: str = "query_time",
        desc: bool = True,
        fields: str = "query,timestamp,query_time,memory_max,digest,index_names",
        text: str = "",
        digest: str = "",
        show_internal: bool = True,
    ) -> dict | list:
        """Slow query list via NGM → GET /ngm/api/v1/slow_query/list.

        Use this for starter/essential/shared clusters whose slow query data is
        not exposed through Data Proxy SQL `slow_query_logs`.
        """
        headers = self._ngm_context_from_cluster_id(cluster_id)
        return self._get(
            "/ngm/api/v1/slow_query/list",
            {
                "begin_time": begin_time,
                "end_time": end_time,
                "fields": fields,
                "limit": int(limit),
                "orderBy": order_by,
                "desc": str(desc).lower(),
                "text": text,
                "digest": digest,
                "show_internal": str(show_internal).lower(),
            },
            extra_headers=headers,
        )

    # ── LogStats convenience helpers ─────────────────────────

    @staticmethod
    def _validate_log_stats_date(date: int | str) -> str:
        value = str(date)
        if len(value) != 8 or not value.isdigit():
            raise ValueError("LogStats date must be YYYYMMDD, for example 20260618")
        return value

    def log_stats_schema(self, cluster_id: str) -> dict:
        """LogStats table schema through canonical table-schemas."""
        return self.get_schema(cluster_id, "log_stats")

    def statements(
        self,
        cluster_id: str,
        date: str,
        limit: int = 100,
        timeout: int = 120,
    ) -> dict:
        """Query persisted SQL Statements through canonical SQL.

        ``date`` must be a single ``YYYY-MM-DD`` partition. The physical
        provider URL and execution engine remain server-resolved.
        """
        parts = date.split("-")
        if (
            len(parts) != 3
            or list(map(len, parts)) != [4, 2, 2]
            or not date.replace("-", "").isdigit()
        ):
            raise ValueError("Statements date must be YYYY-MM-DD, for example 2026-07-22")
        sql = (
            "SELECT * FROM clinic_data_proxy.statements "
            f"WHERE date = '{date}' LIMIT {int(limit)}"
        )
        return self.query_sql(cluster_id, sql, timeout=timeout)

    def log_stats_summary(
        self,
        cluster_id: str,
        date: int | str,
        limit: int = 50,
        timeout: int = 120,
    ) -> dict:
        """Aggregate LogStats by level/component for one YYYYMMDD date.

        LogStats currently uses AWS Delta data. Use an unquoted numeric
        YYYYMMDD date in SQL so Athena can compare the partition field without
        a varchar/bigint mismatch.
        """
        d = self._validate_log_stats_date(date)
        sql = f"""
SELECT
  level,
  component,
  SUM(count) AS total_count
FROM log_stats
WHERE date = {d}
GROUP BY level, component
ORDER BY total_count DESC
LIMIT {int(limit)}
""".strip()
        return self.query_sql(cluster_id, sql, timeout=timeout)

    def log_stats_top(
        self,
        cluster_id: str,
        date: int | str,
        limit: int = 50,
        timeout: int = 120,
    ) -> dict:
        """Top LogStats messages for one YYYYMMDD date."""
        d = self._validate_log_stats_date(date)
        sql = f"""
SELECT
  level,
  component,
  file,
  line,
  msg_digest,
  sample,
  SUM(count) AS total_count
FROM log_stats
WHERE date = {d}
GROUP BY level, component, file, line, msg_digest, sample
ORDER BY total_count DESC
LIMIT {int(limit)}
""".strip()
        return self.query_sql(cluster_id, sql, timeout=timeout)

    def log_stats_search(
        self,
        cluster_id: str,
        date: int | str,
        pattern: str,
        limit: int = 50,
        timeout: int = 120,
    ) -> dict:
        """Search LogStats sample/digest/file/component for a substring."""
        d = self._validate_log_stats_date(date)
        escaped = pattern.replace("\\", "\\\\").replace("'", "''")
        like = f"%{escaped}%"
        sql = f"""
SELECT
  level,
  component,
  file,
  line,
  msg_digest,
  sample,
  SUM(count) AS total_count
FROM log_stats
WHERE date = {d}
  AND (
    sample LIKE '{like}'
    OR msg_digest LIKE '{like}'
    OR file LIKE '{like}'
    OR component LIKE '{like}'
  )
GROUP BY level, component, file, line, msg_digest, sample
ORDER BY total_count DESC
LIMIT {int(limit)}
""".strip()
        return self.query_sql(cluster_id, sql, timeout=timeout)


# ── CLI: python3 clinic_api.py <method> <args...> ─────────

def _cli():
    """Minimal CLI for quick testing."""
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        print("Usage: python3 clinic_api.py <method> <args...>")
        print("  query_range  <cluster_id> <query> <start> <end> [step]")
        print("  query_sql    <cluster_id> <sql> [timeout]")
        print("  query_instant <cluster_id> <query> [time]")
        print("  log_stats_schema  <cluster_id>")
        print("  log_stats_summary <cluster_id> <YYYYMMDD> [limit] [timeout]")
        print("  log_stats_top     <cluster_id> <YYYYMMDD> [limit] [timeout]")
        print("  log_stats_search  <cluster_id> <YYYYMMDD> <pattern> [limit] [timeout]")
        print("  ngm_slow_query_list <cluster_id> <begin_unix_sec> <end_unix_sec> [limit] [orderBy] [desc] [fields] [text] [digest] [show_internal]")
        print("  query_exceeded_iops_by_pod <cluster_id> <tikv|tiflash> <start> <end> [step] [rate_window=2m]")
        print("  query_node_disk_iops_by_pod <cluster_id> <tikv|tiflash> <start> <end> [step] [rate_window=1m]")
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    api = ClinicAPI()
    method = sys.argv[1]

    if method == "query_range":
        result = api.query_range(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5],
                                  sys.argv[6] if len(sys.argv) > 6 else "60")
    elif method == "query_sql":
        result = api.query_sql(sys.argv[2], sys.argv[3],
                                int(sys.argv[4]) if len(sys.argv) > 4 else 60)
    elif method == "query_instant":
        result = api.query_instant(sys.argv[2], sys.argv[3],
                                    sys.argv[4] if len(sys.argv) > 4 else None)
    elif method == "log_stats_schema":
        result = api.log_stats_schema(sys.argv[2])
    elif method == "log_stats_summary":
        result = api.log_stats_summary(
            sys.argv[2],
            sys.argv[3],
            int(sys.argv[4]) if len(sys.argv) > 4 else 50,
            int(sys.argv[5]) if len(sys.argv) > 5 else 120,
        )
    elif method == "log_stats_top":
        result = api.log_stats_top(
            sys.argv[2],
            sys.argv[3],
            int(sys.argv[4]) if len(sys.argv) > 4 else 50,
            int(sys.argv[5]) if len(sys.argv) > 5 else 120,
        )
    elif method == "log_stats_search":
        result = api.log_stats_search(
            sys.argv[2],
            sys.argv[3],
            sys.argv[4],
            int(sys.argv[5]) if len(sys.argv) > 5 else 50,
            int(sys.argv[6]) if len(sys.argv) > 6 else 120,
        )
    elif method == "ngm_slow_query_list":
        result = api.ngm_slow_query_list(
            sys.argv[2],
            sys.argv[3],
            sys.argv[4],
            int(sys.argv[5]) if len(sys.argv) > 5 else 100,
            sys.argv[6] if len(sys.argv) > 6 else "query_time",
            sys.argv[7].lower() == "true" if len(sys.argv) > 7 else True,
            sys.argv[8] if len(sys.argv) > 8 else "query,timestamp,query_time,memory_max,digest,index_names",
            sys.argv[9] if len(sys.argv) > 9 else "",
            sys.argv[10] if len(sys.argv) > 10 else "",
            sys.argv[11].lower() == "true" if len(sys.argv) > 11 else True,
        )
    elif method == "query_exceeded_iops_by_pod":
        result = api.query_exceeded_iops_by_pod(
            sys.argv[2],
            sys.argv[3],
            sys.argv[4],
            sys.argv[5],
            sys.argv[6] if len(sys.argv) > 6 else "60",
            sys.argv[7] if len(sys.argv) > 7 else "2m",
        )
    elif method == "query_node_disk_iops_by_pod":
        result = api.query_node_disk_iops_by_pod(
            sys.argv[2],
            sys.argv[3],
            sys.argv[4],
            sys.argv[5],
            sys.argv[6] if len(sys.argv) > 6 else "60",
            sys.argv[7] if len(sys.argv) > 7 else "1m",
        )
    else:
        print(f"Unknown method: {method}")
        sys.exit(1)

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    _cli()
