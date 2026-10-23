#!/usr/bin/env python3
"""Read-only, container-scoped host observer; Python 3.8+, no third party modules.

Detached lifecycle follows a full CID with this deployment's labels. The only
processes it can signal are diagnostic children created in its own sessions.
It never changes journald, sysctl, kernel or Docker configuration, opens model
request bodies, or reads process environ/cmdline.
"""
import argparse
import datetime
import glob
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import threading
import time

DEPLOYMENT = "glm53flash-1p1d-21-22"
SERVICES = ("meta", "model", "proxy")
MIB = 1024 * 1024
INSPECT_FORMAT = ('{"id":{{json .Id}},"image":{{json .Image}},'
                  '"deployment":{{json (index .Config.Labels "deployment")}},'
                  '"service":{{json (index .Config.Labels "service")}},'
                  '"state":{{json .State}}}')
EVENT_FORMAT = ('{"id":{{json .Actor.ID}},"type":{{json .Type}},'
                '"action":{{json .Action}},"time":{{json .Time}},'
                '"timeNano":{{json .TimeNano}}}')


def clock():
    return {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "monotonic_seconds": time.monotonic()}


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(temporary), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


class RotatingLog:
    """Bound recent output and preserve the first segment outside the ring."""
    def __init__(self, path, max_bytes=10 * MIB, backups=5, first_bytes=2 * MIB):
        self.path = Path(path)
        self.max_bytes, self.backups, self.first_bytes = max_bytes, backups, first_bytes
        self.first_path = self.path.with_name(self.path.name + ".first")
        self.lock = threading.Lock()
        self.size = self.path.stat().st_size if self.path.exists() else 0
        self.first_size = self.first_path.stat().st_size if self.first_path.exists() else 0

    @staticmethod
    def _append(path, data):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(data)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        with self.lock:
            remaining = max(0, self.first_bytes - self.first_size)
            if remaining:
                prefix = data[:remaining]
                self._append(self.first_path, prefix)
                self.first_size += len(prefix)
            while data:
                if self.size >= self.max_bytes:
                    last = self.path.with_name(self.path.name + "." + str(self.backups))
                    if last.exists():
                        last.unlink()
                    for index in range(self.backups - 1, 0, -1):
                        old = self.path.with_name(self.path.name + "." + str(index))
                        if old.exists():
                            os.replace(str(old), str(old.with_name(self.path.name + "." + str(index + 1))))
                    if self.path.exists():
                        os.replace(str(self.path), str(self.path.with_name(self.path.name + ".1")))
                    self.size = 0
                chunk = data[:self.max_bytes - self.size]
                self._append(self.path, chunk)
                self.size += len(chunk)
                data = data[len(chunk):]

    def record(self, value):
        self.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def terminate_owned(process):
    """Only signal a diagnostic subprocess created by this observer."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=3)
    except ProcessLookupError:
        pass


def execute(argv, timeout=10, limit=2 * MIB, stop=None):
    """Drain both pipes, retain bounded bytes, record truncation and timeouts."""
    started = clock()
    info = {"argv": argv, "started": started, "timeout_seconds": timeout}
    output = {"stdout": bytearray(), "stderr": bytearray()}
    total = {"stdout": 0, "stderr": 0}
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=os.name == "posix")
    except OSError as exc:
        info.update(status="missing_command" if isinstance(exc, FileNotFoundError) else "start_error",
                    returncode=None, finished=clock(), error=str(exc), truncated={})
        return info, b"", b""

    def drain(name):
        pipe = getattr(process, name)
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                total[name] += len(chunk)
                remaining = max(0, limit - len(output[name]))
                output[name].extend(chunk[:remaining])
        finally:
            pipe.close()

    workers = [threading.Thread(target=drain, args=(name,), daemon=True) for name in output]
    for worker in workers:
        worker.start()
    status = "completed"
    while process.poll() is None:
        if stop is not None and stop.is_set():
            status = "observer_stopping"
            terminate_owned(process)
            break
        if time.monotonic() - started["monotonic_seconds"] >= timeout:
            status = "timeout"
            terminate_owned(process)
            break
        time.sleep(0.05)
    for worker in workers:
        worker.join(timeout=2)
    info.update(status=status, returncode=process.returncode, finished=clock(), bytes_seen=total,
                truncated={name: total[name] > len(output[name]) for name in output},
                pipe_drain_complete=not any(worker.is_alive() for worker in workers))
    return info, bytes(output["stdout"]), bytes(output["stderr"])


def process_birth(pid, proc=Path("/proc")):
    """Protect against PID reuse using boot ID and Linux process start ticks."""
    text = (proc / str(pid) / "stat").read_text(encoding="ascii")
    # comm may contain spaces and parentheses; fields following ')' start at 3.
    start_ticks = text[text.rfind(")") + 2:].split()[19]
    boot_id = (proc / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    return {"pid": pid, "start_ticks": start_ticks, "boot_id": boot_id}


class ObserverLock:
    def __init__(self, directory, cid):
        self.path = Path(directory) / "observer.lock"
        self.identity = process_birth(os.getpid())
        self.value = dict(self.identity, container=cid, acquired=clock())
        self.owned = False

    def acquire(self):
        for _attempt in range(2):
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(str(self.path), flags, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(self.value, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.owned = True
                return
            except FileExistsError:
                if self.path.is_symlink():
                    raise RuntimeError("Refusing symlink observer.lock")
                try:
                    previous = json.loads(self.path.read_text(encoding="utf-8"))
                    previous_birth = {key: previous[key] for key in ("pid", "start_ticks", "boot_id")}
                except (OSError, ValueError, KeyError):
                    raise RuntimeError("Cannot safely verify existing observer.lock; refusing duplicate")
                try:
                    current = process_birth(int(previous["pid"]))
                except FileNotFoundError:
                    current = None
                except (OSError, ValueError, IndexError):
                    raise RuntimeError("Cannot safely verify existing observer PID; refusing duplicate")
                if current == previous_birth:
                    raise RuntimeError("Observer already running; PID " + str(previous["pid"]))
                # A fresh per-run directory is normal. Never race another process
                # by unlinking a stale lock which it may just have replaced.
                raise RuntimeError("Stale observer.lock retained; review prior metadata and use a fresh output directory")
        raise RuntimeError("Concurrent lock acquisition; refusing duplicate")

    def release(self):
        if self.owned:
            try:
                if json.loads(self.path.read_text(encoding="utf-8")) == self.value:
                    self.path.unlink()
            except (FileNotFoundError, ValueError):
                pass


def inspect_container(cid, service=None):
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("--container requires a full lowercase 64-character Docker CID")
    info, stdout, stderr = execute(["docker", "inspect", "--format", INSPECT_FORMAT, cid], limit=128 * 1024)
    if info.get("returncode") != 0:
        raise RuntimeError("Scoped container inspect failed: " + json.dumps(info) + " " + stderr.decode("utf-8", "replace"))
    value = json.loads(stdout.decode("utf-8"))
    if value.get("id") != cid or value.get("deployment") != DEPLOYMENT or value.get("service") not in SERVICES:
        raise RuntimeError("CID/deployment/service labels do not belong to this deployment")
    if service is not None and value["service"] != service:
        raise RuntimeError("Container service differs from --service")
    return value


class Follow:
    def __init__(self, name, argv, directory, events):
        self.name, self.argv, self.events = name, argv, events
        self.process = None
        self.workers = []
        self.totals = {"stdout": 0, "stderr": 0}
        self.finished = False
        self.errors = []
        try:
            self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, start_new_session=os.name == "posix")
        except OSError as exc:
            events.record(dict(clock(), event="follow_start_failed", name=name, argv=argv, error=str(exc)))
            return
        try:
            events.record(dict(clock(), event="follow_started", name=name, argv=argv, child_pid=self.process.pid))
        except Exception:
            terminate_owned(self.process)
            self.process.stdout.close()
            self.process.stderr.close()
            raise
        for channel in ("stdout", "stderr"):
            log = RotatingLog(directory / (name + "." + channel + ".log"))
            worker = threading.Thread(target=self._drain, args=(channel, log), daemon=True)
            worker.start()
            self.workers.append(worker)

    def _drain(self, channel, log):
        pipe = getattr(self.process, channel)
        try:
            while True:
                # read1 emits sparse kernel lines without waiting for 64KiB.
                chunk = pipe.read1(65536)
                if not chunk:
                    break
                self.totals[channel] += len(chunk)
                log.write(chunk)
        except Exception as exc:
            self.errors.append(str(exc))
        finally:
            pipe.close()

    def alive(self):
        return self.process is not None and self.process.poll() is None and not self.errors

    def close(self):
        if self.finished:
            return
        self.finished = True
        if self.process is not None:
            terminate_owned(self.process)
            for worker in self.workers:
                worker.join(timeout=2)
            self.events.record(dict(clock(), event="follow_finished", name=self.name, argv=self.argv,
                                    returncode=self.process.returncode, bytes_seen=self.totals,
                                    errors=self.errors,
                                    pipe_drain_complete=not any(worker.is_alive() for worker in self.workers)))


def parse_since(value):
    if value is None:
        return time.time()
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--since-utc must include a timezone, preferably Z or +00:00")
    return parsed.timestamp()


class HostObserver:
    def __init__(self, cid, output, service=None, interval=30, npu_interval=60, since_utc=None):
        self.cid, self.output, self.service = cid, Path(output), service
        self.interval, self.npu_interval = interval, npu_interval
        self.stop = threading.Event()
        self.followers = []
        self.partial = set()
        self.state = None
        self.events = RotatingLog(self.output / "observer-events.jsonl")
        self.snapshots = RotatingLog(self.output / "host-snapshots.jsonl")
        self.lock = None
        self.started_epoch = parse_since(since_utc)
        self.metadata = {"schema": 1, "container": cid, "deployment": DEPLOYMENT,
                         "started": clock(), "status": "starting", "coverage": {},
                         "read_only": True, "rotation_bytes": 10 * MIB, "rotation_backups": 5,
                         "first_segment_bytes_per_stream": 2 * MIB, "since_epoch": self.started_epoch,
                         "snapshot_interval_seconds": interval, "npu_interval_seconds": npu_interval}

    def save_metadata(self, status):
        self.metadata.update(status=status, updated=clock(), partial=sorted(self.partial))
        atomic_json(self.output / "observer.json", self.metadata)

    def command_snapshot(self, name, argv, limit=2 * MIB):
        info, stdout, stderr = execute(argv, limit=limit, stop=self.stop)
        self.events.record(dict(clock(), event="command_snapshot", name=name, command=info))
        log = RotatingLog(self.output / (name + ".log"))
        log.record({"snapshot": clock(), "command": info})
        log.write(b"\n--- stdout ---\n" + stdout + b"\n--- stderr ---\n" + stderr + b"\n")
        if info.get("returncode") != 0 or info.get("status") != "completed":
            self.partial.add(name + ": " + str(info.get("status")) + "/" + str(info.get("returncode")))
        if any(info.get("truncated", {}).values()):
            self.partial.add(name + ": bounded snapshot truncated; see command metadata")
        return info, stdout, stderr

    def add_follow(self, name, argv):
        follower = Follow(name, argv, self.output, self.events)
        self.followers.append(follower)
        if not follower.alive():
            self.partial.add(name + ": follow unavailable")
        return follower

    def start_kernel(self):
        # All current-boot kernel history is requested, with an explicit byte cap.
        info, stdout, _ = self.command_snapshot("journal-kernel-boot", ["journalctl", "-k", "-b", "--no-pager", "-o", "short-monotonic"], 4 * MIB)
        journal_ok = info.get("returncode") == 0 and b"-- No entries --" not in stdout and bool(stdout.strip())
        self.command_snapshot("journal-kernel-tail", ["journalctl", "-k", "-b", "--no-pager", "-n", "1000", "-o", "short-monotonic"])
        self.command_snapshot("dmesg-boot", ["dmesg", "--color=never"], 4 * MIB)
        if journal_ok:
            self.metadata["coverage"]["kernel"] = "current boot journal snapshot + follow from deployment start"
            return self.add_follow("journal-kernel", ["journalctl", "-k", "-b", "--follow", "--no-pager", "-o", "short-monotonic", "--since", "@" + str(int(self.started_epoch))])
        return self.start_dmesg()

    def start_dmesg(self):
        self.partial.add("kernel: journal unavailable; dmesg ring may omit older boot messages")
        self.metadata["coverage"]["kernel"] = "dmesg snapshot + follow fallback; partial boot history"
        return self.add_follow("dmesg-follow", ["dmesg", "--follow", "--color=never"])

    def read_file(self, path, limit=128 * 1024):
        try:
            with open(str(path), "rb") as stream:
                data = stream.read(limit + 1)
            return {"path": str(path), "data": data[:limit].decode("utf-8", "replace"),
                    "truncated": len(data) > limit}
        except OSError as exc:
            return {"path": str(path), "error": str(exc)}

    def snapshot(self, phase):
        data = dict(clock(), phase=phase, container=self.cid, state=self.state)
        paths = ["/proc/meminfo", "/proc/vmstat", "/proc/buddyinfo", "/proc/pressure/memory",
                 "/proc/pressure/cpu", "/proc/pressure/io"]
        for pattern in ("/sys/devices/system/node/node[0-9]*/meminfo", "/sys/devices/system/node/node[0-9]*/numastat"):
            paths.extend(sorted(glob.glob(pattern))[:128])
        if phase != "periodic":
            paths.extend(["/etc/os-release", "/proc/version", "/proc/iomem", "/proc/zoneinfo",
                          "/proc/sys/kernel/numa_balancing", "/proc/sys/vm/zone_reclaim_mode",
                          "/sys/kernel/mm/transparent_hugepage/enabled"])
        data["files"] = [self.read_file(path) for path in paths]
        info, output, _ = execute(["docker", "top", self.cid, "-eo", "pid"], limit=64 * 1024, stop=self.stop)
        data["docker_top"] = info
        pids = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
        data["process_count"] = len(pids)
        data["processes_truncated"] = len(pids) > 256
        data["process_files"] = []
        budget = 4 * MIB
        for pid in pids[:256]:
            names = ["status", "statm", "io", "schedstat", "limits", "cgroup"]
            if phase != "periodic":
                names.extend(["numa_maps", "smaps_rollup"])
            for name in names:
                if budget <= 0:
                    break
                item = self.read_file(Path("/proc") / str(pid) / name, min(128 * 1024, budget))
                budget -= len(item.get("data", "").encode("utf-8"))
                data["process_files"].append(item)
            if budget <= 0:
                data["processes_truncated"] = True
                break
        self.snapshots.record(data)
        if info.get("returncode") != 0:
            self.partial.add("docker top: process metrics incomplete; container may have exited")

    def system_logs(self):
        command = ["journalctl", "-b", "--no-pager", "-o", "short-monotonic"]
        info, output, _ = self.command_snapshot("journal-system-boot", command, 4 * MIB)
        if info.get("returncode") != 0 or not output.strip() or b"-- No entries --" in output:
            self.partial.add("system journal unavailable or empty; check journal-system logs")
        self.command_snapshot("journal-system-tail", command + ["-n", "2000"])
        self.add_follow("journal-system", command + ["--follow", "--since", "@" + str(int(self.started_epoch))])
        found = []
        for path in ("/var/log/messages", "/var/log/kern.log", "/var/log/syslog"):
            if Path(path).is_file():
                found.append(path)
                name = "os-file-" + Path(path).name.replace(".", "-")
                self.command_snapshot(name, ["tail", "-n", "2000", path])
                self.add_follow(name + "-follow", ["tail", "-F", "-n", "0", path])
        self.metadata["coverage"]["os_files"] = found
        self.metadata["coverage"]["system_journal"] = "current boot bounded snapshots + continuous follow"

    def run(self, once=False):
        if platform.system() != "Linux":
            raise RuntimeError("Host observer requires Linux; no Linux/driver acceptance on this host")
        if self.output.is_symlink():
            raise RuntimeError("Refusing symlink output directory")
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.output), 0o700)
        initial = inspect_container(self.cid, self.service)
        self.state, self.service = initial["state"], initial["service"]
        self.lock = ObserverLock(self.output, self.cid)
        self.lock.acquire()
        self.metadata.update(observer_identity=self.lock.identity, identity=initial, once=once)
        original_handlers = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            original_handlers[signum] = signal.signal(signum, lambda _sig, _frame: self.stop.set())
        reason = "once"
        try:
            # Lower only this observer's CPU scheduling priority; children inherit
            # it. This is no substitute for an actual <=5% performance A/B test.
            try:
                os.nice(10)
                self.metadata["observer_nice_increment"] = 10
            except (AttributeError, OSError):
                self.metadata["observer_nice_increment"] = None
            self.save_metadata("initializing")
            atomic_json(self.output / "observer-registered.json", dict(clock(), observer_identity=self.lock.identity,
                         container=self.cid, status="initializing", note="Process registered; coverage not yet ready"))
            kernel = self.start_kernel()
            self.system_logs()
            self.add_follow("docker-events", ["docker", "events", "--filter", "container=" + self.cid,
                                               "--since", str(int(self.started_epoch)), "--format", EVENT_FORMAT])
            self.snapshot("initial")
            self.command_snapshot("npu-smi", ["npu-smi", "info"], 512 * 1024)
            for follower in self.followers:
                if not follower.alive():
                    self.partial.add(follower.name + ": follow stopped during initialization")
            self.save_metadata("partial" if self.partial else "ready")
            atomic_json(self.output / "observer-ready.json", dict(clock(), container=self.cid,
                         status="partial" if self.partial else "ready", partial=sorted(self.partial)))
            next_snapshot = time.monotonic() + self.interval
            next_npu = time.monotonic() + self.npu_interval
            next_state = time.monotonic() + 2
            failures = 0
            while not once and not self.stop.wait(0.2):
                now = time.monotonic()
                partial_before = set(self.partial)
                if not kernel.alive() and kernel.name == "journal-kernel":
                    kernel.close()
                    kernel = self.start_dmesg()
                    self.save_metadata("partial")
                for follower in self.followers:
                    if not follower.alive():
                        self.partial.add(follower.name + ": follow stopped; see observer-events and stderr")
                        follower.close()
                if self.partial != partial_before:
                    self.save_metadata("partial")
                if now >= next_state:
                    try:
                        inspected = inspect_container(self.cid, self.service)
                        self.state = inspected["state"]
                        failures = 0
                        if self.state.get("Status") in ("exited", "dead"):
                            reason = "container_" + self.state["Status"]
                            break
                    except Exception as exc:
                        failures += 1
                        self.events.record(dict(clock(), event="inspect_failed", consecutive_failures=failures, error=str(exc)))
                        if failures >= 3:
                            self.partial.add("state unavailable after 3 checks; observer stopped without touching workload")
                            reason = "state_unavailable"
                            break
                    next_state = time.monotonic() + 5
                if now >= next_snapshot:
                    self.snapshot("periodic")
                    self.save_metadata("partial" if self.partial else "ready")
                    next_snapshot = time.monotonic() + self.interval
                if now >= next_npu:
                    self.command_snapshot("npu-smi", ["npu-smi", "info"], 512 * 1024)
                    next_npu = time.monotonic() + self.npu_interval
            if self.stop.is_set():
                reason = "observer_signal"
            self.snapshot("final")
            self.command_snapshot("journal-kernel-final", ["journalctl", "-k", "-b", "--no-pager", "-n", "2000", "-o", "short-monotonic"])
        except Exception as exc:
            reason = "observer_error"
            self.partial.add("observer exception: " + str(exc))
            try:
                self.events.record(dict(clock(), event="observer_exception", error=str(exc)))
            except OSError:
                pass
        finally:
            for follower in self.followers:
                try:
                    follower.close()
                except OSError:
                    # Disk/log failures must not leave our diagnostic children.
                    self.partial.add(follower.name + ": unable to persist follow completion")
            self.metadata.update(finished=clock(), finish_reason=reason, last_container_state=self.state)
            try:
                self.save_metadata("finished")
            finally:
                self.lock.release()
                for signum, handler in original_handlers.items():
                    signal.signal(signum, handler)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, help="full CID belonging to this deployment")
    parser.add_argument("--output", required=True, type=Path, help="run-specific host directory, mode 0700")
    parser.add_argument("--service", choices=SERVICES, help="optional expected service identity check")
    parser.add_argument("--since-utc", help="ISO 8601 UTC deployment start, e.g. 2026-10-23T10:00:00Z")
    parser.add_argument("--once", action="store_true", help="bounded initial/final snapshots, then exit")
    args = parser.parse_args(argv)
    try:
        return HostObserver(args.container, args.output, args.service, since_utc=args.since_utc).run(args.once)
    except Exception as exc:
        print("Host logging unavailable (workload untouched): " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
