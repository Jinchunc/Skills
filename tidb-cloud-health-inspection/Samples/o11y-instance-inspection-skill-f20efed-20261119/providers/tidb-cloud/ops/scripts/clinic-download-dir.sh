# Shared default directory for NGM downloads (diagnostic files, continuous profiling, etc.).
# Requires nothing; sourced by scripts/clinic-shell.sh before sub-skills that write files.
#
# 1) CLINIC_DIAGNOSTIC_DOWNLOAD_DIR — if set when the download runs
# 2) Else $PWD at download time

_clinic_default_download_dir() {
    if [ -n "${CLINIC_DIAGNOSTIC_DOWNLOAD_DIR:-}" ]; then
        printf '%s\n' "${CLINIC_DIAGNOSTIC_DOWNLOAD_DIR}"
        return 0
    fi
    printf '%s\n' "${PWD}"
}
