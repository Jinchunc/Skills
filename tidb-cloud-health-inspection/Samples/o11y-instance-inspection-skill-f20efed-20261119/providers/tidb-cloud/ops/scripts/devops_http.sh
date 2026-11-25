#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${ENV_FILE:-.env}"

METHOD="GET"
PATH_ONLY=""
QUERY=""
BODY=""
BODY_FILE=""
TIMEOUT_SECONDS="${DEVOPS_TIMEOUT_SECONDS:-30}"
HEADERS=()
DEVOPS_ENV=""

usage() {
  cat <<'EOF'
Usage:
  devops_http.sh --path /api/path [--env prod|staging|dev] \
    [--method METHOD] \
    [--query "k=v&x=y"] \
    [--body '{"a":"b"}' | --body-file /path/to/body.json] \
    [--header "Key: Value"] \
    [--timeout 30] \
    [--env-file /path/to/.env]

Required:
  --path             HTTP path only, for example: /cloudconfig/cluster/config

Optional:
  --env              Target environment: prod (default), staging, dev
                     Sets the base domain automatically:
                       prod:    https://prod.devops.pingcap.com
                       staging: https://staging.devops.pingcap.com
                       dev:     https://dev.devops.pingcap.com
  --method           HTTP method, default: GET
  --query            Raw query string, for example: cluster_id=xxx&cluster_type=DEDICATED_TIDB
  --body             Raw request body, usually JSON
  --body-file        Read request body from file
  --header           Extra HTTP header, can be passed multiple times
  --timeout          Curl timeout in seconds, default: 30
  --env-file         Path to .env file, default: .env

Environment variables:
  DEVOPS_API_TOKEN       API token used for authentication.
                          Can be set in shell environment directly.
                          If not set, script will try to load it from .env.
  DEVOPS_BASE_DOMAIN     Override base domain. If set, takes precedence over --env.

Examples:
  # Query cluster config in prod (default)
  ./devops_http.sh --path /cloudconfig/cluster/config \
    --query "cluster_id=xxx&cluster_type=DEDICATED_TIDB"

  # Create a config change in staging
  ./devops_http.sh --env staging --method POST --path /cloudconfig/cluster/changes \
    --body '{"cluster_type":"DEDICATED_TIDB","cluster_id":"xxx"}'

  # Apply a change in dev
  ./devops_http.sh --env dev --method POST --path /cloudconfig/changes/123/apply \
    --body '{}'
EOF
}

log_err() {
  printf '[devops_http] %s\n' "$*" >&2
}

cleanup() {
  [[ -n "${TMP_BODY:-}" && -f "${TMP_BODY}" ]] && rm -f "${TMP_BODY}"
  [[ -n "${TMP_CURL_ERR:-}" && -f "${TMP_CURL_ERR}" ]] && rm -f "${TMP_CURL_ERR}"
}
trap cleanup EXIT

load_env_file() {
  local env_path="$1"

  if [[ ! -f "$env_path" ]]; then
    return 0
  fi

  set -a
  source "$env_path"
  set +a
}

normalize_method() {
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]'
}

normalize_path() {
  local p="$1"
  [[ -n "$p" ]] || {
    log_err "missing required argument: --path"
    usage
    exit 1
  }

  if [[ "$p" != /* ]]; then
    p="/$p"
  fi

  printf '%s' "$p"
}

build_url() {
  local base="$1"
  local path_only="$2"
  local query="$3"

  base="${base%/}"
  local url="${base}${path_only}"

  if [[ -n "$query" ]]; then
    query="${query#\?}"
    url="${url}?${query}"
  fi

  printf '%s' "$url"
}

has_content_type_header() {
  local h
  for h in "${HEADERS[@]}"; do
    if [[ "$h" =~ ^[Cc][Oo][Nn][Tt][Ee][Nn][Tt]-[Tt][Yy][Pp][Ee][[:space:]]*: ]]; then
      return 0
    fi
  done
  return 1
}

validate_inputs() {
  if [[ -n "$BODY" && -n "$BODY_FILE" ]]; then
    log_err "--body and --body-file cannot be used together"
    exit 1
  fi

  if [[ -n "$BODY_FILE" && ! -f "$BODY_FILE" ]]; then
    log_err "body file not found: ${BODY_FILE}"
    exit 1
  fi

  local h
  for h in "${HEADERS[@]}"; do
    if [[ "$h" != *:* ]]; then
      log_err "invalid --header value: ${h}"
      log_err 'header must be in the form "Key: Value"'
      exit 1
    fi
  done
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --method)
        METHOD="${2:-}"
        shift 2
        ;;
      --path)
        PATH_ONLY="${2:-}"
        shift 2
        ;;
      --query)
        QUERY="${2:-}"
        shift 2
        ;;
      --body)
        BODY="${2:-}"
        shift 2
        ;;
      --body-file)
        BODY_FILE="${2:-}"
        shift 2
        ;;
      --header)
        HEADERS+=("${2:-}")
        shift 2
        ;;
      --timeout)
        TIMEOUT_SECONDS="${2:-}"
        shift 2
        ;;
      --env)
        DEVOPS_ENV="${2:-}"
        shift 2
        ;;
      --env-file)
        ENV_FILE="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        log_err "unknown argument: $1"
        usage
        exit 1
        ;;
    esac
  done
}

main() {
  parse_args "$@"

  METHOD="$(normalize_method "$METHOD")"
  PATH_ONLY="$(normalize_path "/api/v1/devops$PATH_ONLY")"

  validate_inputs

  # Load env file when present so non-token variables (for example DEVOPS_BASE_DOMAIN)
  # are available even if token is already provided via process environment.
  load_env_file "$ENV_FILE"

  local env_domain=""
  case "${DEVOPS_ENV}" in
    prod)    env_domain="https://prod.devops.pingcap.com" ;;
    staging) env_domain="https://staging.devops.pingcap.com" ;;
    dev)     env_domain="https://dev.devops.pingcap.com" ;;
    "")      ;;
    *)
      log_err "unknown --env value: ${DEVOPS_ENV} (valid: prod, staging, dev)"
      exit 1
      ;;
  esac
  local base_domain="${DEVOPS_BASE_DOMAIN:-${env_domain:-https://prod.devops.pingcap.com}}"
  local api_token="${DEVOPS_API_TOKEN:-}"
  if [[ -z "$api_token" ]]; then
    log_err "DEVOPS_API_TOKEN is required but not set (environment variable or env file: ${ENV_FILE})"
    exit 1
  fi

  local url
  url="$(build_url "$base_domain" "$PATH_ONLY" "$QUERY")"

  TMP_BODY="$(mktemp)"
  TMP_CURL_ERR="$(mktemp)"

  local -a curl_args=(
    -sS
    -X "$METHOD"
    -o "$TMP_BODY"
    -w "%{http_code}"
    --connect-timeout 10
    --max-time "$TIMEOUT_SECONDS"
    -H "Accept: application/json"
    -H "X-API-TOKEN: ${api_token}"
    "$url"
  )

  local h
  for h in "${HEADERS[@]}"; do
    curl_args+=(-H "$h")
  done

  if [[ -n "$BODY" || -n "$BODY_FILE" ]]; then
    if ! has_content_type_header; then
      curl_args+=(-H "Content-Type: application/json")
    fi
  fi

  if [[ -n "$BODY" ]]; then
    curl_args+=(--data "$BODY")
  elif [[ -n "$BODY_FILE" ]]; then
    curl_args+=(--data-binary "@${BODY_FILE}")
  fi

  local http_code=""
  local curl_exit_code=0

  if ! http_code="$(curl "${curl_args[@]}" 2>"$TMP_CURL_ERR")"; then
    curl_exit_code=$?
    log_err "curl execution failed"
    log_err "method: ${METHOD}"
    log_err "url: ${url}"
    log_err "curl exit code: ${curl_exit_code}"

    if [[ -s "$TMP_CURL_ERR" ]]; then
      log_err "curl stderr:"
      cat "$TMP_CURL_ERR" >&2
    fi

    if [[ -s "$TMP_BODY" ]]; then
      log_err "response body:"
      cat "$TMP_BODY" >&2
    fi

    exit "$curl_exit_code"
  fi

  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    cat "$TMP_BODY"
    exit 0
  fi

  log_err "request failed"
  log_err "method: ${METHOD}"
  log_err "url: ${url}"
  log_err "http status: ${http_code}"

  if [[ -s "$TMP_BODY" ]]; then
    log_err "response body:"
    cat "$TMP_BODY" >&2
  else
    log_err "response body: <empty>"
  fi

  exit 1
}

main "$@"