#!/usr/bin/env python3
"""Official DP arguments, isolated debug configuration, bounded logs and supervision."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

from logging_utils import StreamTee, exit_code, write_json, write_metadata


def prepare_local_config(source, folder):
    """Preserve official nonlogging fields; LocalConfig supports log_level only.

    log_path is a MetaConfig field, not a LocalConfig setting. LocalService's
    native std::cout/callback output is captured in this DP's stdout.log.
    """
    source = Path(source)
    destination = folder / "config" / "mmc-local.conf"
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = source.read_text(encoding="utf-8").splitlines()
    result, found = [], False
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key == "ock.mmc.log_path":
            continue
        if key == "ock.mmc.log_level":
            if not found:
                result.append("ock.mmc.log_level = debug")
                found = True
        else:
            result.append(line)
    if not found:
        result.append("ock.mmc.log_level = debug")
    destination.write_text("\n".join(result) + "\n", encoding="utf-8", newline="\n")
    return destination


def signal_child_group(process, signum):
    # The group may retain workers after its original leader exits.
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def main(argv=None, out=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["P", "D"], required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--dp-size", type=int, required=True)
    parser.add_argument("--tp-size", type=int, required=True)
    parser.add_argument("--dp-address", required=True)
    parser.add_argument("--dp-rpc-port", default="12325")
    parser.add_argument("--vllm-start-port", type=int, required=True)
    args = parser.parse_args(argv)
    if (args.role, args.dp_size, args.tp_size, args.dp_address, args.vllm_start_port) not in [
            ("P", 2, 8, "192.0.2.21", 9081), ("D", 4, 4, "192.0.2.22", 9900)]:
        parser.error("Only the fixed official two-node A3 topology is supported")
    out = Path("/run/glm53flash") if out is None else Path(out)
    processes, tees, folders = [], [], []
    stop_signal = [None]
    code = 1
    def stop(signum, _frame):
        stop_signal[0] = signum
        for proc in processes:
            signal_child_group(proc, signum)
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for rank in range(args.dp_size):
            if stop_signal[0] is not None:
                code = 128 + stop_signal[0]
                return code
            folder = out / ("dp" + str(rank))
            folder.mkdir(parents=True, exist_ok=True)
            for directory in ("plog", "memcache", "mooncake", "vllm_ascend"):
                (folder / directory).mkdir(exist_ok=True)
            env = os.environ.copy()
            env["ASCEND_PROCESS_LOG_PATH"] = str(folder / "plog")
            env["MC_LOG_DIR"] = str(folder / "mooncake")
            env["MMC_LOCAL_CONFIG_PATH"] = str(prepare_local_config(
                env.get("MMC_LOCAL_CONFIG_PATH", "/opt/glm53flash/config/mmc-local.conf"), folder))
            env["FLASH_ROLE"] = args.role
            write_metadata(folder, "{}-DP{}".format(args.role, rank), env, {
                "local_config": env["MMC_LOCAL_CONFIG_PATH"], "local_log_level": "debug",
                "memcache_log_sink": str(folder / "stdout.log"),
                "memcache_native_log_path_supported": False,
                "vllm_ascend_log_path": str(folder / "vllm_ascend"),
                "native_log_retention": "Native libraries own file rotation; inspect disk usage and collect before cleanup.",
            })
            devices = ",".join(str(i) for i in range(rank * args.tp_size, (rank + 1) * args.tp_size))
            command = ["bash", args.template, devices, str(args.vllm_start_port + rank),
                       str(args.dp_size), str(rank), args.dp_address, args.dp_rpc_port, str(args.tp_size)]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, start_new_session=True)
            processes.append(process)
            folders.append(folder)
            tees.append(StreamTee((process.stdout, process.stderr), folder / "stdout.log"))
            print("Started {} DP{} pid={} devices={} port={}; log={}".format(
                args.role, rank, process.pid, devices, args.vllm_start_port + rank, folder), flush=True)
        while True:
            if stop_signal[0] is not None:
                code = 128 + stop_signal[0]
                return code
            for rank, proc in enumerate(processes):
                result = proc.poll()
                if result is not None:
                    print("DP{} exited: {}; terminating this launcher's remaining process groups".format(
                        rank, result), flush=True)
                    code = exit_code(result) if result != 0 else 1
                    return code
            time.sleep(0.2)
    except Exception:
        traceback.print_exc()
        code = 1
        return code
    finally:
        # Each DP owns a process group including worker grandchildren. Never
        # pkill system-wide or touch another deployment's processes.
        for proc in processes:
            signal_child_group(proc, signal.SIGTERM)
        deadline = time.monotonic() + 30
        for proc in processes:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                signal_child_group(proc, signal.SIGKILL)
                proc.wait()
        for proc in processes:
            signal_child_group(proc, signal.SIGKILL)
        for folder, proc, tee in zip(folders, processes, tees):
            try:
                tee.close()
            except Exception as exc:
                print("Could not finish DP log drainage; launcher code={}: {}".format(code, exc), file=sys.stderr, flush=True)
            write_json(folder / "exit-status.json", {
                "child_returncode": proc.returncode, "exit_code": exit_code(proc.returncode),
                "launcher_exit_code": code, "received_signal": stop_signal[0],
                "finished_at": time.time(), "logging_errors": tee.log.errors,
                "captured_bytes": tee.log.bytes_seen,
                "docker_mirror_bytes_dropped": tee.mirror_bytes_dropped,
            })
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
