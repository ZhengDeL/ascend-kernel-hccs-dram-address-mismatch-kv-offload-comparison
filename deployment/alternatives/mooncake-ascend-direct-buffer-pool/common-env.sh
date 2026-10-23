#!/usr/bin/env bash
# Source after CANN set_env.sh. This candidate never initializes MemCache.
export CANDIDATE_BUNDLE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# In the audited Mooncake, existence enables these two switches; '=0' is unsafe.
unset ASCEND_ENABLE_USE_FABRIC_MEM ASCEND_USE_ASYNC_TRANSFER
unset MMC_META_CONFIG_PATH MMC_LOCAL_CONFIG_PATH MF_LOG_LEVEL ASCEND_MF_LOG_LEVEL
unset HCCL_INTRA_ROCE_ENABLE MC_USE_TENT ASCEND_USE_SHMEM
unset MOONCAKE_GLOBAL_SEGMENT_SIZE
export ASCEND_BUFFER_POOL=4:8
export ASCEND_GLOBAL_RESOURCE_CONFIG='{"comm_resource_config.protocol_desc":["hccs:device"]}'
export ASCEND_LOCAL_COMM_RES='{"version":"1.2"}'
export PYTHONHASHSEED=0
export PYTHONUNBUFFERED=1
export PYTHONFAULTHANDLER=1
export ACL_OP_INIT_MODE=1
export VLLM_USE_V2_MODEL_RUNNER=0
export VLLM_CONFIGURE_LOGGING=1
export VLLM_LOGGING_LEVEL=DEBUG
export VLLM_LOGGING_COLOR=0
export VLLM_LOGGING_STREAM=ext://sys.stdout
export VLLM_LOGGING_CONFIG_PATH="$CANDIDATE_BUNDLE_DIR/config/vllm-logging.json"
export VLLM_LOG_STATS_INTERVAL=10
export ASCEND_GLOBAL_LOG_LEVEL=0
export ASCEND_SLOG_PRINT_TO_STDOUT=0
export HCCL_ENTRY_LOG_ENABLE=1
export MC_LOG_LEVEL=TRACE
