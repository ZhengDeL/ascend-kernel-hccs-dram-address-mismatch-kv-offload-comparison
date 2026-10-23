#!/usr/bin/env python3
"""Copy this deployment's logs plus bounded Host evidence into a local bundle."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
from collect_server import execute

DEPLOYMENT = "glm53flash-1p1d-21-22"
LOGROOT = Path("/var/ai-model/lzd") / DEPLOYMENT
MAX_FILE = 64 * 1024 * 1024


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Bundle:
    def __init__(self, output, max_total=2 * 1024 ** 3, max_file=MAX_FILE):
        if max_total <= 0 or max_file <= 0:
            raise ValueError("Evidence byte limits must be positive")
        self.output = Path(output)
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(self.output, 0o700)
        self.max_total, self.max_file, self.used = max_total, max_file, 0
        self.files, self.commands, self.issues = [], [], []

    def json(self, relative, value):
        target = self.output / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(target, 0o600)

    def command(self, label, argv, timeout=15, limit=4 * 1024 * 1024):
        result, stdout, stderr = execute(argv, timeout, limit=limit)
        folder = self.output / "commands"
        folder.mkdir(mode=0o700, exist_ok=True)
        for name, data in (("stdout", stdout), ("stderr", stderr)):
            path = folder / (label + "." + name + ".txt")
            path.write_bytes(data)
            os.chmod(path, 0o600)
        self.commands.append(dict(label=label, **result))
        if result.get("status") != "ok" or any(result.get("truncated", {}).values()) or result.get("pipe_drain_incomplete"):
            self.issues.append({"component": label, "reason": "command failed or truncated", "command": result})
        return result, stdout

    def copy_tree(self, source, relative):
        source = Path(source)
        if source.is_symlink() or not source.is_dir():
            self.issues.append({"source": str(source), "reason": "source directory absent or symlink"})
            return
        # Preserve fixed first-error and initial context files before expendable rolls.
        paths = sorted(source.rglob("*"), key=lambda p: (not any(
            x in p.name.lower() for x in ("first", "error", "status", "plan", "effective")), str(p)))
        base = source.resolve()
        for path in paths:
            if path.is_symlink():
                self.issues.append({"source": str(path), "reason": "symlink skipped"})
                continue
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(base)
                before = resolved.stat()
                allowance = min(self.max_file, max(0, self.max_total - self.used))
                if not allowance:
                    self.issues.append({"source": str(path), "reason": "total byte cap reached"})
                    continue
                destination = self.output / relative / path.relative_to(source)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest = hashlib.sha256()
                copied = 0
                descriptor = os.open(str(resolved), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
                with os.fdopen(descriptor, "rb") as original, destination.open("xb") as saved:
                    opened = os.fstat(original.fileno())
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise OSError("Source identity changed before read")
                    while copied < allowance:
                        block = original.read(min(1024 * 1024, allowance - copied))
                        if not block:
                            break
                        saved.write(block)
                        digest.update(block)
                        copied += len(block)
                os.chmod(destination, 0o600)
                after = resolved.stat()
                truncated = copied < before.st_size or after.st_size > copied
                record = {"source": str(path), "path": destination.relative_to(self.output).as_posix(),
                          "bytes": copied, "sha256": digest.hexdigest(), "source_bytes_before": before.st_size,
                          "source_changed": (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns),
                          "truncated": truncated}
                self.files.append(record)
                self.used += copied
                if truncated:
                    self.issues.append({"source": str(path), "reason": "snapshot reached file or total limit / source grew"})
                elif record["source_changed"]:
                    self.issues.append({"source": str(path), "reason": "source changed during read; review byte snapshot"})
            except (OSError, ValueError) as error:
                self.issues.append({"source": str(path), "reason": str(error)})

    def finish(self):
        self.json("collection-report.json", {"completed_utc": utc_now(), "partial": bool(self.issues),
                  "copied_bytes": self.used, "max_total_bytes": self.max_total,
                  "budget_scope": "copied run/native-driver files; bounded commands, metrics and host-profile use separate limits",
                  "max_file_bytes": self.max_file, "files": self.files,
                  "commands": self.commands, "issues": self.issues,
                  "live_log_note": "Running sources can change; recorded byte snapshots are not atomic system snapshots."})
        manifest = []
        for path in sorted(self.output.rglob("*")):
            if path.is_file():
                manifest.append({"path": path.relative_to(self.output).as_posix(), "bytes": path.stat().st_size,
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.json("manifest.sha256.json", {"files": manifest, "excludes": ["manifest.sha256.json"]})
        archive = Path(str(self.output) + ".tar.gz")
        with archive.open("xb") as stream:
            os.chmod(archive, 0o600)
            with tarfile.open(fileobj=stream, mode="w:gz") as package:
                package.add(self.output, arcname=self.output.name, recursive=True)
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum = Path(str(archive) + ".sha256")
        checksum.write_text(digest.hexdigest() + "  " + archive.name + "\n", encoding="ascii")
        os.chmod(checksum, 0o600)
        return archive


def inspect_template():
    return ('{"id":{{json .Id}},"state":{{json .State}},"mounts":{{json .Mounts}},'
            '"image":{{json .Image}},"deployment":{{json (index .Config.Labels "deployment")}},'
            '"service":{{json (index .Config.Labels "service")}}}')


def run_plan(folder, logroot, node, service):
    """Only this deployment's direct run directories are eligible for copying."""
    folder = Path(folder)
    if folder.is_symlink() or folder.resolve().parent != (logroot / "runs").resolve():
        raise ValueError("run mount must be a direct non-symlink child of this deployment's runs")
    plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("run plan must be a JSON object")
    expected = "glm53flash-21-22-{}-{}".format(service, node)
    if plan.get("node") != node or plan.get("service") != service or plan.get("container") != expected:
        raise ValueError("run plan node/service/container does not match")
    if plan.get("deployment", DEPLOYMENT) != DEPLOYMENT:
        raise ValueError("run plan belongs to another deployment")
    return plan


def check_observer(bundle, folder):
    metadata = folder / "os/observer.json"
    try:
        observer = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(observer, dict):
            raise ValueError("observer metadata must be a JSON object")
        if not isinstance(observer.get("partial", []), list):
            raise ValueError("observer partial must be an array")
        for gap in observer.get("partial", []):
            bundle.issues.append({"component": "host-observer", "run": folder.name, "reason": gap})
        if observer.get("status") not in ("ready", "partial", "finished"):
            bundle.issues.append({"component": "host-observer", "run": folder.name, "reason": "observer initialization incomplete"})
        if observer.get("finish_reason") in ("observer_error", "state_unavailable", "observer_signal"):
            bundle.issues.append({"component": "host-observer", "run": folder.name, "reason": observer["finish_reason"]})
    except (OSError, ValueError, TypeError) as error:
        bundle.issues.append({"component": "host-observer", "run": folder.name, "reason": "observer metadata unavailable: " + str(error)})


def collect(node, service="all", logroot=LOGROOT, output=None, max_total=2 * 1024 ** 3):
    logroot = Path(logroot)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + str(time.time_ns())[-8:]
    output = Path(output) if output else logroot / "manual-evidence" / (stamp + "-" + str(node))
    bundle = Bundle(output, max_total=max_total)
    selected = ["model", "meta", "proxy"] if service == "all" and node == 21 else ["model"] if service == "all" else [service]
    containers = []
    for component in selected:
        name = "glm53flash-21-22-{}-{}".format(component, node)
        status, raw = bundle.command(component + "-identity", ["docker", "inspect", "--format", inspect_template(), name])
        try:
            if status["status"] != "ok":
                raise ValueError("container missing/uninspectable")
            identity = json.loads(raw)
            if identity.get("deployment") != DEPLOYMENT or identity.get("service") != component:
                raise ValueError("container labels differ; no container logs read")
            cid = identity["id"]
            if not re.fullmatch(r"[0-9a-f]{64}", cid):
                raise ValueError("invalid container ID")
            bundle.command(component + "-docker-stdout", ["docker", "logs", "--timestamps", "--tail", "20000", cid], timeout=30)
            if identity.get("state", {}).get("Running"):
                containers.append(cid)
            found = False
            for mount in identity.get("mounts", []):
                if mount.get("Destination") == "/run/glm53flash":
                    source = Path(mount["Source"])
                    run_plan(source, logroot, node, component)
                    check_observer(bundle, source)
                    bundle.copy_tree(source, Path("runs") / source.name)
                    found = True
            if not found:
                bundle.issues.append({"component": component, "reason": "no verified /run/glm53flash bind mount"})
        except (OSError, ValueError, KeyError, TypeError) as error:
            bundle.issues.append({"component": component, "reason": str(error)})
    # Retain earlier failed attempts even when their container has since been removed.
    already = {item["path"].split("/")[1] for item in bundle.files if item["path"].startswith("runs/")}
    for folder in sorted((logroot / "runs").glob("*")):
        if folder.name in already or folder.is_symlink() or not folder.is_dir():
            continue
        try:
            plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                bundle.issues.append({"source": str(folder), "reason": "invalid non-object run plan"})
                continue
            if plan.get("node") == node and plan.get("service") in selected:
                run_plan(folder, logroot, node, plan["service"])
                check_observer(bundle, folder)
                bundle.copy_tree(folder, Path("runs") / folder.name)
        except (OSError, ValueError, TypeError):
            continue
    driver_logs = Path("/var/log/npu")
    bundle.copy_tree(driver_logs, Path("host-native-npu"))
    bundle.json("native-log-coverage.json", {
        "driver_log_path": str(driver_logs), "driver_log_path_exists": driver_logs.is_dir(),
        "driver_daemon_logging_modified": False,
        "memfabric_ptracer": "Collect runs/*/native-memfabric if produced; absence does not prove trace enabled. Requires upstream ENABLE_PTRACER build.",
        "ascend_fallback": "Collect runs/*/native-ascend and per-DP plog/vllm_ascend; native file retention is component-specific."})
    argv = [sys.executable, str(ROOT / "tools/collect_server.py"), "--output", str(output / "host-profile")]
    for cid in containers:
        argv += ["--container", cid]
    result, _ = bundle.command("host-profile", argv, timeout=900)
    if result["status"] != "ok":
        bundle.issues.append({"component": "host-profile", "reason": result["status"]})
    for label, argv in [
            ("journal-system-boot", ["journalctl", "-b", "--utc", "--no-pager", "-n", "10000", "-o", "short-iso-precise"]),
            ("journal-kernel-boot", ["journalctl", "-k", "-b", "--utc", "--no-pager", "-n", "10000", "-o", "short-iso-precise"])]:
        result, _ = bundle.command(label, argv, timeout=30)
        if result["status"] != "ok":
            bundle.issues.append({"component": label, "reason": result["status"]})
    bundle.command("log-daemon-state", ["systemctl", "show", "systemd-journald.service", "rsyslog.service",
                   "--property=Id,LoadState,ActiveState,SubState"], timeout=15)
    ports = [9081, 9082] if node == 21 else list(range(9900, 9904))
    targets = [("memcache", "http://192.0.2.21:8000/metrics")]
    targets += [("vllm-" + str(p), "http://10.224.157.{}:{}/metrics".format(node, p)) for p in ports]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for label, url in targets:
        try:
            with opener.open(url, timeout=3) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            folder = output / "metrics"
            folder.mkdir(mode=0o700, exist_ok=True)
            target = folder / (label + ".txt")
            target.write_bytes(data[:4 * 1024 * 1024])
            os.chmod(target, 0o600)
            if len(data) > 4 * 1024 * 1024:
                bundle.issues.append({"component": label, "reason": "metrics truncated at 4MiB"})
        except (OSError, ValueError) as error:
            bundle.issues.append({"component": label, "reason": str(error)})
    return bundle.finish(), bool(bundle.issues)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=int, choices=[21, 22], required=True)
    parser.add_argument("--service", choices=["all", "meta", "model", "proxy"], default="all")
    parser.add_argument("--max-total-mib", type=int, default=2048)
    args = parser.parse_args()
    if args.node == 22 and args.service not in ("all", "model"):
        parser.error("node22 only has model")
    if sys.platform != "linux":
        parser.error("Live evidence collection requires Linux")
    if args.max_total_mib <= 0:
        parser.error("--max-total-mib must be positive")
    from deploy import local_nic
    local_nic(args.node, None)
    archive, partial = collect(args.node, args.service, max_total=args.max_total_mib * 1024 ** 2)
    print(str(archive) + (" (partial; review collection-report.json)" if partial else ""))
    sys.exit(2 if partial else 0)
