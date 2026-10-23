#!/usr/bin/env bash
# Foreground, separate terminal, on the Prefill host's candidate container.
set -Eeuo pipefail
: "${RUN_DIR:?host-persistent unique session directory}"
bundle="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v mooncake_master >/dev/null || { echo 'mooncake_master is absent; no packages were installed' >&2; exit 1; }
exec python3 "$bundle/capture.py" --log-dir "$RUN_DIR/master" -- mooncake_master \
  --rpc_port=50088 --metrics_port=9003 --enable_metric_reporting=true \
  --eviction_high_watermark_ratio=0.9 --eviction_ratio=0.1 \
  --logtostderr=true --minloglevel=0 --v=1
