#!/usr/bin/env python3
"""Bounded, read-only Linux evidence collection; Python 3.8+ standard library."""
import argparse
import datetime
import glob
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from analyze_iomem import analyze, load_windows

MAX_BYTES = 2 * 1024 * 1024
PACKAGES = ["torch", "torch-npu", "vllm", "vllm-ascend", "memfabric", "memcache",
            "memcache-hybrid", "memfabric-hybrid", "mf-memcache", "mf-transfer", "moon-cake",
            "mooncake-transfer-engine", "mooncake-transfer-engine-npu"]
ENV_ALLOW = ["FULL_MEMCACHE_STORAGE_ENABLED", "FULL_MEMCACHE", "ASCEND_BUFFER_POOL",
             "ASCEND_BUFFER_POOL_SIZE", "ASCEND_BUFFER_POOL_ENABLE", "ASCEND_BUFFER_POOL_TYPE",
             "ASCEND_HOME_PATH", "ASCEND_OPP_PATH", "ASCEND_AICPU_PATH",
             "ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES", "HCCL_WHITELIST_DISABLE",
             "ASCEND_ENABLE_USE_FABRIC_MEM", "ASCEND_GLOBAL_RESOURCE_CONFIG", "ASCEND_LOCAL_COMM_RES",
             "HCCL_SOCKET_IFNAME", "HCCL_IF_IP", "HCCL_CONNECT_TIMEOUT", "HCCL_EXEC_TIMEOUT",
             "PYTORCH_NPU_ALLOC_CONF", "MALLOC_ARENA_MAX", "LD_LIBRARY_PATH",
             "MF_HYBM_RDMA_SWAP_SPACE_SIZE", "HCOM_MAX_SLICE_SIZE", "HCOM_RECV_DATA_SIZE",
             "MEMFABRIC_HYBRID_EXTEND_LIB_PATH", "MF_HOST_SHM_USE_MEMFD",
             "MMC_META_CONFIG_PATH", "MMC_LOCAL_CONFIG_PATH", "PYTHONUNBUFFERED", "PYTHONFAULTHANDLER",
             "VLLM_CONFIGURE_LOGGING", "VLLM_LOGGING_LEVEL", "VLLM_LOGGING_COLOR", "VLLM_LOGGING_STREAM",
             "VLLM_LOGGING_CONFIG_PATH", "VLLM_LOG_STATS_INTERVAL", "ASCEND_GLOBAL_LOG_LEVEL",
             "ASCEND_SLOG_PRINT_TO_STDOUT", "ASCEND_PROCESS_LOG_PATH", "HCCL_ENTRY_LOG_ENABLE",
             "MF_LOG_LEVEL", "ASCEND_MF_LOG_LEVEL", "MC_LOG_LEVEL", "MC_LOG_DIR"]

VERSION_FILES = ["/etc/os-release", "/etc/lsb-release", "/proc/version",
                 "/usr/local/Ascend/driver/version.info", "/usr/local/Ascend/driver/scene.info"]
SYSTEM_FILES = ["/proc/meminfo", "/proc/iomem", "/proc/buddyinfo", "/proc/pagetypeinfo",
                "/proc/zoneinfo", "/proc/interrupts", "/proc/modules", "/proc/locks",
                "/proc/sys/kernel/osrelease", "/proc/config.gz", "/sys/kernel/security/lockdown",
                "/proc/swaps", "/proc/pressure/memory", "/proc/self/limits",
                "/proc/self/status", "/proc/self/cgroup", "/proc/self/mountinfo",
                "/sys/devices/system/node/online", "/sys/devices/system/node/possible",
                "/sys/devices/system/memory/block_size_bytes", "/sys/kernel/mm/transparent_hugepage/enabled",
                "/sys/devices/system/memory/auto_online_blocks",
                "/sys/kernel/mm/transparent_hugepage/defrag", "/proc/sys/vm/nr_hugepages",
                "/proc/sys/vm/overcommit_memory", "/proc/sys/vm/overcommit_ratio",
                "/proc/sys/vm/max_map_count", "/proc/sys/vm/zone_reclaim_mode",
                "/proc/sys/vm/swappiness", "/proc/sys/kernel/numa_balancing",
                "/sys/fs/cgroup/cgroup.controllers", "/sys/fs/cgroup/memory.max",
                "/sys/fs/cgroup/memory.high", "/sys/fs/cgroup/memory.current",
                "/sys/fs/cgroup/memory.stat", "/sys/fs/cgroup/memory.events",
                "/sys/fs/cgroup/cpuset.mems.effective", "/sys/fs/cgroup/cpuset.cpus.effective",
                "/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes"]
PATTERNS = ["/sys/devices/system/node/node[0-9]*/meminfo", "/sys/devices/system/node/node[0-9]*/cpulist",
            "/sys/devices/system/node/node[0-9]*/distance", "/sys/devices/system/node/node[0-9]*/numastat",
            "/sys/devices/system/node/node[0-9]*/hugepages/hugepages-*/*",
            "/sys/kernel/mm/hugepages/hugepages-*/*",
            "/sys/bus/pci/devices/*/vendor", "/sys/bus/pci/devices/*/device",
            "/sys/bus/pci/devices/*/class", "/sys/bus/pci/devices/*/numa_node",
            "/sys/bus/pci/devices/*/local_cpulist", "/sys/bus/pci/devices/*/resource",
            "/sys/bus/pci/devices/*/current_link_speed", "/sys/bus/pci/devices/*/current_link_width",
            "/sys/module/*/version", "/sys/module/*/srcversion",
            "/sys/module/amd_iommu/parameters/*", "/sys/module/iommu/parameters/*",
            "/sys/module/vfio_iommu_type1/parameters/*",
            "/usr/local/Ascend/ascend-toolkit/*/version.info",
            "/usr/local/Ascend/ascend-toolkit/*/*-linux/ascend_toolkit_install.info",
            "/usr/local/Ascend/*/version.info"]
COMMANDS = [
    ("uname", ["uname", "-a"]), ("lscpu", ["lscpu", "-J"]),
    ("lsmem", ["lsmem", "--all", "--bytes", "--output", "RANGE,SIZE,STATE,NODE"]),
    ("free", ["free", "--bytes"]),
    ("numactl", ["numactl", "--hardware"]), ("numastat", ["numastat", "-m"]),
    ("lspci_tree", ["lspci", "-tv"]), ("lspci_details", ["lspci", "-nnvv"]),
    ("lsmod", ["lsmod"]), ("npu_info", ["npu-smi", "info"]),
    ("dmesg", ["dmesg", "--color=never"]),
    ("boot_kernel_journal", ["journalctl", "-k", "-b", "--no-pager", "-n", "2000", "-o", "short-monotonic"]),
    ("dmi_bios", ["dmidecode", "--type", "bios"]),
    ("dmi_system", ["dmidecode", "--type", "system"]),
    ("dmi_memory", ["dmidecode", "--type", "memory"]),
    ("dmi_processor", ["dmidecode", "--type", "processor"]),
    ("rpm_versions", ["rpm", "-q", "kernel", "kernel-core", "kernel-devel", "glibc", "numactl", "numactl-libs"]),
    ("dpkg_versions", ["dpkg-query", "-W", "-f=${Package}\t${Version}\n", "linux-image-*", "libc6", "numactl", "libnuma1"]),
]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def package_metadata():
    result = {}
    for name in PACKAGES:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
        except Exception as error:
            result[name] = {"error": str(error)}
    return result


def execute(argv, timeout, limit=MAX_BYTES):
    """No shell. Drain both pipes while retaining bounded output; kill own process group."""
    started = time.monotonic()
    result = {"argv": argv, "timeout_seconds": timeout, "started_utc": utc_now()}
    data = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    threads = []
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=os.name == "posix")
    except OSError as error:
        result.update(status="missing_command" if isinstance(error, FileNotFoundError) else "start_error",
                      returncode=None, elapsed_seconds=round(time.monotonic() - started, 3))
        return result, b"", str(error).encode("utf-8")

    def drain(name, pipe):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                totals[name] += len(chunk)
                room = max(0, limit - len(data[name]))
                data[name].extend(chunk[:room])
        finally:
            pipe.close()

    for name in data:
        worker = threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
        worker.start()
        threads.append(worker)
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=5)
    for worker in threads:
        worker.join(timeout=2)
    if timed_out:
        data["stderr"].extend(b"\n[collector] command exceeded timeout; process group terminated.\n")
    result.update(status="timeout" if timed_out else "ok" if process.returncode == 0 else "command_failed",
                  returncode=process.returncode, elapsed_seconds=round(time.monotonic() - started, 3),
                  output_bytes=totals,
                  truncated={name: totals[name] > limit for name in data},
                  pipe_drain_incomplete=any(worker.is_alive() for worker in threads))
    return result, bytes(data["stdout"]), bytes(data["stderr"])


class Collector:
    def __init__(self, output, root="/", timeout=15):
        self.output, self.root, self.timeout = Path(output), Path(root), timeout
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(self.output, 0o700)
        self.files, self.commands, self.notes = [], [], []

    def save(self, relative, data):
        destination = self.output / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(data)
        os.chmod(destination, 0o600)
        return str(destination.relative_to(self.output)).replace("\\", "/")

    def json(self, relative, value):
        return self.save(relative, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def source(self, absolute):
        return self.root / absolute.lstrip("/")

    def read(self, absolute):
        source = self.source(absolute)
        record = {"source": absolute}
        try:
            if not stat.S_ISREG(source.stat().st_mode):
                raise OSError("not a regular proc/sys/text file; skipped")
            with source.open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            record.update(status="ok", truncated=len(data) > MAX_BYTES,
                          path=self.save("raw/files/" + absolute.lstrip("/"), data[:MAX_BYTES]))
        except OSError as error:
            data = None
            record.update(status="read_failed", error=str(error))
        self.files.append(record)
        return data[:MAX_BYTES] if data is not None else None

    def command(self, name, argv):
        record, stdout, stderr = execute(argv, self.timeout)
        record["stdout_path"] = self.save("raw/commands/" + name + ".stdout.txt", stdout)
        record["stderr_path"] = self.save("raw/commands/" + name + ".stderr.txt", stderr)
        self.commands.append(record)
        if record["status"] != "ok":
            print("{}: {} (see {})".format(name, record["status"], record["stderr_path"]), file=sys.stderr)
        return record, stdout

    def links(self, name, pattern):
        result = []
        matches = sorted(glob.glob(str(self.source(pattern))))
        if len(matches) > 4096:
            self.notes.append(name + " links capped at 4096; report is incomplete")
        for path in matches[:4096]:
            item = Path(path)
            result.append({"path": "/" + str(item.relative_to(self.root)).replace("\\", "/"),
                           "target": os.readlink(str(item)) if item.is_symlink() else None})
        self.json("raw/" + name + ".json", result)

    def boot_options(self):
        """Collect only OS/memory boot flags; full cmdline can contain credentials."""
        allow = {"amd_iommu", "intel_iommu", "iommu", "iommu.passthrough", "iommu.strict",
                 "mem", "memmap", "numa", "numa_balancing", "crashkernel", "hugepages",
                 "hugepagesz", "default_hugepagesz", "transparent_hugepage", "isolcpus",
                 "nohz_full", "rcu_nocbs", "nosmt", "pcie_acs_override", "efi", "efi_fake_mem"}
        try:
            with self.source("/proc/cmdline").open("r", encoding="utf-8", errors="replace") as stream:
                options = stream.read(131072).split()
            self.json("raw/boot_memory_options.json", {"status": "ok", "options": [
                option for option in options if option.split("=", 1)[0] in allow],
                "full_cmdline_collected": False})
        except OSError as error:
            self.json("raw/boot_memory_options.json", {"status": "read_failed", "error": str(error)})

    def cgroup(self, pid):
        """Only follow that PID's cgroup paths, never traverse unrelated workloads."""
        source = self.source("/proc/{}/cgroup".format(pid))
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            hierarchy, controllers, suffix = parts
            if ".." in Path(suffix).parts:
                continue
            if hierarchy == "0" and not controllers:
                base = "/sys/fs/cgroup/" + suffix.lstrip("/")
                names = ["memory.max", "memory.high", "memory.current", "memory.stat", "memory.events",
                         "memory.numa_stat", "cpuset.mems.effective", "cpuset.cpus.effective"]
            elif "memory" in controllers.split(","):
                base = "/sys/fs/cgroup/memory/" + suffix.lstrip("/")
                names = ["memory.limit_in_bytes", "memory.usage_in_bytes", "memory.stat", "memory.failcnt"]
            else:
                continue
            for name in names:
                absolute = base.rstrip("/") + "/" + name
                if not any(item["source"] == absolute for item in self.files):
                    self.read(absolute)

    def finalize(self, metadata):
        metadata.update(files=self.files, commands=self.commands, notes=self.notes)
        self.json("collection_status.json", metadata)
        hashes = []
        for path in sorted(self.output.rglob("*")):
            if path.is_file():
                hashes.append({"path": str(path.relative_to(self.output)).replace("\\", "/"),
                               "bytes": path.stat().st_size,
                               "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.json("manifest.sha256.json", {"algorithm": "SHA256", "excludes": ["manifest.sha256.json"], "files": hashes})


def container_python(timeout=None):
    # Imports metadata only. Does not import torch/torch_npu/vllm or execute startup scripts.
    # Killing docker CLI alone cannot guarantee killing the exec process in its container.
    # An in-container default SIGALRM therefore bounds the metadata process itself.
    deadline = [] if timeout is None else ["import signal; signal.alarm(" + str(max(1, math.ceil(timeout))) + ")"]
    return "\n".join(deadline + [
        "import importlib.metadata as m,json,os,pathlib,platform,glob",
        "names=" + repr(PACKAGES), "allow=" + repr(ENV_ALLOW),
        "installed={d.metadata.get('Name','').lower().replace('_','-'):d.version for d in m.distributions()}",
        "r={'python':platform.python_version(),'packages':{n:installed.get(n) for n in names},'environment':{k:os.environ[k] for k in allow if k in os.environ},'files':{}}",
        "paths=['/etc/os-release','/proc/self/limits','/proc/self/cgroup','/usr/local/Ascend/driver/version.info']",
        "paths += glob.glob('/usr/local/Ascend/ascend-toolkit/*/version.info')",
        "paths += glob.glob('/usr/local/Ascend/ascend-toolkit/*/*-linux/ascend_toolkit_install.info')",
        "for p in sorted(set(paths))[:128]:",
        " try:",
        "  with open(p, encoding='utf-8', errors='replace') as f: r['files'][p]={'status':'ok','text':f.read(131072)}",
        " except OSError as e: r['files'][p]={'status':'read_failed','error':str(e)}",
        "print(json.dumps(r))"])


def collect(args):
    windows = load_windows(args.windows_json)
    collector = Collector(args.output, root=args.fs_root, timeout=args.timeout)
    metadata = {"schema_version": 1, "started_utc": utc_now(), "platform": platform.platform(),
                "python_version": platform.python_version(), "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
                "read_only": True, "network_or_upload": False, "fixture_root": str(args.fs_root) if args.fs_root != "/" else None,
                "limits": {"each_file_bytes": MAX_BYTES, "each_command_stream_bytes": MAX_BYTES,
                           "each_command_timeout_seconds": args.timeout},
                "disclaimer": "Raw evidence can include serials, IPs, physical addresses, paths and log content. Keep local; review before sharing."}
    iomem = None
    for absolute in VERSION_FILES + SYSTEM_FILES:
        data = collector.read(absolute)
        if absolute == "/proc/iomem":
            iomem = data
    collector.boot_options()
    try:
        release = collector.source("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9._+-]+", release):
            collector.read("/boot/config-" + release)
            collector.read("/lib/modules/" + release + "/build/.config")
    except OSError:
        pass  # Missing release already appears in the per-file collection status.
    selected = set()
    for pattern in PATTERNS:
        matches = sorted(glob.glob(str(collector.source(pattern))))
        for path in matches:
            absolute = "/" + str(Path(path).relative_to(collector.root)).replace("\\", "/")
            selected.add(absolute)
    if len(selected) > 4096:
        collector.notes.append("sysfs/version file selection capped at 4096; report is incomplete")
    for absolute in sorted(selected)[:4096]:
        if not any(item["source"] == absolute for item in collector.files):
            collector.read(absolute)
    collector.links("iommu_groups", "/sys/kernel/iommu_groups/*/devices/*")
    collector.links("pci_driver_links", "/sys/bus/pci/devices/*/driver")
    collector.links("node_memory_blocks", "/sys/devices/system/node/node[0-9]*/memory[0-9]*")
    collector.cgroup("self")
    for pid in args.pid:
        for name in ["maps", "numa_maps", "status", "limits", "cgroup"]:
            collector.read("/proc/{}/{}".format(pid, name))
        collector.cgroup(str(pid))
    if args.fs_root == "/":
        collector.json("raw/python_package_metadata.json", package_metadata())
        collector.json("raw/environment_allowlist.json", {key: os.environ[key] for key in ENV_ALLOW if key in os.environ})
    if iomem is not None:
        report = analyze(iomem.decode("utf-8", errors="replace"), windows)
        iomem_status = next(item for item in collector.files if item["source"] == "/proc/iomem")
        if iomem_status.get("truncated"):
            report["visibility"] = "unknown_or_incomplete"
            report["iomem_system_ram_bytes"] = None
            report["iomem_system_ram_ranges"] = None
            report["window_union_system_ram_intersection_bytes"] = None
            for item in report["windows"]:
                item["iomem_system_ram_intersection_bytes"] = None
                item["intersection_ranges"] = None
            report["interpretation"].append("iomem reached the read limit; do not rely on reported capacity.")
        collector.json("iomem_analysis.json", report)
    else:
        collector.json("iomem_analysis.json", {"visibility": "unavailable", "capacity_bytes": None,
                                               "reason": "iomem read failed; inspect collection_status.json"})
    if not args.no_commands:
        for name, argv in COMMANDS:
            collector.command(name, argv)
        for module in ["drv_devmng", "devdrv", "hisi_hdc", "ascend_drv", "svm", "kae_driver"]:
            collector.command("modinfo_" + module, ["modinfo", module])
        for index, container in enumerate(args.container):
            # Deliberately omit Config.Env, Args, Cmd, labels, full inspect and credential files.
            template = ('{"id":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
                        '"image_name":{{json .Config.Image}},"state":{{json .State.Status}},'
                        '"pid":{{json .State.Pid}},"privileged":{{json .HostConfig.Privileged}},'
                        '"ipc_mode":{{json .HostConfig.IpcMode}},"network_mode":{{json .HostConfig.NetworkMode}},'
                        '"shm_size":{{json .HostConfig.ShmSize}},"memory":{{json .HostConfig.Memory}},'
                        '"memory_swap":{{json .HostConfig.MemorySwap}},"ulimits":{{json .HostConfig.Ulimits}},'
                        '"devices":{{json .HostConfig.Devices}},"mounts":{{json .Mounts}}}')
            _, inspection = collector.command("container_{}_inspect".format(index), ["docker", "inspect", "--type", "container", "--format", template, container])
            try:
                pid = json.loads(inspection)["pid"]
                if isinstance(pid, int) and pid > 0:
                    collector.read("/proc/{}/cgroup".format(pid))
                    collector.cgroup(str(pid))
            except (ValueError, TypeError, KeyError):
                collector.notes.append("container {} PID unavailable; cgroup not followed".format(index))
            collector.command("container_{}_metadata".format(index), ["docker", "exec", container, "python3", "-c", container_python(args.timeout)])
    metadata["completed_utc"] = utc_now()
    collector.finalize(metadata)
    print("Collection complete: {} (raw evidence; keep local, review before sharing)".format(collector.output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="new output directory; refuses to reuse existing directories")
    parser.add_argument("--pid", type=int, action="append", default=[], help="target process PID; repeatable")
    parser.add_argument("--container", action="append", default=[], help="existing Docker container name/ID; repeatable")
    parser.add_argument("--windows-json", type=Path, help="exact-version driver address windows; see example schema")
    parser.add_argument("--timeout", type=float, default=15, help="per command seconds, 0.1..120; default 15")
    parser.add_argument("--no-commands", action="store_true", help="collect files only; do not execute host/container commands")
    parser.add_argument("--fs-root", default="/", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0.1 <= args.timeout <= 120:
        parser.error("timeout must be between 0.1 and 120 seconds")
    if any(pid <= 0 for pid in args.pid):
        parser.error("PID must be positive")
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value) for value in args.container):
        parser.error("container name/ID contains unsupported characters")
    if args.fs_root != "/" and (not args.no_commands or args.container):
        parser.error("fixture root requires --no-commands and no --container")
    if args.fs_root == "/" and sys.platform != "linux":
        parser.error("server collection requires Linux; use unit tests on Windows")
    try:
        collect(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, "Collection failed: {}\n".format(error))


if __name__ == "__main__":
    main()
