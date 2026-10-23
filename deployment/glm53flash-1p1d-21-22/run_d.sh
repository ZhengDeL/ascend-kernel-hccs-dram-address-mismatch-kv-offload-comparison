#!/usr/bin/env bash
# Serving flags follow the user's latest A3 recipe, frozen at 4e6eac4e.
set -Eeuo pipefail
: "${LOCAL_IP:?}" "${NIC_NAME:?}" "${MODEL_PATH:?}"
export ASCEND_GLOBAL_RESOURCE_CONFIG='{"comm_resource_config.protocol_desc":["hccs:device"]}'
export ASCEND_LOCAL_COMM_RES='{"version":"1.2"}'
export HCCL_IF_IP="$LOCAL_IP"
export GLOO_SOCKET_IFNAME="$NIC_NAME"
export TP_SOCKET_IFNAME="$NIC_NAME"
export HCCL_SOCKET_IFNAME="$NIC_NAME"
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export VLLM_USE_V2_MODEL_RUNNER=0
export HCCL_OP_EXPANSION_MODE=AIV
export HCCL_BUFFSIZE=1024
export ASCEND_RT_VISIBLE_DEVICES="$1"
kv_config="$(python3 /opt/glm53flash/connector_config.py D "$4")"
additional_config="$(python3 -c 'import json,os; from pathlib import Path; c=json.loads("{\"multistream_overlap_shared_expert\":true,\"recompute_scheduler_enable\":true,\"enable_shared_expert_dp\":true,\"enable_flashcomm1\":true}"); c["ascend_log_path"]=str(Path(os.environ["ASCEND_PROCESS_LOG_PATH"]).parent/"vllm_ascend"); print(json.dumps(c))')"
exec vllm serve "$MODEL_PATH" \
  --host 0.0.0.0 \
  --port "$2" \
  --data-parallel-size "$3" \
  --data-parallel-rank "$4" \
  --data-parallel-address "$5" \
  --data-parallel-rpc-port "$6" \
  --tensor-parallel-size "$7" \
  --enable-expert-parallel \
  --seed 1024 \
  --served-model-name glm \
  --safetensors-load-strategy prefetch \
  --max-num-seqs 32 \
  --max-model-len 1048576 \
  --max-num-batched-tokens 192 \
  --trust-remote-code \
  --quantization ascend \
  --limit-mm-per-prompt '{"image": 1, "video": 0}' \
  --gpu-memory-utilization 0.92 \
  --tool-call-parser glm47 \
  --reasoning-parser glm47 \
  --enable-auto-tool-choice \
  --kv-cache-dtype int8 \
  --compilation-config '{"cudagraph_mode": "FULL_DECODE_ONLY"}' \
  --speculative-config '{"num_speculative_tokens": 3, "method": "deepseek_mtp", "enforce_eager": false}' \
  --additional-config "$additional_config" \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --kv-transfer-config "$kv_config"
