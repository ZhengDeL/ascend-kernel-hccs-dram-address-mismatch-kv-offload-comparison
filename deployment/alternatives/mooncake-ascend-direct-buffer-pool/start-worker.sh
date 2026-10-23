#!/usr/bin/env bash
# Run manually inside the separate candidate container: bash start-worker.sh P 0
set -Eeuo pipefail
: "${1:?role P or D}" "${2:?DP rank}" "${RUN_DIR:?host-persistent unique session directory}"
: "${LOCAL_IP:?actual local server IP}" "${NIC_NAME:?actual local interface}" "${MOONCAKE_MASTER:?actual Master IP:50088}"
case "$1:$2" in P:[01]|D:[0-3]) ;; *) echo 'Require P rank 0-1 or D rank 0-3' >&2; exit 2;; esac
case "$RUN_DIR" in /*) ;; *) echo 'RUN_DIR must be absolute' >&2; exit 2;; esac
case "$MOONCAKE_MASTER" in 192.0.2.*|198.51.100.*|203.0.113.*) echo 'Replace the documentation Master address in local environment first' >&2; exit 2;; esac
bundle="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$bundle/capture.py" --log-dir "$RUN_DIR/$1/dp$2" -- /bin/bash "$bundle/worker-entry.sh" "$1" "$2"
