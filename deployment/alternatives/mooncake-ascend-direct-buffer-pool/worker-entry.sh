#!/usr/bin/env bash
# Invoked under capture.py so CANN setup failures are also persisted.
set -Eeo pipefail
if [[ -r /usr/local/Ascend/ascend-toolkit/set_env.sh ]]; then
  source /usr/local/Ascend/ascend-toolkit/set_env.sh
elif [[ -r /usr/local/Ascend/cann/set_env.sh ]]; then
  source /usr/local/Ascend/cann/set_env.sh
else
  echo 'Missing CANN set_env.sh; repair the explicitly selected image first' >&2
  exit 1
fi
set -u
bundle="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$bundle/prepare-worker.sh" "$1" "$2" "$RUN_DIR"
export HCCL_IF_IP="$LOCAL_IP"
export GLOO_SOCKET_IFNAME="$NIC_NAME" TP_SOCKET_IFNAME="$NIC_NAME" HCCL_SOCKET_IFNAME="$NIC_NAME"
export HCCL_OP_EXPANSION_MODE=AIV HCCL_BUFFSIZE=1024
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
if [[ "$1" == P ]]; then
  dp_size=2; tp_size=8; port=$((10081 + $2)); max_seqs=64; max_tokens=4096
  specific=(--prefix-cache-retention-interval None --speculative-config '{"num_speculative_tokens":3,"method":"deepseek_mtp","enforce_eager":true}')
  extra='{"enable_flashcomm1":true,"enable_dsa_cp":true,"enable_prefill_mc2":true,"enable_fused_mc2":1,"enable_shared_expert_dp":true,"multistream_overlap_shared_expert":false}'
else
  dp_size=4; tp_size=4; port=$((10100 + $2)); max_seqs=32; max_tokens=192
  specific=(--compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' --speculative-config '{"num_speculative_tokens":3,"method":"deepseek_mtp","enforce_eager":false}')
  extra='{"multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true,"enable_shared_expert_dp":true,"enable_flashcomm1":true}'
fi
export ASCEND_RT_VISIBLE_DEVICES="$(python3 - "$2" "$tp_size" <<'PY'
import sys
rank, tp = map(int, sys.argv[1:])
print(','.join(map(str, range(rank*tp, (rank+1)*tp))))
PY
)"
additional_config="$(python3 - "$extra" "$CANDIDATE_DP_DIR/vllm_ascend" <<'PY'
import json, sys
config = json.loads(sys.argv[1])
config['ascend_log_path'] = sys.argv[2]
print(json.dumps(config))
PY
)"
exec vllm serve /var/ai-model/GLM-5.3-Flash-w8a8 \
  --host 0.0.0.0 --port "$port" \
  --data-parallel-size "$dp_size" --data-parallel-rank "$2" \
  --data-parallel-address "$LOCAL_IP" --data-parallel-rpc-port 13325 \
  --tensor-parallel-size "$tp_size" --enable-expert-parallel --seed 1024 \
  --served-model-name glm --safetensors-load-strategy prefetch \
  --max-num-seqs "$max_seqs" --max-model-len 1048576 --max-num-batched-tokens "$max_tokens" \
  --trust-remote-code --quantization ascend --limit-mm-per-prompt '{"image":1,"video":0}' \
  --gpu-memory-utilization 0.92 --tool-call-parser glm47 --reasoning-parser glm47 --enable-auto-tool-choice \
  --kv-cache-dtype int8 \
  "${specific[@]}" --additional-config "$additional_config" \
  --enable-prefix-caching --enable-chunked-prefill \
  --kv-transfer-config "$(cat -- "$CANDIDATE_CONNECTOR_PATH")"
