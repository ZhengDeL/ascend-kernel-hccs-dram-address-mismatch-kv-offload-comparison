#!/usr/bin/env python3
"""Offline schema/shell/gate/stub checks; never import torch or contact a server."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "validation-artifacts"


def main():
    checks = []
    for path in ROOT.glob("config/*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["kv_connector"] in ("LMCacheMPConnector", "MultiConnector", "MooncakeConnectorV2")
        children = data.get("kv_connector_extra_config", {}).get("connectors", [data])
        for child in children:
            if child["kv_connector"] == "LMCacheMPConnector":
                assert child["kv_connector_module_path"] == "lmcache.integration.vllm.lmcache_mp_connector"
                extra = child["kv_connector_extra_config"]
                assert extra["lmcache.mp.mp_transfer_mode"] == "lmcache_driven"
                assert extra["lmcache.mp.autostart"] is False
                assert "lmcache.mp.server_urls" not in extra
        checks.append("JSON " + path.name)
    capability = json.loads((ROOT / "CAPABILITIES.json").read_text(encoding="utf-8"))
    assert capability["attention_block_size"] is None and capability["chunk_size"] is None
    assert not capability["npu_tested"] and not capability["performance_limit_tested"]
    for rank in (0, 1):
        env = (ROOT / "config" / ("cache-p-dp%d.env" % rank)).read_text(encoding="utf-8")
        for key in ("ATTENTION_BLOCK_SIZE", "MAMBA_BLOCK_SIZE", "LMCACHE_CHUNK_SIZE", "LMCACHE_CAPACITY_REVIEWED"):
            assert re.search(r"^" + key + r"=$", env, re.M)
    checks.append("Unresolved real geometry remains blank; capabilities are untested")
    for path in list(ROOT.glob("*.md")) + list(ROOT.glob("*.json")) + list(ROOT.glob("*.sh")) + list(ROOT.glob("config/*")):
        data = path.read_text(encoding="utf-8")
        for candidate in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", data):
            address = ipaddress.ip_address(candidate)
            assert address.is_loopback or candidate == "0.0.0.0" or address in ipaddress.ip_network("192.0.2.0/24"), (path.name, candidate)
    checks.append("Addresses restricted to RFC5737 / loopback / wildcard bind")
    bash = shutil.which("bash")
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.is_file():
        bash = str(git_bash)
    if not bash:
        raise RuntimeError("Bash required for offline shell/stub validation")
    ARTIFACTS.mkdir(exist_ok=True)
    for path in ROOT.glob("*.sh"):
        assert b"\r" not in path.read_bytes(), path.name
        subprocess.run([bash, "-n", path.name], cwd=ROOT, check=True, capture_output=True)
    for path in ROOT.glob("*.md"):
        for number, block in enumerate(re.findall(r"```bash\n(.*?)```", path.read_text(encoding="utf-8"), re.S)):
            result = subprocess.run([bash, "-n"], input=block, text=True, cwd=ROOT, capture_output=True)
            assert result.returncode == 0, (path.name, number, result.stderr)
    checks.append("Shell scripts LF and Bash syntax, including documentation examples")
    blank = subprocess.run([bash, "./start_cache.sh", "0"], cwd=ROOT, capture_output=True, text=True)
    assert blank.returncode == 2 and "ATTENTION_BLOCK_SIZE" in blank.stderr
    (ARTIFACTS / "unresolved-gate.txt").write_text(blank.stderr, encoding="utf-8")
    checks.append("Real blank geometry fails before any lmcache invocation")
    case = ARTIFACTS / "stub-case"
    (case / "config").mkdir(parents=True, exist_ok=True)
    (case / "bin").mkdir(exist_ok=True)
    for name in ("start_cache.sh", "diagnostic-env.sh"):
        (case / name).write_bytes((ROOT / name).read_bytes())
    env = (ROOT / "config/cache-p-dp0.env").read_text(encoding="utf-8")
    for key, value in {"ATTENTION_BLOCK_SIZE": "8", "MAMBA_BLOCK_SIZE": "8", "LMCACHE_CHUNK_SIZE": "16", "LMCACHE_L1_GIB": "1", "LMCACHE_CAPACITY_REVIEWED": "yes"}.items():
        env = re.sub(r"^" + key + r"=.*$", key + "=" + value, env, flags=re.M)
    (case / "config/cache-p-dp0.env").write_text(env, encoding="utf-8", newline="\n")
    (case / "bin/lmcache").write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$STUB_ARGS\"\necho 'STUB ONLY: no server and no NPU activity'\nexit \"${STUB_EXIT_CODE:-7}\"\n", encoding="utf-8", newline="\n")
    # Git Bash exposes a synthetic /proc without Linux MemAvailable. Only this
    # copied test case receives synthetic 2GiB availability; production does not.
    (case / "bin/awk").write_text("#!/usr/bin/env bash\necho 2097152\n", encoding="utf-8", newline="\n")
    result = subprocess.run([bash, "-c", 'ulimit() { echo STUB_ONLY_MEMLOCK; }; export -f ulimit; export PATH="$PWD/validation-artifacts/stub-case/bin:$PATH"; export STUB_ARGS="$PWD/validation-artifacts/stub-case/argv.txt"; export LMCACHE_LOG_ROOT="$PWD/validation-artifacts/stub-case/logs"; export STUB_EXIT_CODE=7; bash validation-artifacts/stub-case/start_cache.sh 0'], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 7, (result.returncode, result.stdout, result.stderr)
    args = (case / "argv.txt").read_text(encoding="utf-8").splitlines()
    assert args[0] == "server" and args[args.index("--chunk-size") + 1] == "16"
    assert args[args.index("--l1-size-gb") + 1] == "1"
    assert "--separate-object-groups" in args
    assert args[args.index("--supported-transfer-mode") + 1] == "lmcache_driven"
    (ARTIFACTS / "stub-failure.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    checks.append("Synthetic geometry/L1=1GiB stub forwards exact CLI; pipefail preserves exit 7")
    report = {"offline_only": True, "checks": checks, "npu_or_server_tested": False,
              "stub_geometry_note": "8/8/16, L1=1 and synthetic 2GiB MemAvailable exist ONLY in copied validation stub, not deployment defaults"}
    (ARTIFACTS / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
