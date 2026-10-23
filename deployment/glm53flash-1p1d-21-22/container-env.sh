#!/usr/bin/env bash
# Initialize the selected image's CANN environment, then replace the shell.
set -Eeo pipefail
if [[ -r /usr/local/Ascend/ascend-toolkit/set_env.sh ]]; then
  source /usr/local/Ascend/ascend-toolkit/set_env.sh
elif [[ -r /usr/local/Ascend/cann/set_env.sh ]]; then
  source /usr/local/Ascend/cann/set_env.sh
else
  echo 'Missing CANN set_env.sh in image; install/repair image explicitly.' >&2
  exit 1
fi
set -u
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
memcache_lib="$(python3 -c 'import importlib.util; from pathlib import Path; s=importlib.util.find_spec("memcache_hybrid"); print(str(Path(s.origin).parent / "lib") if s and s.origin else "")')"
python_lib="$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR") or "")')"
export LD_LIBRARY_PATH="${memcache_lib}:${python_lib}:${LD_LIBRARY_PATH:-}"
exec "$@"
