#!/usr/bin/env python3
"""Container PID 1: capture preflight/native output, supervise and preserve status."""
import importlib.metadata
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

from logging_utils import CaptureOwnOutput, exit_code, write_json, write_metadata

OUT = Path("/run/glm53flash")


def snapshot(name, out=OUT):
    for label, command in [("npu", ["npu-smi", "info"]), ("memory", ["free", "-m"])]:
        try:
            res = subprocess.run(command, capture_output=True, text=True, timeout=15)
            (out / (name + "-" + label + ".txt")).write_text(
                "exit={}\n{}\n{}".format(res.returncode, res.stdout[-100000:], res.stderr[-8000:]), encoding="utf-8")
        except Exception as exc:
            try:
                (out / (name + "-" + label + ".txt")).write_text(str(exc), encoding="utf-8")
            except OSError:
                print("Snapshot could not be saved: " + label, file=sys.stderr, flush=True)


def feature_source(out):
    """Inspect distribution files without importing torch/vLLM and initializing NPU."""
    source = None
    try:
        distribution = importlib.metadata.distribution("vllm-ascend")
        source = Path(distribution.locate_file(
            "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/memcache_backend.py"))
    except importlib.metadata.PackageNotFoundError:
        pass
    present = source is not None and source.is_file()
    write_json(out / "feature-source.json", {
        "memcache_backend_source_present": present,
        "memcache_dp_init_barrier_text_present": present and
            "memcache_dp_init_barrier" in source.read_text(encoding="utf-8", errors="replace"),
        "document_reference_commit": "4e6eac4e476d9be3476e99440f4cabed67b253ea",
        "image_tag": "v0.30.0rc2-a3",
        "runtime_feature_acceptance": "pending actual vLLM initialization",
    })


def main(argv=None, out=None):
    argv = sys.argv[1:] if argv is None else argv
    out = OUT if out is None else Path(out)
    out.mkdir(parents=True, exist_ok=True)
    # Set up before parsing arguments or importing/starting business packages.
    with CaptureOwnOutput(out / "stdout.log") as capture:
        code = 1
        process = None
        requested_signal = [None]

        def forward(signum, _frame):
            requested_signal[0] = signum
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass

        previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            if len(argv) < 3 or argv[1] != "--" or argv[0] not in ("model", "meta", "proxy"):
                raise ValueError("Usage: runtime.py {model|meta|proxy} -- command [args...]")
            service, command = argv[0], argv[2:]
            for directory in ("plog", "memcache", "mooncake", "vllm_ascend"):
                (out / directory).mkdir(parents=True, exist_ok=True)
            write_metadata(out, service)
            snapshot("before", out)
            # Real entrypoints initialize dependencies once; failures and their
            # full traceback are already captured by the fd-level tee.
            if service == "model":
                try:
                    feature_source(out)
                except Exception as exc:
                    print("Feature source diagnostics unavailable: " + str(exc), file=sys.stderr, flush=True)
            if service == "proxy" and (len(command) < 2 or not Path(command[1]).is_file()):
                raise RuntimeError("Official proxy is absent in selected image")
            if requested_signal[0] is not None:
                code = 128 + requested_signal[0]
            else:
                process = subprocess.Popen(command, start_new_session=True)
                if requested_signal[0] is not None:
                    forward(requested_signal[0], None)
                code = exit_code(process.wait())
        except Exception:
            print("Runtime preflight/launch failed:", file=sys.stderr, flush=True)
            traceback.print_exc()
            code = 1
        finally:
            try:
                write_json(out / "exit-status.json", {
                    "exit_code": code, "child_returncode": process.returncode if process is not None else None,
                    "received_signal": requested_signal[0], "finished_at": time.time(),
                    "logging_errors": capture.tee.log.errors,
                    "docker_mirror_bytes_dropped": capture.tee.mirror_bytes_dropped,
                    "capture_counts_scope": "Counts at child exit; final drainage may add output.",
                })
            except Exception as exc:
                print("Could not save exit status; original code={}: {}".format(code, exc), file=sys.stderr, flush=True)
            snapshot("after" if code == 0 else "failure", out)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return code


if __name__ == "__main__":
    sys.exit(main())
