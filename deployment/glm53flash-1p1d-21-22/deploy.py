#!/usr/bin/env python3
"""Fixed two-node GLM-5.3-Flash A3 recipe. Standard library; never SSH/pull/remove."""
import argparse
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
import urllib.request

ROOT = Path(__file__).resolve().parent
IMAGE = "quay.io/ascend/vllm-ascend:v0.30.0rc2-a3"
MODEL = "/var/ai-model/GLM-5.3-Flash-w8a8"
LOGROOT = Path("/var/ai-model/lzd/glm53flash-1p1d-21-22")
IPS = {21: "192.0.2.21", 22: "192.0.2.22"}
PROXY = "/vllm-workspace/vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py"
COMMIT = "4e6eac4e476d9be3476e99440f4cabed67b253ea"
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
    result = {"node": node, "ip": IPS[node], "service": service, "role": role,
              "container": name, "image": IMAGE, "model": MODEL, "log_root": LOGROOT.as_posix(),
              "nic": nic or "AUTO_BY_LOCAL_IP", "command": command,
              "ports": PORTS.get("model" + str(node), []) if service == "model" else PORTS[service],
              "protocol": "device_sdma", "worker_dram_gb": 20 if service == "model" else 0,
              "node_pool_gb": 320 if service == "model" else 0,
              "vllm_ascend_reference_commit": COMMIT,
              "topology": {"P": {"nodes": [21], "dp": 2, "tp": 8, "pp": 1},
                           "D": {"nodes": [22], "dp": 4, "tp": 4, "pp": 1}}}
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
    for name, cmd in [("kernel-tail", ["journalctl", "-k", "-n", "500", "--no-pager"]),
                      ("npu-smi", ["npu-smi", "info"]), ("memory", ["free", "-m"]),
                      ("sockets", ["ss", "-lnt"])]:
        try:
            result = run(cmd, check=False, timeout=15)
            (out / (name + ".txt")).write_text("exit={}\n{}\n{}".format(
                result.returncode, result.stdout[-120000:], result.stderr[-8000:]), encoding="utf-8")
        except Exception as exc:
            (out / (name + ".txt")).write_text(str(exc), encoding="utf-8")
    if container:
        for label, cmd in [("state", ["docker", "inspect", "--format", "{{json .State}}", container]),
                           ("stdout", ["docker", "logs", "--tail", "1000", container])]:
            try:
                result = run(cmd, check=False, timeout=15)
                (out / (label + ".txt")).write_text(result.stdout[-200000:] + result.stderr[-12000:], encoding="utf-8")
            except Exception as exc:
                (out / (label + ".txt")).write_text(str(exc), encoding="utf-8")


def start(plan):
    if platform.system() != "Linux":
        raise RuntimeError("Real start requires Linux; use plan / --dry-run on this host")
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
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns())[-6:]
    run_dir = LOGROOT / "runs" / (stamp + "-" + plan["service"] + "-" + str(plan["node"]))
    run_dir.mkdir(parents=True, mode=0o700)
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    capture(run_dir / "before")
    env = {"LOCAL_IP": plan["ip"], "NIC_NAME": plan["nic"], "MODEL_PATH": MODEL,
           "PYTHONUNBUFFERED": "1", "PYTHONHASHSEED": "0", "ACL_OP_INIT_MODE": "1",
           "ASCEND_ENABLE_USE_FABRIC_MEM": "1", "ASCEND_RT_VISIBLE_DEVICES": ",".join(map(str, range(16))),
           "MMC_META_CONFIG_PATH": "/opt/glm53flash/config/mmc-meta.conf",
           "MMC_LOCAL_CONFIG_PATH": "/opt/glm53flash/config/mmc-local.conf",
           "ASCEND_PROCESS_LOG_PATH": "/run/glm53flash/plog"}
    argv = ["docker", "run", "--pull", "never", "--detach", "--name", plan["container"], "--network", "host",
            "--shm-size", "100g", "--ulimit", "memlock=-1:-1", "--cap-add", "IPC_LOCK",
            "--label", "deployment=glm53flash-1p1d-21-22", "--label", "service=" + plan["service"],
            "--log-opt", "max-size=50m", "--log-opt", "max-file=5",
            "--volume", str(ROOT) + ":/opt/glm53flash:ro",
            "--volume", str(run_dir) + ":/run/glm53flash",
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
    argv += ["--entrypoint", "/bin/bash", IMAGE, "/opt/glm53flash/container-env.sh",
             "python3", "/opt/glm53flash/runtime.py", plan["service"], "--"] + plan["command"]
    try:
        result = run(argv, timeout=60)
        (run_dir / "container-id.txt").write_text(result.stdout.strip() + "\n", encoding="utf-8")
        time.sleep(2)
        state = json.loads(run(["docker", "inspect", "--format", "{{json .State}}", plan["container"]]).stdout)
        if not state["Running"]:
            raise RuntimeError("Container exited with code {}; inspect {}".format(state["ExitCode"], run_dir))
    except Exception:
        try:
            capture(run_dir / "failure", plan["container"])
        except Exception as capture_error:
            print("Failure capture could not complete: " + str(capture_error), file=sys.stderr)
        raise
    print("Created {}. Startup is asynchronous; container running is not model/cache acceptance.\nLogs: {}".format(
        plan["container"], run_dir))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "start", "collect"])
    parser.add_argument("--service", choices=["meta", "model", "proxy"], default="model")
    parser.add_argument("--node", type=int, choices=[21, 22], required=True)
    parser.add_argument("--nic")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.service != "model" and args.node != 21:
        parser.error("Meta and proxy are fixed on node 21")
    plan = description(args.service, args.node, args.nic)
    if args.action == "plan" or args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    elif args.action == "collect":
        if platform.system() != "Linux":
            raise RuntimeError("Read-only live collection requires Linux")
        output = LOGROOT / "manual-evidence" / (time.strftime("%Y%m%d-%H%M%S") + "-" + str(args.node))
        capture(output, plan["container"])
        collector = ROOT / "tools" / "collect_server.py"
        if collector.exists():
            try:
                result = run([sys.executable, str(collector), "--output", str(output / "full"),
                              "--container", plan["container"]], check=False, timeout=900)
            except subprocess.TimeoutExpired:
                (output / "collector-status.txt").write_text(
                    "timeout=900s\npartial=true\nReview preserved files and manifest before using this evidence.\n", encoding="utf-8")
                raise RuntimeError("Collector timed out; partial evidence preserved at " + str(output))
            (output / "collector-status.txt").write_text("exit={}\n{}\n{}".format(
                result.returncode, result.stdout[-20000:], result.stderr[-8000:]), encoding="utf-8")
            if result.returncode:
                raise RuntimeError("Collector failed; partial evidence preserved at " + str(output))
        print(output)
    else:
        start(plan)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
