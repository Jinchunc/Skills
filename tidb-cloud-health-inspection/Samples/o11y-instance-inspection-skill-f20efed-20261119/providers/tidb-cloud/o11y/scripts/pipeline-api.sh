#!/bin/bash
# Monitoring Pipeline API Manager
# Usage: source pipeline-api.sh
#
# Requires these env vars (set by o11y-auth.sh + resolve-cluster.sh):
#   O11Y_TOKEN   - Bearer token for data-export APIs
#   BIZ_TYPE     - O11Y bizType (e.g. prod-tidbcloud-nextgen)
#   VENDOR       - Cloud vendor (aws, gcp, azure, alicloud)
#   REGION       - Cloud region (us-west-2, asia-east1, etc.)
#   ORG_ID       - Tenant/org ID
#   GS_URL       - GS base URL
#   CLUSTER_ID   - TiDB Cloud cluster ID

# ── Internal helpers ─────────────────────────────────────────────────────────

_pipeline_check_env() {
	local missing=()
	for var in O11Y_TOKEN BIZ_TYPE VENDOR REGION ORG_ID GS_URL CLUSTER_ID; do
		local value
		value="$(printenv "$var")"
		if [ -z "$value" ]; then
			missing+=("$var")
		fi
	done
	if [ ${#missing[@]} -gt 0 ]; then
		echo "ERROR: Missing required env vars: ${missing[*]}" >&2
		echo "Run o11y-auth.sh and resolve-cluster.sh first." >&2
		return 1
	fi
}

_pipeline_base_url() {
	echo "${GS_URL}/${BIZ_TYPE}/v1beta1/internal/clusters/${CLUSTER_ID}/monitoringPipelines"
}

_pipeline_curl() {
	local method="$1"
	local url="$2"
	local body="$3"

	local args=(
		-s -X "$method" "$url"
		-H "Authorization: Bearer ${O11Y_TOKEN}"
		-H "Content-Type: application/json"
		-H "x-region-id: ${VENDOR}-${REGION}"
		-H "x-org-id: ${ORG_ID}"
	)
	if [ -n "$body" ]; then
		args+=(-d "$body")
	fi

	local response
	response=$(curl "${args[@]}")
	local rc=$?
	if [ $rc -ne 0 ]; then
		echo "ERROR: curl failed with exit code $rc" >&2
		return 1
	fi

	local code
	code=$(echo "$response" | jq -r '.code // empty')
	if [ -n "$code" ] && [ "$code" != "0" ]; then
		echo "ERROR: API returned code=$code" >&2
		echo "$response" | jq . >&2
		return 1
	fi

	echo "$response"
}

# ── Public API ───────────────────────────────────────────────────────────────

# Create a monitoring pipeline (data export task).
#
# Usage: pipeline_create <dataType> <startTime> [endTime] [options...]
#
# Arguments:
#   dataType   - slowquery | rawlog | sqlstmt
#   startTime  - RFC3339 timestamp (e.g. 2026-03-01T00:00:00Z)
#   endTime    - RFC3339 timestamp (optional, defaults to now)
#
# Options:
#   --name <displayName>       Custom task name (default: <dataType>-export)
#   --prefix <dataPrefix>      S3 key prefix (default: exports/<CLUSTER_ID>/<dataType>)
#   --components <comp1,comp2> Components for rawlog (default: tidb,tikv,pd)
#
# Examples:
#   pipeline_create slowquery "2026-03-01T00:00:00Z" "2026-03-02T00:00:00Z"
#   pipeline_create rawlog "2026-03-01T00:00:00Z" --components tidb,tikv
#   pipeline_create sqlstmt "2026-03-01T00:00:00Z" "2026-03-02T00:00:00Z" --name my-export
pipeline_create() {
	_pipeline_check_env || return 1

	local data_type="$1"
	local start_time="$2"
	shift 2 || {
		echo "Usage: pipeline_create <dataType> <startTime> [endTime] [--name X] [--prefix X] [--components X]" >&2
		return 1
	}

	if [ -z "$data_type" ] || [ -z "$start_time" ]; then
		echo "Usage: pipeline_create <dataType> <startTime> [endTime] [--name X] [--prefix X] [--components X]" >&2
		return 1
	fi

	case "$data_type" in
	slowquery | rawlog | sqlstmt) ;;
	*)
		echo "ERROR: Invalid dataType '$data_type'. Must be: slowquery, rawlog, sqlstmt" >&2
		return 1
		;;
	esac

	local end_time=""
	local display_name="${data_type}-export"
	local data_prefix="exports/${CLUSTER_ID}/${data_type}"
	local components="tidb,tikv,pd"

	if [ $# -gt 0 ] && [[ "$1" != --* ]]; then
		end_time="$1"
		shift
	fi

	while [ $# -gt 0 ]; do
		case "$1" in
		--name)
			display_name="$2"
			shift 2
			;;
		--prefix)
			data_prefix="$2"
			shift 2
			;;
		--components)
			components="$2"
			shift 2
			;;
		*)
			echo "ERROR: Unknown option '$1'" >&2
			return 1
			;;
		esac
	done

	local body
	body=$(jq -n \
		--arg displayName "$display_name" \
		--arg dataType "$data_type" \
		--arg dataPrefix "$data_prefix" \
		--arg startTime "$start_time" \
		'{
            displayName: $displayName,
            dataType: $dataType,
            dataPrefix: $dataPrefix,
            jobType: "Once",
            startTime: $startTime
        }')

	if [ -n "$end_time" ]; then
		body=$(echo "$body" | jq --arg endTime "$end_time" '. + {endTime: $endTime}')
	fi

	if [ "$data_type" = "rawlog" ]; then
		local comp_json
		comp_json=$(echo "$components" | tr ',' '\n' | jq -R . | jq -s .)
		body=$(echo "$body" | jq --argjson rawLogComponents "$comp_json" '. + {rawLogComponents: $rawLogComponents}')
	fi

	echo "Creating ${data_type} export for cluster ${CLUSTER_ID}..." >&2
	local response
	response=$(_pipeline_curl POST "$(_pipeline_base_url)" "$body") || return 1

	local task_id
	task_id=$(echo "$response" | jq -r '.data.id // empty')
	local bucket
	bucket=$(echo "$response" | jq -r '.data.destinationInfo.accessPoint // empty')

	echo "Task created: ${task_id}" >&2
	[ -n "$bucket" ] && echo "S3 destination: ${bucket}" >&2
	echo "$response" | jq .
}

# Get a monitoring pipeline task by ID.
#
# Usage: pipeline_get <taskId>
pipeline_get() {
	_pipeline_check_env || return 1

	local task_id="$1"
	if [ -z "$task_id" ]; then
		echo "Usage: pipeline_get <taskId>" >&2
		return 1
	fi

	_pipeline_curl GET "$(_pipeline_base_url)/${task_id}" | jq .
}

# List monitoring pipeline tasks for the current cluster.
#
# Usage: pipeline_list [dataType]
#
# Arguments:
#   dataType - Optional filter: slowquery | rawlog | sqlstmt
pipeline_list() {
	_pipeline_check_env || return 1

	local url
	url="$(_pipeline_base_url)"
	if [ -n "$1" ]; then
		url="${url}?dataType=$1"
	fi

	_pipeline_curl GET "$url" | jq .
}

# Delete a monitoring pipeline task.
#
# Usage: pipeline_delete <taskId>
pipeline_delete() {
	_pipeline_check_env || return 1

	local task_id="$1"
	if [ -z "$task_id" ]; then
		echo "Usage: pipeline_delete <taskId>" >&2
		return 1
	fi

	echo "Deleting task ${task_id}..." >&2
	_pipeline_curl DELETE "$(_pipeline_base_url)/${task_id}" | jq .
	echo "Task ${task_id} deleted." >&2
}

# Wait for a monitoring pipeline task to become ACTIVE.
#
# Usage: pipeline_wait <taskId> [timeout_secs]
#
# Arguments:
#   taskId       - Task ID from pipeline_create
#   timeout_secs - Max wait time in seconds (default: 300)
#
# Returns 0 when ACTIVE, 1 on timeout or error.
pipeline_wait() {
	_pipeline_check_env || return 1

	local task_id="$1"
	local timeout="${2:-300}"
	if [ -z "$task_id" ]; then
		echo "Usage: pipeline_wait <taskId> [timeout_secs]" >&2
		return 1
	fi

	local elapsed=0
	local interval=10
	echo "Waiting for task ${task_id} to become ACTIVE (timeout: ${timeout}s)..." >&2

	while [ "$elapsed" -lt "$timeout" ]; do
		local response
		response=$(_pipeline_curl GET "$(_pipeline_base_url)/${task_id}") || return 1

		local phase
		phase=$(echo "$response" | jq -r '.data.phase // empty')
		echo "  phase=${phase} (${elapsed}s elapsed)" >&2

		if [ "$phase" = "ACTIVE" ]; then
			echo "Task ${task_id} is ACTIVE." >&2
			echo "$response" | jq .
			return 0
		fi

		sleep "$interval"
		elapsed=$((elapsed + interval))
	done

	echo "ERROR: Timed out after ${timeout}s waiting for task ${task_id} (last phase: ${phase})" >&2
	return 1
}

export -f pipeline_create
export -f pipeline_get
export -f pipeline_list
export -f pipeline_delete
export -f pipeline_wait
export -f _pipeline_check_env
export -f _pipeline_base_url
export -f _pipeline_curl
