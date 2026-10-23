#!/usr/bin/env bash
# Source inside the selected image, after its CANN set_env.sh.
export PYTHONUNBUFFERED=1
export PYTHONFAULTHANDLER=1
export LMCACHE_LOG_LEVEL=DEBUG
export VLLM_CONFIGURE_LOGGING=1
export VLLM_LOGGING_LEVEL=DEBUG
export VLLM_LOGGING_COLOR=0
export ASCEND_GLOBAL_LOG_LEVEL=0
export ASCEND_SLOG_PRINT_TO_STDOUT=0
export HCCL_ENTRY_LOG_ENABLE=1
export MC_LOG_LEVEL=TRACE
# Force only the supported MP transport choice; do not enable remote tiers.
export LMCACHE_MP_TRANSFER_MODE=lmcache_driven
