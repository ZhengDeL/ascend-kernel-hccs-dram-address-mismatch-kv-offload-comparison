#!/usr/bin/env python3
"""Fixed two-node GLM-5.3-Flash A3 recipe. Standard library; never SSH/pull/remove."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.request

ROOT = Path(__file__).resolve().parent
IMAGE = "quay.io/ascend/vllm-ascend:v0.30.0rc2-a3"
MODEL = "/var/ai-model/GLM-5.3-Flash-w8a8"
LOGROOT = Path("/var/ai-model/lzd/glm53flash-1p1d-21-22")
IPS = {21: "192.0.2.21", 22: "192.0.2.22"}
PROXY = "/vllm-workspace/vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py"
COMMIT = "4e6eac4e476d9be3476e99440f4cabed67b253ea"
LOGGING_ENV = {
    "PYTHONUNBUFFERED": "1", "PYTHONFAULTHANDLER": "1", "VLLM_CONFIGURE_LOGGING": "1",
    "VLLM_LOGGING_LEVEL": "DEBUG", "VLLM_LOGGING_COLOR": "0", "VLLM_LOGGING_STREAM": "ext://sys.stdout",
    "VLLM_LOGGING_CONFIG_PATH": "/opt/glm53flash/config/vllm-logging.json", "VLLM_LOG_STATS_INTERVAL": "10",
    "ASCEND_GLOBAL_LOG_LEVEL": "0", "ASCEND_SLOG_PRINT_TO_STDOUT": "0", "HCCL_ENTRY_LOG_ENABLE": "1",
    "MF_LOG_LEVEL": "0", "ASCEND_MF_LOG_LEVEL": "0", "MC_LOG_LEVEL": "TRACE",
    "MC_LOG_DIR": "/run/glm53flash/mooncake"}
PORTS = {"meta": [5000, 6000, 8000],
         "model21": [12325, 9081, 9082] + list(range(36680, 36698)),
         "model22": [12325] + list(range(9900, 9904)) + list(range(36580, 36600)), "proxy": [8081]}


def run(argv, check=True, timeout=30):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("{} failed ({}): {}".format(argv[0], result.returncode, result.stderr[-4000:]))
    return result


def description(service, node, nic):
    role = ("P" if node == 21 else "D") if service == "model" else service
    name = "glm53flash-21-22-{}-{}".format(service, node)
    if service == "model":
        command = ["python3", "/opt/glm53flash/launch_online_dp.py", "--role", role,
                   "--template", "/opt/glm53flash/run_{}.sh".format(role.lower()),
                   "--dp-size", str(2 if node == 21 else 4), "--tp-size", str(8 if node == 21 else 4),
                   "--dp-address", IPS[node], "--dp-rpc-port", "12325",
                   "--vllm-start-port", str(9081 if node == 21 else 9900)]
    elif service == "meta":
        command = ["python3", "-c", "import sys; from memcache_hybrid import MetaService; sys.exit(MetaService.main())"]
    else:
        command = ["python3", PROXY, "--host", "0.0.0.0", "--port", "8081",
                   "--prefiller-hosts", IPS[21], IPS[21], "--prefiller-ports", "9081", "9082",
                   "--decoder-hosts"] + [IPS[22]] * 4 + ["--decoder-ports"] + [str(p) for p in range(9900, 9904)]
    result = {"deployment": "glm53flash-1p1d-21-22", "node": node, "ip": IPS[node], "service": service, "role": role,
              "container": name, "image": IMAGE, "model": MODEL, "log_root": LOGROOT.as_posix(),
              "nic": nic or "AUTO_BY_LOCAL_IP", "command": command,
              "ports": PORTS.get("model" + str(node), []) if service == "model" else PORTS[service],
              "protocol": "device_sdma", "worker_dram_gb": 20 if service == "model" else 0,
              "node_pool_gb": 320 if service == "model" else 0,
              "vllm_ascend_reference_commit": COMMIT,
              "topology": {"P": {"nodes": [21], "dp": 2, "tp": 8, "pp": 1},
                           "D": {"nodes": [22], "dp": 4, "tp": 4, "pp": 1}}}
    result["logging"] = {
        "mode": "diagnostic", "vllm": "DEBUG", "memcache": "debug", "memfabric": "debug",
        "mooncake": "TRACE", "cann": "DEBUG file plog", "hccl_entry_log": True,
        "host_observer": "current boot snapshots + continuous journal/events; 30s memory, 60s NPU",
        "stdout_rotation": "20MiB + 5 backups; separate first-error 128KiB",
        "host_rotation": "10MiB + 5 backups; separate first 2MiB per stream",
        "native_rotation": "component-specific; vLLM-Ascend has no verified total retention limit",
        "performance_acceptance": "pending <=5% A/B under identical logging conditions"}
    if service == "model":
        from connector_config import connector
        result["connectors"] = [connector(role, rank) for rank in range(2 if node == 21 else 4)]
    return result


def check_model():
    path = Path(MODEL)
    config_path = path / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    text_cfg = cfg.get("text_config", cfg)
    identity = json.dumps(cfg).lower()
    if "glm5next" not in identity and "glm5_next" not in identity:
        raise RuntimeError("config.json is not GLM-5.3-Flash / Glm5Next metadata")
    if "linear_attention" not in json.dumps(text_cfg).lower() and "linear_attn_config" not in identity:
        raise RuntimeError("GLM-5.3-Flash hybrid attention metadata is absent")
    quant = path / "quant_model_description.json"
    quant_text = quant.read_text(encoding="utf-8") if quant.exists() else json.dumps(cfg.get("quantization_config", {}))
    if "W8A8C8" in quant_text.upper() or "W8A8" not in quant_text.upper():
        raise RuntimeError("Require W8A8 identity in quantization metadata; directory name is insufficient; C8 is rejected")
    index_path = path / "model.safetensors.index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    shards = sorted(set(index.get("weight_map", {}).values()))
    if not shards:
        raise RuntimeError("Weight index has no shard references")
    total = 0
    for shard in shards:
        candidate = (path / shard).resolve()
        if candidate.parent != path.resolve() or not candidate.is_file() or candidate.stat().st_size <= 0:
            raise RuntimeError("Missing/invalid weight shard: " + shard)
        total += candidate.stat().st_size
    files = [config_path, index_path] + ([quant] if quant.exists() else [])
    return {"metadata_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            "shard_count": len(shards), "weight_bytes": total,
            "full_weight_hash_verified": False}


def local_nic(node, supplied):
    entries = json.loads(run(["ip", "-j", "address", "show"]).stdout)
    matches = [item["ifname"] for item in entries if any(
        addr.get("local") == IPS[node] for addr in item.get("addr_info", []))]
    if len(matches) != 1 or supplied and supplied != matches[0]:
        raise RuntimeError("Local IP / NIC mismatch for " + IPS[node])
    return matches[0]


def endpoint(host, port):
    with socket.create_connection((host, port), timeout=3):
        pass


def check_ports(ports):
    for port in ports:
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError as exc:
                raise RuntimeError("Refusing start; TCP port {} is already in use: {}".format(port, exc))


def capture(out, container=None):
    """Bounded read-only snapshots; collector failure cannot replace service error."""
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "tools"))
    from collect_server import execute
    commands = [("kernel-tail", ["journalctl", "-k", "-n", "500", "--no-pager"]),
                      ("npu-smi", ["npu-smi", "info"]), ("memory", ["free", "-m"]),
                      ("sockets", ["ss", "-lnt"])]
    if container:
        commands.extend([("state", ["docker", "inspect", "--format", "{{json .State}}", container]),
                         ("stdout", ["docker", "logs", "--tail", "1000", container])])
    status = []
    for name, cmd in commands:
        try:
            info, stdout, stderr = execute(cmd, 15, limit=256 * 1024)
            (out / (name + ".stdout.txt")).write_bytes(stdout)
            (out / (name + ".stderr.txt")).write_bytes(stderr)
            status.append(dict(name=name, **info))
        except Exception as exc:
            status.append({"name": name, "status": "capture_failed", "error": str(exc)})
    (out / "capture-status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")


def start(plan):
    if platform.system() != "Linux":
        raise RuntimeError("Real start requires Linux; use plan / --dry-run on this host")
    plan["started_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + str(time.time_ns())[-8:]
    run_dir = LOGROOT / "runs" / (stamp + "-" + plan["service"] + "-" + str(plan["node"]))
    run_dir.mkdir(parents=True, mode=0o700)
    os.chmod(run_dir, 0o700)
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        capture(run_dir / "before")
        return start_checked(plan, run_dir)
    except Exception:
        # Even preflight failures get their own evidence directory. Never replace
        # the original error with a diagnostic error, or touch an existing container.
        try:
            (run_dir / "start-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            owned = (run_dir / "container-id.txt").read_text().strip() if (run_dir / "container-id.txt").exists() else None
            capture(run_dir / "failure", owned)
        except Exception as capture_error:
            print("Failure capture could not complete: " + str(capture_error), file=sys.stderr)
        print("Startup evidence: " + str(run_dir), file=sys.stderr)
        raise


def launch_observer(plan, run_dir, cid):
    """Detached read-only observer. Its own failure must not stop the business."""
    info = {"container": cid, "started_utc": plan["started_utc"], "status": "launch_pending"}
    try:
        with (run_dir / "os-observer-launch.log").open("ab", buffering=0) as log:
            observer = subprocess.Popen([
                sys.executable, str(ROOT / "host_logging.py"), "--container", cid,
                "--output", str(run_dir / "os"), "--service", plan["service"],
                "--since-utc", plan["started_utc"]], stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        info["pid"] = observer.pid
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and observer.poll() is None:
            if (run_dir / "os/observer-registered.json").exists():
                info["status"] = "registered; initialization coverage pending observer-ready.json"
                break
            time.sleep(0.1)
        else:
            info["status"] = "registration_pending" if observer.poll() is None else "launch_failed"
            info["returncode"] = observer.poll()
    except Exception as error:
        info.update(status="launch_failed", error=str(error))
    try:
        (run_dir / "os-observer-launch.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    except OSError as error:
        print("OS observer metadata could not be saved: " + str(error), file=sys.stderr)
    if "failed" in info["status"] or info["status"] == "registration_pending":
        print("OS logging coverage pending/partial; inspect " + str(run_dir / "os-observer-launch.json"), file=sys.stderr)


def start_checked(plan, run_dir):
    for exe in ("docker", "ip", "npu-smi"):
        if not shutil.which(exe):
            raise RuntimeError("Missing host dependency: " + exe)
    plan["nic"] = local_nic(plan["node"], None if plan["nic"] == "AUTO_BY_LOCAL_IP" else plan["nic"])
    if run(["docker", "container", "inspect", plan["container"]], check=False).returncode == 0:
        raise RuntimeError("Container already exists; refusing to reuse/stop/remove: " + plan["container"])
    image = run(["docker", "image", "inspect", IMAGE], check=False)
    if image.returncode:
        raise RuntimeError("Image is not locally available; no image was pulled: " + IMAGE)
    image_info = json.loads(image.stdout)[0]
    if image_info.get("Architecture") != "amd64":
        raise RuntimeError("AMD CPU requires an amd64 image; found " + str(image_info.get("Architecture")))
    plan["image_id"] = image_info["Id"]
    plan["image_repo_digests"] = image_info.get("RepoDigests", [])
    required = ["/usr/local/Ascend/driver", "/etc/ascend_install.info", "/etc/hccn.conf",
                "/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc"]
    devices = ["/dev/davinci" + str(i) for i in range(16)]
    for path in required + devices:
        if not Path(path).exists():
            raise RuntimeError("Required path/device absent: " + path)
    plan["model_metadata"] = check_model() if plan["service"] == "model" else None
    check_ports(plan["ports"])
    if plan["service"] == "model":
        available = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:"))
        if available < (320 + 64) * 1024 ** 3:
            raise RuntimeError("Workers contribute 320 GiB; local protective headroom is 64 GiB; MemAvailable insufficient")
    if plan["service"] == "model":
        for port in (5000, 6000):
            endpoint(IPS[21], port)
    if plan["service"] == "model":
        busy = run(["pgrep", "-f", "vllm.entrypoints|vllm serve|VLLM::|EngineCore|api_server.py|hccl_test"], check=False)
        if busy.returncode == 0:
            raise RuntimeError("Existing model/communication processes detected (PIDs only): " + busy.stdout.strip())
        if busy.returncode != 1:
            raise RuntimeError("Cannot establish existing model process state; install/check pgrep")
        # Inspect device owners without exposing command lines. Meta owned by this
        # package are allowed; all other NPU users must be reviewed manually.
        if os.geteuid() != 0:
            raise RuntimeError("Model start requires root to inspect all /proc device owners safely")
        allowed = set()
        if plan["node"] == 21:
            identity_template = ('{"image":{{json .Image}},"running":{{json .State.Running}},'
                                 '"deployment":{{json (index .Config.Labels "deployment")}},'
                                 '"service":{{json (index .Config.Labels "service")}}}')
            owned = run(["docker", "inspect", "--format", identity_template,
                         "glm53flash-21-22-meta-21"])
            identity = json.loads(owned.stdout)
            if identity != {"image": plan["image_id"], "running": True,
                            "deployment": "glm53flash-1p1d-21-22", "service": "meta"}:
                raise RuntimeError("Local Meta identity/image/running state does not match this deployment")
            top = run(["docker", "top", "glm53flash-21-22-meta-21", "-eo", "pid"])
            allowed.update(int(line.strip()) for line in top.stdout.splitlines() if line.strip().isdigit())
        owners = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) in allowed:
                continue
            try:
                for fd in (proc / "fd").iterdir():
                    try:
                        target = os.readlink(str(fd))
                    except FileNotFoundError:
                        continue
                    if target in devices:
                        owners.append(int(proc.name))
                        break
            except (FileNotFoundError, ProcessLookupError):
                continue
        if owners:
            raise RuntimeError("NPU device descriptors held by other processes; PIDs only: " + str(sorted(set(owners))))
    if plan["service"] == "proxy":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for host, ports in [(IPS[21], [9081, 9082]), (IPS[22], range(9900, 9904))]:
            for port in ports:
                with opener.open("http://{}:{}/health".format(host, port), timeout=5) as response:
                    if response.status != 200:
                        raise RuntimeError("Model endpoint not ready: {}:{}".format(host, port))
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    for folder in ("plog", "native-ascend", "native-memfabric"):
        (run_dir / folder).mkdir(mode=0o700)
    env = {"LOCAL_IP": plan["ip"], "NIC_NAME": plan["nic"], "MODEL_PATH": MODEL,
           "PYTHONUNBUFFERED": "1", "PYTHONHASHSEED": "0", "ACL_OP_INIT_MODE": "1",
           "ASCEND_ENABLE_USE_FABRIC_MEM": "1", "ASCEND_RT_VISIBLE_DEVICES": ",".join(map(str, range(16))),
           "MMC_META_CONFIG_PATH": "/opt/glm53flash/config/mmc-meta.conf",
           "MMC_LOCAL_CONFIG_PATH": "/opt/glm53flash/config/mmc-local.conf",
           "ASCEND_PROCESS_LOG_PATH": "/run/glm53flash/plog"}
    env.update(LOGGING_ENV)
    argv = ["docker", "run", "--pull", "never", "--detach", "--name", plan["container"], "--network", "host",
            "--shm-size", "100g", "--ulimit", "memlock=-1:-1", "--cap-add", "IPC_LOCK",
            "--label", "deployment=glm53flash-1p1d-21-22", "--label", "service=" + plan["service"],
            "--log-opt", "max-size=50m", "--log-opt", "max-file=5",
            "--volume", str(ROOT) + ":/opt/glm53flash:ro",
            "--volume", str(run_dir) + ":/run/glm53flash",
            "--volume", str(run_dir / "native-ascend") + ":/root/ascend/log",
            "--volume", str(run_dir / "native-memfabric") + ":/var/log/memfabric_hybrid",
            "--volume", "/usr/local/Ascend/driver:/usr/local/Ascend/driver:ro",
            "--volume", "/etc/ascend_install.info:/etc/ascend_install.info:ro",
            "--volume", "/etc/hccn.conf:/etc/hccn.conf:ro"]
    if plan["service"] == "model":
        argv += ["--volume", MODEL + ":" + MODEL + ":ro"]
    for optional in ("/usr/local/dcmi", "/usr/local/bin/npu-smi", "/usr/local/Ascend/driver/tools/hccn_tool"):
        if Path(optional).exists():
            argv += ["--volume", optional + ":" + optional + ":ro"]
    for device in devices + required[3:]:
        argv += ["--device", device]
    for key, value in env.items():
        argv += ["--env", key + "=" + value]
    # PID 1 supervises the real process and preserves its exit code/logs. No idle shell.
    # Capture before CANN environment initialization and package-path discovery,
    # so shell/import failures also reach stdout.log and first-error.log.
    argv += ["--entrypoint", "python3", IMAGE, "/opt/glm53flash/runtime.py", plan["service"], "--",
             "/bin/bash", "/opt/glm53flash/container-env.sh"] + plan["command"]
    result = run(argv, timeout=60)
    cid = result.stdout.strip()
    (run_dir / "container-id.txt").write_text(cid + "\n", encoding="utf-8")
    launch_observer(plan, run_dir, cid)
    time.sleep(2)
    state = json.loads(run(["docker", "inspect", "--format", "{{json .State}}", cid]).stdout)
    if not state["Running"]:
        raise RuntimeError("Container exited with code {}; inspect {}".format(state["ExitCode"], run_dir))
    print("Created {}. Startup is asynchronous; container running is not model/cache acceptance.\nLogs: {}".format(
        plan["container"], run_dir))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "start", "collect"])
    parser.add_argument("--service", choices=["all", "meta", "model", "proxy"])
    parser.add_argument("--node", type=int, choices=[21, 22], required=True)
    parser.add_argument("--nic")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.service = args.service or ("all" if args.action == "collect" else "model")
    if args.service == "all" and args.action != "collect":
        parser.error("--service all only applies to collect")
    if args.service not in ("model", "all") and args.node != 21:
        parser.error("Meta and proxy are fixed on node 21")
    if args.action == "collect":
        if args.dry_run:
            print(json.dumps({"action": "collect", "node": args.node, "service": args.service,
                              "log_root": str(LOGROOT), "read_only": True,
                              "output": "manual-evidence/<UTC>-<node>.tar.gz + .sha256",
                              "live_collection_executed": False}, indent=2))
            return 0
        if platform.system() != "Linux":
            raise RuntimeError("Read-only live collection requires Linux")
        local_nic(args.node, args.nic)
        from diagnostics import collect
        archive, partial = collect(args.node, args.service)
        print(str(archive) + (" (partial; review collection-report.json)" if partial else ""))
        return 2 if partial else 0
    plan = description(args.service, args.node, args.nic)
    if args.action == "plan" or args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        start(plan)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
