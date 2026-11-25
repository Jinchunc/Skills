# Clinic API authentication and shared endpoints.
# Sourced by scripts/clinic-shell.sh (or standalone after setting CLINIC_SKILL_ROOT).
#
# Multi-environment support:
#   Set CLINIC_ENV to switch environments (default: prod).
#   Valid values: dev, staging, prod
#   Example: CLINIC_ENV=dev source scripts/clinic-shell.sh && get_clinic_auth

ENV_FILE="${ENV_FILE:-.env}"
CLINIC_ENV="${CLINIC_ENV:-prod}"

_clinic_resolve_endpoint() {
    case "${CLINIC_ENV}" in
        dev)     echo "https://dev-clinic.pingcap.com" ;;
        staging) echo "https://staging-clinic.pingcap.com" ;;
        prod|*)  echo "https://clinic.pingcap.com" ;;
    esac
}

CLINIC_ENDPOINT="$(_clinic_resolve_endpoint)"
CLINIC_API_BASE="${CLINIC_ENDPOINT}/clinic/api/v1"

get_clinic_auth() {
    if [ -f "$ENV_FILE" ]; then
        source "$ENV_FILE"
    fi

    # Re-resolve endpoint in case CLINIC_ENV changed after sourcing
    CLINIC_ENDPOINT="$(_clinic_resolve_endpoint)"
    CLINIC_API_BASE="${CLINIC_ENDPOINT}/clinic/api/v1"

    if [ -z "${CLINIC_API_KEY:-}" ]; then
        echo "ERROR: CLINIC_API_KEY not set" >&2
        echo "Please set CLINIC_API_KEY in $ENV_FILE or environment" >&2
        echo "Create API Key at clinic.pingcap.com User Profile (requires internal user)" >&2
        return 1
    fi

    export CLINIC_API_KEY CLINIC_ENDPOINT CLINIC_API_BASE CLINIC_ENV
    echo "Clinic API Key configured (env=${CLINIC_ENV}, base=${CLINIC_ENDPOINT})" >&2
    return 0
}

check_clinic_auth() {
    if [ -f "$ENV_FILE" ]; then
        source "$ENV_FILE"
    fi

    if [ -z "${CLINIC_API_KEY:-}" ]; then
        return 1
    fi

    return 0
}

_ensure_clinic_auth() {
    if [ -n "${CLINIC_API_KEY:-}" ]; then
        return 0
    fi
    get_clinic_auth
}
