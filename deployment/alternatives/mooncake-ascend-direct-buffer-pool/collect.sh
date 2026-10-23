#!/usr/bin/env bash
# Run on each host. Reads system state; never stops services or alters OS knobs.
set -Eeo pipefail
: "${1:?absolute candidate session directory}"
run_dir="$(realpath -- "$1")"
case "$run_dir" in /|/var|/var/ai-model|/var/ai-model/lzd) echo 'Use a specific candidate session directory' >&2; exit 2;; esac
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$run_dir/host-evidence-$stamp"
mkdir -p -- "$out"
chmod 700 -- "$out"
partial=0
printf 'name\tcommand_exit\ttail_exit\n' > "$out/command-status.tsv"
capture() {
  name="$1"; shift
  set +e
  timeout 30s "$@" 2>&1 | tail -c 8388608 > "$out/$name.txt"
  statuses=("${PIPESTATUS[@]}")
  set -e
  printf '%s\t%s\t%s\n' "$name" "${statuses[0]}" "${statuses[1]}" >> "$out/command-status.tsv"
  if (( statuses[0] != 0 || statuses[1] != 0 )); then partial=1; fi
  return 0
}
capture os-release cat /etc/os-release
capture kernel uname -a
capture cmdline cat /proc/cmdline
capture iomem cat /proc/iomem
capture memory free -m
capture numa numactl -H
capture pci lspci -nnk
capture npu npu-smi info
capture sockets ss -lnt
capture kernel-journal journalctl -k -n 10000 --no-pager -o short-precise
capture system-journal journalctl -n 10000 --no-pager -o short-precise
capture dmesg dmesg -T
capture disk df -h "$run_dir"
printf '%s\n' 'System commands are bounded to 30s and latest 8MiB each; review command-status.tsv.' \
  'Candidate per-DP native/stdout logs are already in the session directory.' \
  'Collect container-only plog/Mooncake/native files through the required persistent mounts.' \
  'This archive contains private machine evidence. Keep local; never push it directly to GitHub.' > "$out/COLLECTION-SCOPE.txt"
printf 'partial=%s\n' "$partial" > "$out/collection-status.txt"
archive="${run_dir%/}-$stamp.tar.gz"
set +e
tar -czf "$archive" -C "$(dirname -- "$run_dir")" "$(basename -- "$run_dir")"
status=$?
set -e
sha256sum -- "$archive" > "$archive.sha256"
printf 'Archive: %s\nArchive exit: %s; review evidence/command-status.tsv and changing-file warnings.\n' "$archive" "$status"
if (( status == 0 && partial != 0 )); then exit 2; fi
exit "$status"
