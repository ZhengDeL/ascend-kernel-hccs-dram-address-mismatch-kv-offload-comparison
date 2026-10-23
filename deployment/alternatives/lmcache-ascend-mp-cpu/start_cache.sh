#!/usr/bin/env bash
# Manual foreground launch only. No install, Docker, SSH or OS mutations.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -ne 1 || ( "$1" != 0 && "$1" != 1 ) ]]; then
  echo 'Usage: bash start_cache.sh {0|1}; fill matching config/cache-p-dpN.env first.' >&2
  exit 2
fi
rank="$1"
source "$SCRIPT_DIR/config/cache-p-dp${rank}.env"
source "$SCRIPT_DIR/diagnostic-env.sh"
for key in ATTENTION_BLOCK_SIZE MAMBA_BLOCK_SIZE LMCACHE_CHUNK_SIZE P_MAX_NUM_BATCHED_TOKENS LMCACHE_L1_GIB; do
  value="${!key:-}"
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "Unresolved/invalid $key: obtain the actual GLM hybrid geometry; no default is assumed." >&2
    exit 2
  fi
done
if (( LMCACHE_CHUNK_SIZE % ATTENTION_BLOCK_SIZE != 0 || LMCACHE_CHUNK_SIZE % MAMBA_BLOCK_SIZE != 0 )); then
  echo 'Chunk size must align with attention AND Mamba blocks; also verify every cacheable group span.' >&2
  exit 2
fi
if (( P_MAX_NUM_BATCHED_TOKENS < MAMBA_BLOCK_SIZE )); then
  echo 'The fixed P max-num-batched-tokens is below the resolved Mamba block size; this candidate is blocked.' >&2
  exit 2
fi
if [[ "${LMCACHE_CAPACITY_REVIEWED:-}" != yes ]]; then
  echo 'Review both L1 pools + model/OS headroom, NUMA and memlock; then set LMCACHE_CAPACITY_REVIEWED=yes.' >&2
  exit 2
fi
if [[ -r /proc/meminfo ]]; then
  available_kib="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
  if [[ ! "$available_kib" =~ ^[0-9]+$ ]] || (( available_kib < LMCACHE_L1_GIB * 1024 * 1024 )); then
    echo 'MemAvailable is below this server L1 cap or unavailable; capacity gate failed.' >&2
    exit 2
  fi
fi
command -v lmcache >/dev/null 2>&1 || { echo 'lmcache CLI is absent; follow the manual pinned installation guide.' >&2; exit 2; }
log_root="${LMCACHE_LOG_ROOT:-/var/ai-model/lzd/lmcache-ascend-mp-cpu}"
run_dir="$log_root/runs/$(date -u +%Y%m%dT%H%M%SZ)-p-dp${rank}-$$"
mkdir -p "$run_dir/plog" "$run_dir/mooncake"
chmod 700 "$run_dir"
cp -- "$SCRIPT_DIR/config/cache-p-dp${rank}.env" "$run_dir/effective-cache.env"
{ date -u; uname -a; ulimit -l; if [[ -r /proc/meminfo ]]; then cat /proc/meminfo; fi; } > "$run_dir/capacity-before.txt"
export ASCEND_PROCESS_LOG_PATH="$run_dir/plog"
export MC_LOG_DIR="$run_dir/mooncake"
printf 'Candidate only: P DP%s; localhost:%s; L1=%sGiB; chunk=%s; logs=%s\n' \
  "$rank" "$LMCACHE_PORT" "$LMCACHE_L1_GIB" "$LMCACHE_CHUNK_SIZE" "$run_dir"
trap 'result=$?; printf "exit_code=%s\n" "$result" > "$run_dir/exit-status.txt"' EXIT
# PIPEFAIL retains a failed lmcache exit even when tee succeeds.
# tee is foreground and unbounded; stop after a short reproduction and inspect disk usage.
lmcache server \
  --transport zmq --host 127.0.0.1 --port "$LMCACHE_PORT" \
  --http-host 127.0.0.1 --http-port "$LMCACHE_HTTP_PORT" \
  --chunk-size "$LMCACHE_CHUNK_SIZE" \
  --l1-size-gb "$LMCACHE_L1_GIB" \
  --separate-object-groups --supported-transfer-mode lmcache_driven \
  --eviction-policy LRU \
  2>&1 | tee -a "$run_dir/lmcache-server.log"
