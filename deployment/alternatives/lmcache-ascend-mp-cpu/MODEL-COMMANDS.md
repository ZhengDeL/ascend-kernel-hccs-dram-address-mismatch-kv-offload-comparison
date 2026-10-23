# 手动模型启动模板

仅供依赖/几何已核实后的独立候选容器使用。该模板使用RFC5737示例地址；复制到现场私有材料后填写真实地址和NIC，公开文件保持示例。不要执行旧MemCache包的启动入口，它会加载不同连接器。镜像固定`quay.io/ascend/vllm-ascend:v0.30.0rc2-a3`；容器准备、16逻辑设备和CANN环境按原官方方案人工核对，MP server与相应P workers放同一容器，避免未验证的跨容器NPU IPC差异。

先在每个模型终端设置以下变量。P运行两个终端，`RANK=0/1`；D运行四个终端，`RANK=0/1/2/3`。第一阶段`ROLE=P PHASE=local`只使用LMCache；第二阶段`PHASE=pd`时D先起、P后起。**未知几何、native依赖或第一阶段往返未通过时，不执行第二阶段。**

```bash
cd /var/ai-model/lzd/lmcache-ascend-mp-cpu
export BUNDLE_DIR="$PWD"
export ROLE=P RANK=0 PHASE=local
export P_HOST=192.0.2.21 D_HOST=192.0.2.22
export NIC_NAME=REPLACE_WITH_ACTUAL_NIC
# 先按镜像实际位置source其CANN set_env.sh
source "$BUNDLE_DIR/diagnostic-env.sh"
unset LMCACHE_USE_UPSTREAM_MP
unset MMC_META_CONFIG_PATH MMC_LOCAL_CONFIG_PATH
```

下面代码由人工粘贴到该终端执行，不会自动安装、SSH、创建容器或修改OS；参数保留原官方P/D各自的不同项。容器、权重只读挂载、NPU设备权限、IP/NIC、端口和旧工作负载排除需事先完成。不要在实际线上环境复用未隔离的端口和设备。

```bash
set -Eeuo pipefail
[[ "$ROLE" == P || "$ROLE" == D ]]
[[ "$PHASE" == local || "$PHASE" == pd ]]
[[ "$RANK" =~ ^[0-9]+$ ]]
if [[ "$ROLE" == P ]]; then
  DP=2; TP=8; START_PORT=9081; LOCAL_IP="$P_HOST"
  (( RANK < DP ))
  if [[ "$PHASE" == local ]]; then
    KV_FILE="$BUNDLE_DIR/config/p-dp${RANK}-lmcache-only.json"
  else
    KV_FILE="$BUNDLE_DIR/config/p-dp${RANK}-multi-candidate.json"
  fi
  ADDITIONAL='{"enable_flashcomm1":true,"enable_dsa_cp":true,"enable_prefill_mc2":true,"enable_fused_mc2":1,"enable_shared_expert_dp":true,"multistream_overlap_shared_expert":false}'
  ROLE_FLAGS=(--max-num-seqs 64 --max-num-batched-tokens 4096
    --prefix-cache-retention-interval None
    --speculative-config '{"num_speculative_tokens":3,"method":"deepseek_mtp","enforce_eager":true}')
else
  [[ "$PHASE" == pd ]]
  DP=4; TP=4; START_PORT=9900; LOCAL_IP="$D_HOST"
  (( RANK < DP ))
  KV_FILE="$BUNDLE_DIR/config/d-mooncake.json"
  ADDITIONAL='{"multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true,"enable_shared_expert_dp":true,"enable_flashcomm1":true}'
  ROLE_FLAGS=(--max-num-seqs 32 --max-num-batched-tokens 192
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}'
    --speculative-config '{"num_speculative_tokens":3,"method":"deepseek_mtp","enforce_eager":false}')
fi
[[ "$NIC_NAME" != REPLACE_WITH_ACTUAL_NIC ]]
MODEL_PATH=/var/ai-model/GLM-5.3-Flash-w8a8
LOG_DIR="/var/ai-model/lzd/lmcache-ascend-mp-cpu/model-logs/$(date -u +%Y%m%dT%H%M%SZ)-${ROLE}-dp${RANK}-$$"
mkdir -p "$LOG_DIR/plog" "$LOG_DIR/mooncake" "$LOG_DIR/vllm_ascend"
export ASCEND_PROCESS_LOG_PATH="$LOG_DIR/plog" MC_LOG_DIR="$LOG_DIR/mooncake"
export ASCEND_GLOBAL_RESOURCE_CONFIG='{"comm_resource_config.protocol_desc":["hccs:device"]}'
export ASCEND_LOCAL_COMM_RES='{"version":"1.2"}'
export HCCL_IF_IP="$LOCAL_IP"
export GLOO_SOCKET_IFNAME="$NIC_NAME" TP_SOCKET_IFNAME="$NIC_NAME" HCCL_SOCKET_IFNAME="$NIC_NAME"
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export VLLM_USE_V2_MODEL_RUNNER=0 HCCL_OP_EXPANSION_MODE=AIV HCCL_BUFFSIZE=1024
export ACL_OP_INIT_MODE=1 ASCEND_ENABLE_USE_FABRIC_MEM=1
export PYTHONHASHSEED=0
export ASCEND_RT_VISIBLE_DEVICES="$(python3 -c 'import sys; r,t=map(int,sys.argv[1:]); print(",".join(map(str,range(r*t,(r+1)*t))))' "$RANK" "$TP")"
ADDITIONAL="$(python3 -c 'import json,sys; c=json.loads(sys.argv[1]); c["ascend_log_path"]=sys.argv[2]; print(json.dumps(c))' "$ADDITIONAL" "$LOG_DIR/vllm_ascend")"
KV_CONFIG="$(cat "$KV_FILE")"
cp "$KV_FILE" "$LOG_DIR/connector-effective.json"
trap 'result=$?; printf "exit_code=%s\n" "$result" > "$LOG_DIR/exit-status.txt"' EXIT
vllm serve "$MODEL_PATH" \
  --host 0.0.0.0 --port "$((START_PORT + RANK))" \
  --data-parallel-size "$DP" --data-parallel-rank "$RANK" \
  --data-parallel-address "$LOCAL_IP" --data-parallel-rpc-port 12325 \
  --tensor-parallel-size "$TP" --enable-expert-parallel --seed 1024 \
  --served-model-name glm --safetensors-load-strategy prefetch \
  --max-model-len 1048576 --trust-remote-code --quantization ascend \
  --limit-mm-per-prompt '{"image":1,"video":0}' --gpu-memory-utilization 0.92 \
  --tool-call-parser glm47 --reasoning-parser glm47 --enable-auto-tool-choice \
  --kv-cache-dtype int8 --mamba-cache-mode align \
  --additional-config "$ADDITIONAL" --enable-prefix-caching --enable-chunked-prefill \
  --kv-transfer-config "$KV_CONFIG" "${ROLE_FLAGS[@]}" \
  2>&1 | tee -a "$LOG_DIR/stdout.log"
```

显式`--mamba-cache-mode align`是LMCache混合缓存必要条件；若该镜像CLI不接受或模型resolved groups不符合，应停止候选，不能删掉该参数伪装支持。local阶段两个P DP都起，其完整MLA/KDA状态先在本地往返验证；pd阶段才按P+LMCache、D+Mooncake及原官方proxy组装。proxy命令沿[固定官方GLM教程](https://docs.vllm.ai/projects/ascend/zh-cn/latest/tutorials/models/GLM5.3-Flash.html#54-prefill-decode-disaggregation-with-kv-cache-pool)的API列表手工替换为私有现场值，本包没有自动代理部署入口。

tee保存的模型文件未做有界轮转，短复现期间监测容量。服务退出码是pipefail下整个流水线结果，`tee`自身失败同样应判取证异常。没有健康/推理/卸载/5%性能成功的预设结论。
