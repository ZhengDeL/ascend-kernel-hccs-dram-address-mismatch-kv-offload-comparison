#!/usr/bin/env python3
"""Bounded, byte-oriented logging without importing any accelerator packages."""
from collections import deque
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time

CHUNK_BYTES = 16384
LOG_BYTES = 20 * 1024 * 1024
LOG_BACKUPS = 5
FIRST_ERROR_BYTES = 128 * 1024
CONTEXT_BYTES = 16 * 1024
MIRROR_QUEUE_CHUNKS = 16
ERROR_PATTERN = re.compile(
    rb"(?i)(\b(?:error|fatal|exception|failed|failure|traceback)\b|address[^\r\n]{0,80}mismatch)")
LOGGING_ENV_KEYS = (
    "PYTHONUNBUFFERED", "VLLM_LOGGING_LEVEL", "VLLM_CONFIGURE_LOGGING",
    "VLLM_LOGGING_CONFIG_PATH", "VLLM_LOG_STATS_INTERVAL", "VLLM_LOGGING_STREAM",
    "ASCEND_GLOBAL_LOG_LEVEL", "ASCEND_SLOG_PRINT_TO_STDOUT", "ASCEND_PROCESS_LOG_PATH",
    "ASCEND_GLOBAL_EVENT_ENABLE", "HCCL_DEBUG_LEVEL", "HCCL_DEBUG_CONFIG", "HCCL_ENTRY_LOG_ENABLE",
    "MF_LOG_LEVEL", "ASCEND_MF_LOG_LEVEL", "MC_LOG_LEVEL", "MC_LOG_DIR",
    "MMC_META_CONFIG_PATH", "MMC_LOCAL_CONFIG_PATH",
)


def exit_code(code):
    return 128 - code if code < 0 else code


def write_json(path, data):
    """A failed diagnostic write must never replace a service's exit status."""
    try:
        path = Path(path)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)
        return True
    except (OSError, ValueError, TypeError) as exc:
        try:
            print("Could not save diagnostic {}: {}".format(path, exc), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass
        return False


def write_metadata(folder, service, env=None, extra=None):
    env = os.environ if env is None else env
    versions = {}
    for package in ("vllm", "vllm-ascend", "torch", "torch-npu", "memcache-hybrid",
                    "memfabric-hybrid", "mooncake-transfer-engine", "aiohttp", "fastapi", "uvicorn"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
        except Exception as exc:
            versions[package] = "metadata-unavailable: " + type(exc).__name__
    write_json(Path(folder) / "versions.json", versions)
    write_json(Path(folder) / "logging-settings.json", {
        "service": service, "environment": {key: env[key] for key in LOGGING_ENV_KEYS if key in env},
        "capture": {"file_bytes": LOG_BYTES, "backup_files": LOG_BACKUPS,
                    "first_error_bytes": FIRST_ERROR_BYTES, "read_chunk_bytes": CHUNK_BYTES,
                    "docker_mirror_queue_chunks_per_stream": MIRROR_QUEUE_CHUNKS,
                    "docker_mirror_backpressure": "Drop mirror copies and count bytes; retain primary file capture."},
        "details": extra or {}, "written_at": time.time(),
        "scope": "Allowlisted logging settings only; no full environment or request/prompt logging.",
    })


class RotatingBinaryLog:
    """Rotate byte streams and retain a bounded first-error window independently.

    No line buffering is used: a multi-gigabyte line has the same memory bound
    as ordinary output. A broken filesystem disables its sink, never its reader.
    """
    def __init__(self, path, max_bytes=LOG_BYTES, backups=LOG_BACKUPS,
                 first_error_bytes=FIRST_ERROR_BYTES, context_bytes=CONTEXT_BYTES,
                 warning=None):
        if min(max_bytes, first_error_bytes, context_bytes) < 1 or backups < 0:
            raise ValueError("Log size limits must be positive and backups nonnegative")
        self.path = Path(path)
        self.first_error_path = self.path.with_name("first-error.log")
        self.max_bytes, self.backups = max_bytes, backups
        self.first_error_bytes, self.context_bytes = first_error_bytes, context_bytes
        self.warning = warning or (lambda message: None)
        self._lock = threading.Lock()
        self._context = deque()
        self._context_size = 0
        self._scan_tail = b""
        self._first = None
        self._first_size = 0
        self._first_started = self.first_error_path.exists()
        self._file = None
        self._size = 0
        self._warned = set()
        self.bytes_seen = 0
        self.errors = []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("ab", buffering=0)
            self._size = self.path.stat().st_size
        except OSError as exc:
            self._failed("rotating-file", exc)

    def _failed(self, target, exc):
        if target not in self._warned:
            self._warned.add(target)
            message = "Logging {} failed for {}: {}".format(target, self.path, exc)
            self.errors.append(message)
            try:
                self.warning(message)
            except Exception:
                pass

    def _rotate(self):
        self._file.close()
        self._file = None
        if self.backups:
            for number in range(self.backups, 0, -1):
                source = self.path if number == 1 else self.path.with_name(self.path.name + "." + str(number - 1))
                target = self.path.with_name(self.path.name + "." + str(number))
                if source.exists():
                    source.replace(target)
        else:
            self.path.unlink(missing_ok=True)
        self._file = self.path.open("wb", buffering=0)
        self._size = 0

    def _preserve_error(self, data):
        if not self._first_started and ERROR_PATTERN.search(self._scan_tail + data):
            self._first_started = True
            try:
                self._first = self.first_error_path.open("xb", buffering=0)
                context = b"".join(self._context)
                if context:
                    self._append_error(context)
            except OSError as exc:
                self._failed("first-error", exc)
        if self._first is not None:
            self._append_error(data)
        self._scan_tail = (self._scan_tail + data)[-512:]
        self._context.append(data)
        self._context_size += len(data)
        while self._context_size > self.context_bytes:
            oldest = self._context.popleft()
            excess = self._context_size - self.context_bytes
            if len(oldest) > excess:
                self._context.appendleft(oldest[excess:])
                self._context_size -= excess
            else:
                self._context_size -= len(oldest)

    def _append_error(self, data):
        try:
            remaining = self.first_error_bytes - self._first_size
            part = data[:remaining]
            _write_file_all(self._first, part)
            self._first_size += len(part)
            if self._first_size >= self.first_error_bytes:
                self._first.close()
                self._first = None
        except OSError as exc:
            self._failed("first-error", exc)
            try:
                self._first.close()
            except OSError:
                pass
            self._first = None

    def write(self, data):
        # Bound memory even when a caller supplies a large buffer directly.
        with self._lock:
            for start in range(0, len(data), CHUNK_BYTES):
                block = data[start:start + CHUNK_BYTES]
                self.bytes_seen += len(block)
                self._preserve_error(block)
                if self._file is None:
                    continue
                try:
                    offset = 0
                    while offset < len(block):
                        if self._size >= self.max_bytes:
                            self._rotate()
                        part = block[offset:offset + self.max_bytes - self._size]
                        _write_file_all(self._file, part)
                        self._size += len(part)
                        offset += len(part)
                except OSError as exc:
                    self._failed("rotating-file", exc)
                    if self._file is not None:
                        try:
                            self._file.close()
                        except OSError:
                            pass
                    self._file = None

    def close(self):
        with self._lock:
            for stream in (self._file, self._first):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError as exc:
                        self._failed("close", exc)
            self._file = self._first = None


def _write_file_all(stream, data):
    view = memoryview(data)
    while view:
        try:
            written = stream.write(view)
        except InterruptedError:
            continue
        if written is None or written <= 0:
            raise OSError("Log file accepted no output bytes")
        view = view[written:]


def _write_all(fd, data):
    view = memoryview(data)
    total = 0
    while view:
        try:
            written = os.write(fd, view)
            if written <= 0:
                return total
            total += written
            view = view[written:]
        except InterruptedError:
            continue
        except OSError:
            return total
    return total


class MirrorOutput:
    """A full Docker stdout pipe must not stop the primary log reader or model.

    Only the mirror copy can be dropped under backpressure; the bounded primary
    log and independent first-error file are written before enqueueing it.
    """
    def __init__(self, fd):
        self.fd = os.dup(fd)
        self.queue = queue.Queue(maxsize=MIRROR_QUEUE_CHUNKS)
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.bytes_dropped = 0
        self._counter_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _drop(self, size):
        with self._counter_lock:
            self.bytes_dropped += size

    def offer(self, block):
        if self.failed.is_set() or self.stop.is_set():
            self._drop(len(block))
            return
        try:
            self.queue.put_nowait(block)
        except queue.Full:
            self._drop(len(block))

    def _run(self):
        try:
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    block = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self.failed.is_set():
                    self._drop(len(block))
                else:
                    written = _write_all(self.fd, block)
                    if written != len(block):
                        self._drop(len(block) - written)
                        self.failed.set()
        finally:
            os.close(self.fd)

    def close(self, timeout):
        self.stop.set()
        self.thread.join(timeout=max(0, timeout))
        return not self.thread.is_alive()


class StreamTee:
    """Drain child stdout/stderr concurrently to one file and original streams."""
    def __init__(self, streams, path, mirrors=(1, 2), **limits):
        self.mirrors = tuple(MirrorOutput(fd) for fd in mirrors)
        self.log = RotatingBinaryLog(path, warning=self._warning, **limits)
        self.threads = []
        for stream, mirror in zip(streams, self.mirrors):
            thread = threading.Thread(target=self._drain, args=(stream, mirror), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _warning(self, message):
        self.mirrors[-1].offer((message + "\n").encode("utf-8", "replace"))

    def _drain(self, stream, mirror):
        try:
            while True:
                block = os.read(stream.fileno(), CHUNK_BYTES)
                if not block:
                    break
                self.log.write(block)
                mirror.offer(block)
        except (OSError, ValueError) as exc:
            self._warning("Log stream read failed: " + str(exc))
        finally:
            stream.close()

    def close(self, timeout=5):
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            self._warning("Log drain timed out: a descendant still holds stdout/stderr open")
            # Daemon readers retain ownership until process exit; closing their
            # descriptors concurrently risks corrupting unrelated reused fds.
            return
        self.log.close()
        for mirror in self.mirrors:
            if not mirror.close(deadline - time.monotonic()):
                self.log._failed("mirror-timeout", OSError("Docker output remains blocked; primary file was drained"))

    @property
    def mirror_bytes_dropped(self):
        return sum(mirror.bytes_dropped for mirror in self.mirrors)


class CaptureOwnOutput:
    """Capture Python, native, and inherited child output from the first preflight."""
    def __init__(self, path, **limits):
        self.path, self.limits = path, limits
        self.saved = []
        self.tee = None

    def __enter__(self):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        pairs = []
        streams = []
        writes = []
        try:
            for destination in (1, 2):
                self.saved.append(os.dup(destination))
            for _ in (1, 2):
                read, write = os.pipe()
                pairs.append((read, write))
                writes.append(write)
                streams.append(os.fdopen(read, "rb", buffering=0))
            self.tee = StreamTee(streams, self.path, mirrors=self.saved, **self.limits)
            for destination, (_, write) in zip((1, 2), pairs):
                os.dup2(write, destination)
                os.close(write)
                writes.remove(write)
        except Exception:
            for destination, saved in zip((1, 2), self.saved):
                os.dup2(saved, destination)
                os.close(saved)
            for fd in writes:
                os.close(fd)
            if self.tee is not None:
                self.tee.close()
            else:
                for stream in streams:
                    stream.close()
            raise
        return self

    def __exit__(self, *_):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        for destination, saved in zip((1, 2), self.saved):
            try:
                os.dup2(saved, destination)
                os.close(saved)
            except OSError as exc:
                self.tee.log._failed("restore-stream", exc)
        try:
            self.tee.close()
        except Exception as exc:
            # Diagnostics are never allowed to replace the business exit code.
            self.tee.log._failed("close-tee", exc)
