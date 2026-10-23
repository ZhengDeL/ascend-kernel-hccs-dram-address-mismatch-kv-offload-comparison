#!/usr/bin/env python3
"""Official per-DP launcher arguments with per-rank logs and failure propagation."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["P", "D"], required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--dp-size", type=int, required=True)
    parser.add_argument("--tp-size", type=int, required=True)
    parser.add_argument("--dp-address", required=True)
    parser.add_argument("--dp-rpc-port", default="12325")
    parser.add_argument("--vllm-start-port", type=int, required=True)
    args = parser.parse_args()
    if (args.role, args.dp_size, args.tp_size, args.dp_address, args.vllm_start_port) not in [
            ("P", 2, 8, "192.0.2.21", 9081), ("D", 4, 4, "192.0.2.22", 9900)]:
        parser.error("Only the fixed official two-node A3 topology is supported")
    processes, streams = [], []
    stop_signal = [None]
    def stop(signum, _frame):
        stop_signal[0] = signum
        for proc in processes:
            if proc.poll() is None:
                proc.send_signal(signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for rank in range(args.dp_size):
            folder = Path("/run/glm53flash") / ("dp" + str(rank))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "plog").mkdir(exist_ok=True)
            env = os.environ.copy()
            env["ASCEND_PROCESS_LOG_PATH"] = str(folder / "plog")
            env["FLASH_ROLE"] = args.role
            devices = ",".join(str(i) for i in range(rank * args.tp_size, (rank + 1) * args.tp_size))
            command = ["bash", args.template, devices, str(args.vllm_start_port + rank),
                       str(args.dp_size), str(rank), args.dp_address, args.dp_rpc_port, str(args.tp_size)]
            stream = (folder / "stdout.log").open("a", encoding="utf-8")
            streams.append(stream)
            processes.append(subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, env=env))
            print("Started {} DP{} pid={} devices={} port={}; log={}".format(
                args.role, rank, processes[-1].pid, devices, args.vllm_start_port + rank, folder), flush=True)
        while True:
            for rank, proc in enumerate(processes):
                code = proc.poll()
                if code is not None:
                    if stop_signal[0] is not None:
                        return 128 + stop_signal[0]
                    print("DP{} exited: {}; terminating only this launcher's remaining children".format(rank, code), flush=True)
                    stop(signal.SIGTERM, None)
                    return code if code > 0 else 128 - code if code < 0 else 1
            if stop_signal[0] is not None:
                return 128 + stop_signal[0]
            time.sleep(1)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 30
        for proc in processes:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    sys.exit(main())
