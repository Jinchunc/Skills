#!/bin/bash
# O11Y Unified Auth Manager
# Usage: source o11y-auth.sh && get_o11y_auth
#
# Authenticates via GitHub PAT -> DS /client/auth endpoint.
# Returns three credential sets:
#   - O11Y_TOKEN:      bearer token for data-export APIs (monitoringPipelines)
#   - O11Y_AUTH0_TOKEN: token for O11Y control plane (GS cluster lookup, DS metrics)
#   - AWS STS creds:    for S3 bucket access (exported data)
#
# Caches all credentials in .env with expiration tracking.
#
# Required .env variables:
#   GITHUB_PAT=ghp_...
#
# Cached variables (managed by this script):
#   O11Y_TOKEN=ey...
#   O11Y_AUTH0_TOKEN=ey...
#   O11Y_TOKEN_EXPIRES=1770427199
#   O11Y_AWS_ACCESS_KEY_ID=ASIA...
#   O11Y_AWS_SECRET_ACCESS_KEY=...
#   O11Y_AWS_SESSION_TOKEN=...

ENV_FILE="${ENV_FILE:-.env}"
DS_AUTH_URL="https://www.ds.us-west-2.aws.observability.tidbcloud.com/client/auth"

o11y_auth_request() {
	local response=""
	local body=""
	local http_code=""
	local attempt=""
	local delay=1

	for attempt in 1 2 3; do
		response=$(curl -s -w '\n%{http_code}' -X POST "$DS_AUTH_URL" \
			-H 'Content-Type: application/json' \
			-d "{\"github_token\": \"${GITHUB_PAT}\"}")
		http_code="${response##*$'\n'}"
		body="${response%$'\n'*}"

		if [ "$http_code" = "200" ]; then
			printf '%s\n' "$body"
			return 0
		fi

		case "$http_code" in
		000|403|429|5??)
			if [ "$attempt" -lt 3 ]; then
				echo "WARN: /client/auth attempt ${attempt} failed with HTTP ${http_code}; retrying..." >&2
				sleep "$delay"
				delay=$((delay * 2))
				continue
			fi
			;;
		esac

		echo "ERROR: /client/auth failed with HTTP ${http_code}" >&2
		printf '%s\n' "$body" >&2
		return 1
	done

	echo "ERROR: /client/auth failed after 3 attempts" >&2
	printf '%s\n' "$body" >&2
	return 1
}

get_o11y_auth() {
	# Load .env
	if [ -f "$ENV_FILE" ]; then
		source "$ENV_FILE"
	fi

	if [ -z "$GITHUB_PAT" ]; then
		echo "GITHUB_PAT is required but not set." >&2
		echo -n "Enter your GitHub Personal Access Token: " >&2
		read -r GITHUB_PAT
		if [ -z "$GITHUB_PAT" ]; then
			echo "ERROR: No token provided" >&2
			return 1
		fi
		# Save to .env for future use
		if [ ! -f "$ENV_FILE" ]; then
			touch "$ENV_FILE"
		fi
		echo "GITHUB_PAT=${GITHUB_PAT}" >>"$ENV_FILE"
		echo "GITHUB_PAT saved to $ENV_FILE" >&2
	fi

	# Check if we have a valid cached token
	local now=$(date +%s)
	local token_expires="${O11Y_TOKEN_EXPIRES:-$O11Y_AUTH0_TOKEN_EXPIRES}"

	if [ -n "$O11Y_TOKEN" ] && [ -n "$token_expires" ]; then
		local expires_with_buffer=$((token_expires - 300))
		if [ "$now" -lt "$expires_with_buffer" ]; then
			echo "Using cached O11Y token (expires in $((token_expires - now))s)" >&2
			export O11Y_TOKEN
			export O11Y_AUTH0_TOKEN
			export AWS_ACCESS_KEY_ID="$O11Y_AWS_ACCESS_KEY_ID"
			export AWS_SECRET_ACCESS_KEY="$O11Y_AWS_SECRET_ACCESS_KEY"
			export AWS_SESSION_TOKEN="$O11Y_AWS_SESSION_TOKEN"
			echo "$O11Y_TOKEN"
			return 0
		fi
		echo "Token expired or expiring soon, fetching new one..." >&2
	else
		echo "No cached token found, fetching new one..." >&2
	fi

	# Fetch new tokens + AWS credentials
	local response
	response=$(o11y_auth_request) || return 1

	local token=$(echo "$response" | jq -r '.token')
	local auth0_token=$(echo "$response" | jq -r '.auth0_token')
	local ak=$(echo "$response" | jq -r '.access_key_id')
	local sk=$(echo "$response" | jq -r '.secret_access_key')
	local st=$(echo "$response" | jq -r '.session_token')

	if [ -z "$token" ] || [ "$token" = "null" ] || [ -z "$auth0_token" ] || [ "$auth0_token" = "null" ]; then
		echo "ERROR: Failed to get O11Y token" >&2
		echo "$response" >&2
		return 1
	fi

	# Auth response doesn't include expires_in; assume 12h (conservative for STS)
	local expires_in=43200
	local expires_at=$((now + expires_in))

	# Update .env file -- remove old cached lines
	grep -v "^O11Y_TOKEN=" "$ENV_FILE" |
		grep -v "^O11Y_AUTH0_TOKEN=" |
		grep -v "^O11Y_AUTH0_TOKEN_EXPIRES=" |
		grep -v "^O11Y_TOKEN_EXPIRES=" |
		grep -v "^O11Y_AWS_ACCESS_KEY_ID=" |
		grep -v "^O11Y_AWS_SECRET_ACCESS_KEY=" |
		grep -v "^O11Y_AWS_SESSION_TOKEN=" \
			>"${ENV_FILE}.tmp"

	# Add new cached lines
	cat >>"${ENV_FILE}.tmp" <<EOF
O11Y_TOKEN=${token}
O11Y_AUTH0_TOKEN=${auth0_token}
O11Y_AUTH0_TOKEN_EXPIRES=${expires_at}
O11Y_TOKEN_EXPIRES=${expires_at}
O11Y_AWS_ACCESS_KEY_ID=${ak}
O11Y_AWS_SECRET_ACCESS_KEY=${sk}
O11Y_AWS_SESSION_TOKEN=${st}
EOF

	mv -f "${ENV_FILE}.tmp" "$ENV_FILE"

	# Export for immediate use
	export O11Y_TOKEN="$token"
	export O11Y_AUTH0_TOKEN="$auth0_token"
	export AWS_ACCESS_KEY_ID="$ak"
	export AWS_SECRET_ACCESS_KEY="$sk"
	export AWS_SESSION_TOKEN="$st"

	echo "New token fetched, expires in ${expires_in}s (at $(date -r "$expires_at" 2>/dev/null || date -d "@$expires_at" 2>/dev/null))" >&2
	echo "$token"
}

# Maps clinic deployType -> O11Y base bizType
# Usage: resolve_biz_type "premium"        -> "tidbcloud-nextgen"
#        resolve_biz_type "premium" "prod"  -> "prod-tidbcloud-nextgen"
resolve_biz_type() {
	local deploy_type="$1"
	local env="${2:-prod}" # default to prod if not provided
	local base=""

	case "$deploy_type" in
	dedicated)
		base="tidbcloud"
		;;
	premium | resource_pool)
		base="tidbcloud-nextgen"
		;;
	starter | essential)
		base="devtier"
		;;
	*)
		echo "ERROR: Unknown deployType '$deploy_type'" >&2
		return 1
		;;
	esac

	if [ -z "$env" ]; then
		echo "$base"
	elif [ "$env" = "prod" ] && [ "$base" = "tidbcloud" ]; then
		# Prod dedicated has no prefix (anomaly)
		echo "tidbcloud"
	else
		echo "${env}-${base}"
	fi
}

# Export functions for use in subshells when sourced by bash.
if [ -n "${BASH_VERSION:-}" ]; then
	export -f get_o11y_auth
	export -f resolve_biz_type
fi
