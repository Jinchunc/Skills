#!/bin/bash
# Clinic API shell helpers — aggregator entrypoint for canonical Data Proxy and legacy NGM workflows.
# Usage: source scripts/clinic-shell.sh && get_clinic_auth
#
# Loads sub-skill scripts under this skill directory:
#   scripts/clinic-auth.sh
#   scripts/clinic-download-dir.sh  (_clinic_default_download_dir for NGM downloads)
#   scripts/clinic-serverless-parent.sh  (clinic_parent_* pool-level metrics/logs)
#   cluster-metadata/scripts/clinic-cluster.sh
#   activity-hub/scripts/clinic-activity.sh
#   data-proxy/scripts/clinic-data-proxy.sh
#   metrics/scripts/clinic-metrics.sh
#   loki/scripts/clinic-loki.sh
#   continuous-profiling/scripts/clinic-continuous-profiling.sh
#   diagnostic-files/scripts/clinic-diagnostic-files.sh

if [ -n "${BASH_SOURCE[0]:-}" ]; then
    _CLINIC_SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
    _CLINIC_SKILL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi

source "${_CLINIC_SKILL_ROOT}/scripts/clinic-auth.sh"
source "${_CLINIC_SKILL_ROOT}/scripts/clinic-download-dir.sh"
source "${_CLINIC_SKILL_ROOT}/scripts/clinic-serverless-parent.sh"
source "${_CLINIC_SKILL_ROOT}/cluster-metadata/scripts/clinic-cluster.sh"
source "${_CLINIC_SKILL_ROOT}/activity-hub/scripts/clinic-activity.sh"
source "${_CLINIC_SKILL_ROOT}/data-proxy/scripts/clinic-data-proxy.sh"
source "${_CLINIC_SKILL_ROOT}/metrics/scripts/clinic-metrics.sh"
source "${_CLINIC_SKILL_ROOT}/loki/scripts/clinic-loki.sh"
source "${_CLINIC_SKILL_ROOT}/continuous-profiling/scripts/clinic-continuous-profiling.sh"
source "${_CLINIC_SKILL_ROOT}/diagnostic-files/scripts/clinic-diagnostic-files.sh"

_clinic_export_functions() {
    # `export -f` is Bash-specific and breaks when this file is sourced in zsh.
    # Only export functions when running under Bash.
    if [ -z "${BASH_VERSION:-}" ]; then
        return 0
    fi

    export -f get_clinic_auth
    export -f check_clinic_auth
    export -f _ensure_clinic_auth
    export -f _clinic_path_segment
    export -f _clinic_canonical_org_id_from_cluster_id
    export -f _clinic_parent_cpi
    export -f clinic_parent_metrics_query
    export -f clinic_parent_metrics_query_range
    export -f clinic_parent_metrics_series
    export -f clinic_parent_logs_query_range
    export -f clinic_parent_logs_labels
    export -f clinic_parent_logs_label_values
    export -f clinic_parent_logs_series
    export -f clinic_list_clusters
    export -f clinic_search_clusters
    export -f clinic_search_cluster_by_id
    export -f clinic_get_cluster
    export -f clinic_get_cluster_by_id
    export -f clinic_get_components
    export -f clinic_get_resource_pool_components
    export -f clinic_get_topology
    export -f clinic_legacy_search_clusters
    export -f clinic_legacy_search_cluster_by_id
    export -f clinic_legacy_get_cluster
    export -f clinic_legacy_get_cluster_by_id
    export -f clinic_legacy_get_resource_pool_components
    export -f clinic_legacy_get_topology
    export -f clinic_get_activities
    export -f clinic_legacy_get_activities
    export -f clinic_dataproxy_query
    export -f clinic_dataproxy_batch_query
    export -f clinic_dataproxy_schema
    export -f clinic_dataproxy_capabilities
    export -f clinic_statements_query
    export -f clinic_top_ru_summary
    export -f clinic_top_ru_user_stats
    export -f clinic_legacy_dataproxy_query
    export -f clinic_legacy_dataproxy_batch_query
    export -f clinic_legacy_dataproxy_schema
    export -f clinic_legacy_dataproxy_info
    export -f _clinic_ngm_normalize_deploy_type
    export -f _clinic_slow_query_ngm_context_from_cluster_id
    export -f clinic_ngm_slow_query_list
    export -f _clinic_log_stats_validate_date
    export -f clinic_log_stats_schema
    export -f clinic_log_stats_summary
    export -f clinic_log_stats_top
    export -f clinic_log_stats_search
    export -f clinic_metrics_query
    export -f clinic_metrics_query_range
    export -f clinic_metrics_series
    export -f clinic_legacy_metrics_query
    export -f clinic_legacy_metrics_query_range
    export -f clinic_legacy_metrics_series
    export -f clinic_loki_query_range
    export -f clinic_loki_query
    export -f clinic_loki_labels
    export -f clinic_loki_label_values
    export -f clinic_loki_series
    export -f clinic_legacy_loki_query
    export -f clinic_legacy_loki_query_range
    export -f clinic_legacy_loki_labels
    export -f clinic_legacy_loki_label_values
    export -f clinic_legacy_loki_series
    export -f clinic_system_logs_default_datasource
    export -f clinic_system_logs_datasources
    export -f clinic_system_logs_datasources_table
    export -f clinic_system_logs_datasource_for_region
    export -f clinic_system_logs_query_range
    export -f clinic_system_logs_labels
    export -f clinic_system_logs_label_values
    export -f clinic_system_logs_series
    export -f clinic_legacy_system_logs_datasources
    export -f clinic_legacy_system_logs_query_range
    export -f clinic_legacy_system_logs_labels
    export -f clinic_legacy_system_logs_label_values
    export -f clinic_legacy_system_logs_series
    export -f clinic_continuous_profiling_group_profiles
    export -f clinic_continuous_profiling_detail
    export -f clinic_continuous_profiling_action_token
    export -f clinic_continuous_profiling_download
    export -f clinic_continuous_profiling_fetch
    export -f clinic_diagnostic_list_plan_replayer
    export -f clinic_diagnostic_list_oom_record
    export -f clinic_diagnostic_download_file
}

_clinic_export_functions
