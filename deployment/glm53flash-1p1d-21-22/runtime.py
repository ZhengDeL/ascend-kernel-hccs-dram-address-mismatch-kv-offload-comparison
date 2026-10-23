#!/usr/bin/env python3
"""Container PID 1: bounded diagnostics, dependency gates, stdout tee, exact exit code."""
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

OUT = Path("/run/glm53flash")


def snapshot(name):
    for label, command in [("npu", ["npu-smi", "info"]), ("memory", ["free", "-m"])]:
        try:
            res = subprocess.run(command, capture_output=True, text=True, timeout=15)
            (OUT / (name + "-" + label + ".txt")).write_text(
                "exit={}\n{}\n{}".format(res.returncode, res.stdout[-100000:], res.stderr[-8000:]), encoding="utf-8")
        except Exception as exc:
            try:
                (OUT / (name + "-" + label + ".txt")).write_text(str(exc), encoding="utf-8")
            except OSError:
                print("Snapshot could not be saved: " + label, file=sys.stderr, flush=True)


def main():
    service = sys.argv[1]
    command = sys.argv[3:]
    (OUT / "plog").mkdir(parents=True, exist_ok=True)
    (OUT / "memcache").mkdir(parents=True, exist_ok=True)
    versions = {}
    for package in ("vllm", "vllm-ascend", "memcache-hybrid", "memfabric-hybrid", "mooncake-transfer-engine"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    (OUT / "versions.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")
    snapshot("before")
    code = 1
    try:
        for module in (("memcache_hybrid", "memfabric_hybrid") if service == "meta" else
                       ("vllm", "vllm_ascend", "memcache_hybrid", "memfabric_hybrid", "mooncake") if service == "model" else
                       ("aiohttp", "fastapi", "uvicorn")):
            importlib.import_module(module)
        # Official vLLM/MemCache entrypoints perform their own connector and HAL checks.
        # Do not impose a different source tree's factory/scanner API on the installed image.
        if service == "model":
            from importlib.util import find_spec
            spec = find_spec("vllm_ascend")
            package = Path(spec.origin).parent
            source = package / "distributed/kv_transfer/kv_pool/ascend_store/backend/memcache_backend.py"
            (OUT / "feature-source.json").write_text(json.dumps({
                "memcache_backend_source_present": source.is_file(),
                "memcache_dp_init_barrier_text_present": source.is_file() and
                    "memcache_dp_init_barrier" in source.read_text(encoding="utf-8", errors="replace"),
                "document_reference_commit": "4e6eac4e476d9be3476e99440f4cabed67b253ea",
                "image_tag": "v0.30.0rc2-a3",
                "runtime_feature_acceptance": "pending actual vLLM initialization"
            }, indent=2), encoding="utf-8")
        if service == "proxy" and not Path(command[1]).is_file():
            raise RuntimeError("Official proxy is absent in selected image")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, start_new_session=True)
        def forward(signum, _frame):
            if process.poll() is None:
                os.killpg(process.pid, signum)
        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        with (OUT / "stdout.log").open("a", encoding="utf-8") as log:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
        code = process.wait()
        if code < 0:
            code = 128 - code
    except Exception as exc:
        print("Runtime preflight/launch failed: " + repr(exc), file=sys.stderr, flush=True)
        try:
            (OUT / "runtime-error.txt").write_text(repr(exc), encoding="utf-8")
        except OSError:
            pass
    finally:
        try:
            (OUT / "exit-status.json").write_text(json.dumps({"exit_code": code, "finished_at": time.time()}), encoding="utf-8")
        except OSError as exc:
            print("Could not save exit status; original code={}: {}".format(code, exc), file=sys.stderr, flush=True)
        snapshot("after" if code == 0 else "failure")
    return code


if __name__ == "__main__":
    sys.exit(main())
