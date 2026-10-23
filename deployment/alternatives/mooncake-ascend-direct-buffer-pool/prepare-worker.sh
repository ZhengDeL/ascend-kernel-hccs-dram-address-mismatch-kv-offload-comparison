#!/usr/bin/env bash
# Source: source ./prepare-worker.sh P 0 /var/ai-model/lzd/<unique-session>
: "${1:?role P or D}" "${2:?DP rank}" "${3:?absolute host-persistent run directory}"
: "${MOONCAKE_MASTER:?actual Master host:port; rendered config stays private}"
export CANDIDATE_ROLE="$1" CANDIDATE_DP_RANK="$2"
case "$CANDIDATE_ROLE:$CANDIDATE_DP_RANK" in P:[01]|D:[0-3]) ;; *) echo 'Require P rank 0-1 or D rank 0-3' >&2; return 2;; esac
case "$3" in /*) ;; *) echo 'Run directory must be absolute' >&2; return 2;; esac
source "$(dirname -- "${BASH_SOURCE[0]}")/common-env.sh"
export CANDIDATE_DP_DIR="$3/$CANDIDATE_ROLE/dp$CANDIDATE_DP_RANK"
mkdir -p -- "$CANDIDATE_DP_DIR"/{config,plog,mooncake,vllm_ascend}
chmod 700 -- "$CANDIDATE_DP_DIR"
export ASCEND_PROCESS_LOG_PATH="$CANDIDATE_DP_DIR/plog"
export MC_LOG_DIR="$CANDIDATE_DP_DIR/mooncake"
export MOONCAKE_CONFIG_PATH="$CANDIDATE_DP_DIR/config/mooncake.json"
export CANDIDATE_CONNECTOR_PATH="$CANDIDATE_DP_DIR/config/connector.json"
python3 "$CANDIDATE_BUNDLE_DIR/render-config.py" --role "$CANDIDATE_ROLE" --rank "$CANDIDATE_DP_RANK" --output "$CANDIDATE_DP_DIR/config" || return $?
python3 - "$CANDIDATE_BUNDLE_DIR" "$CANDIDATE_DP_DIR" "$CANDIDATE_ROLE" "$CANDIDATE_DP_RANK" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
from logging_utils import write_metadata
write_metadata(sys.argv[2], sys.argv[3] + '-DP' + sys.argv[4], extra={
    'candidate': 'Mooncake Ascend Direct relay buffer; not a MemCache fix',
    'ASCEND_BUFFER_POOL': os.environ.get('ASCEND_BUFFER_POOL'), 'fabric_mem': 'unset', 'async_transfer': 'unset',
    'cpu_pool_per_worker_bytes': 20 * 1024**3 if sys.argv[3] == 'P' else 0,
    'store_local_buffer_size_bytes': 0,
    'native_retention': 'Library-controlled; collect promptly and check free space',
})
PY
