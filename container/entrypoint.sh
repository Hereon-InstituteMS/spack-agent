#!/usr/bin/env bash
set -euo pipefail

if [[ ! -x /opt/spack/bin/spack ]]; then
    echo "spack-agent image contract error: /opt/spack/bin/spack is missing" >&2
    exit 1
fi

if [[ -f /opt/intel/oneapi/setvars.sh ]]; then
    # oneAPI's environment scripts reference optional unset variables.
    set +u
    source /opt/intel/oneapi/setvars.sh --force >/dev/null
    set -u
fi

if [[ -n "${SPACK_AGENT_REPOSITORY:-}" ]]; then
    if [[ ! -d "$SPACK_AGENT_REPOSITORY" ]]; then
        echo "spack-agent mount error: repository not found: $SPACK_AGENT_REPOSITORY" >&2
        exit 1
    fi
    spack repo add --scope=user "$SPACK_AGENT_REPOSITORY" >/dev/null
fi

exec "$@"