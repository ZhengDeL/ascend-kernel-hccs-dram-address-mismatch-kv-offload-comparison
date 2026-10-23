#!/usr/bin/env python3
"""Offline regression tests. Signal/process-group tests require real Linux."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from launch_online_dp import prepare_local_config
from logging_utils import RotatingBinaryLog, StreamTee, _write_file_all, write_metadata

ROOT = Path(__file__).resolve().parent
LINUX = sys.platform.startswith("linux")


class BoundedLoggingTests(unittest.TestCase):
    def test_short_writes_are_retried_without_losing_bytes(self):
        output = bytearray()

        class ShortWriter:
            def write(self, view):
                output.extend(view[:3])
                return len(view[:3])

        _write_file_all(ShortWriter(), b"all native log bytes")
        self.assertEqual(bytes(output), b"all native log bytes")

    def test_own_fd_and_preflight_traceback_are_captured(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stdout.log"
            expression = (
                "import sys,os,traceback;sys.path.insert(0,{root!r});from logging_utils import CaptureOwnOutput\n"
                "with CaptureOwnOutput({path!r}):\n"
                " os.write(1,b'native-out\\n');os.write(2,b'native-err\\n')\n"
                " try:\n  import no_such_preflight_module\n"
                " except Exception:\n  traceback.print_exc()\n"
            ).format(root=str(ROOT), path=str(path))
            result = subprocess.run([sys.executable, "-u", "-c", expression], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = path.read_bytes()
            self.assertIn(b"native-out", output)
            self.assertIn(b"native-err", output)
            self.assertIn(b"ModuleNotFoundError", output)
            self.assertIn(b"ModuleNotFoundError", (path.parent / "first-error.log").read_bytes())
            self.assertIn(b"native-out", result.stdout)
            self.assertIn(b"native-err", result.stderr)

    def test_blocked_docker_mirror_does_not_block_primary_capture_or_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stdout.log"
            read, write = os.pipe()
            proc = None
            tee = None
            drainer = None
            try:
                proc = subprocess.Popen([sys.executable, "-c", "import os;os.write(1,b'x'*2097152);raise SystemExit(19)"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                tee = StreamTee((proc.stdout, proc.stderr), path, mirrors=(write, write), max_bytes=4*1024*1024)
                self.assertEqual(proc.wait(timeout=5), 19)
                start = time.monotonic()
                tee.close(timeout=0.3)
                self.assertLess(time.monotonic() - start, 1)
                self.assertEqual(path.stat().st_size, 2097152)
                self.assertGreater(tee.mirror_bytes_dropped, 0)
            finally:
                os.close(write)

                def drain_pipe():
                    try:
                        while os.read(read, 16384):
                            pass
                    finally:
                        os.close(read)

                drainer = threading.Thread(target=drain_pipe, daemon=True)
                drainer.start()
                if tee is not None:
                    for mirror in tee.mirrors:
                        mirror.close(5)
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()
                drainer.join(timeout=5)
                self.assertFalse(drainer.is_alive())

    def test_rotation_retains_first_traceback_and_bounds_long_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stdout.log"
            log = RotatingBinaryLog(path, max_bytes=256, backups=2,
                                    first_error_bytes=1024, context_bytes=64)
            log.write(b"last healthy message\n")
            log.write(b"Trace")
            log.write(b"back (most recent call last):\n  File 'worker.py', line 4\nValueError: first failure\n")
            log.write(b"x" * 70000)  # A line larger than every configured log.
            log.write(b"ERROR: a later error must not replace the first\n")
            log.close()
            retained = (path.parent / "first-error.log").read_bytes()
            self.assertIn(b"last healthy message", retained)
            self.assertIn(b"Traceback (most recent call last)", retained)
            self.assertIn(b"ValueError: first failure", retained)
            self.assertNotIn(b"a later error", retained)
            self.assertLessEqual(len(retained), 1024)
            files = list(path.parent.glob("stdout.log*"))
            self.assertEqual(len(files), 3)
            self.assertTrue(all(file.stat().st_size <= 256 for file in files))
            self.assertLessEqual(log._context_size, 64)
            # Reopening a log never overwrites its preserved first failure.
            reopened = RotatingBinaryLog(path, max_bytes=256, backups=2)
            reopened.write(b"ERROR: later run\n")
            reopened.close()
            self.assertEqual(retained, (path.parent / "first-error.log").read_bytes())

    def test_both_streams_large_binary_output_and_nonzero_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            command = [sys.executable, "-c", (
                "import os,sys; "
                "os.write(1,b'out-start\\n'+b'x'*120000+b'\\xff\\nout-end\\n'); "
                "os.write(2,b'ERROR: stderr-marker\\n'+b'y'*120000+b'\\nstderr-end\\n'); "
                "sys.exit(23)")]
            with (folder / "mirrored-out.bin").open("wb") as stdout, (folder / "mirrored-err.bin").open("wb") as stderr:
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                tee = StreamTee((proc.stdout, proc.stderr), folder / "stdout.log",
                                mirrors=(stdout.fileno(), stderr.fileno()), max_bytes=1024*1024)
                self.assertEqual(proc.wait(timeout=10), 23)
                tee.close()
            logged = (folder / "stdout.log").read_bytes()
            self.assertIn(b"out-start\n", logged)
            self.assertIn(b"out-end\n", logged)
            self.assertIn(b"stderr-marker\n", logged)
            self.assertIn(b"stderr-end\n", logged)
            self.assertIn(b"\xff", logged)
            self.assertIn(b"out-end\n", (folder / "mirrored-out.bin").read_bytes())
            self.assertIn(b"stderr-end\n", (folder / "mirrored-err.bin").read_bytes())
            self.assertEqual(tee.log.bytes_seen, len(logged))

    def test_broken_log_sink_continues_draining_and_preserves_process_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            blocker = folder / "not-a-directory"
            blocker.write_bytes(b"block")
            with (folder / "mirror.bin").open("wb") as mirror:
                proc = subprocess.Popen([sys.executable, "-c", "import os; os.write(1,b'output survives'); raise SystemExit(17)"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                tee = StreamTee((proc.stdout, proc.stderr), blocker / "stdout.log",
                                mirrors=(mirror.fileno(), mirror.fileno()))
                self.assertEqual(proc.wait(timeout=10), 17)
                tee.close()
            self.assertIn(b"output survives", (folder / "mirror.bin").read_bytes())
            self.assertTrue(tee.log.errors)

    def test_per_dp_config_preserves_all_nonlogging_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "mmc-local.conf"
            original = (
                "# Preserve the upstream protocol and pool\n"
                "ock.mmc.meta_service_url = tcp://192.0.2.21:5000\n"
                "ock.mmc.local_service.protocol = device_sdma\n"
                "ock.mmc.local_service.dram.size = 20GB\n"
                "ock.mmc.log_level = info\n"
                "ock.mmc.log_path = /unsupported/local/path\n")
            source.write_text(original, encoding="utf-8")
            outputs = [prepare_local_config(source, folder / ("dp" + str(rank))) for rank in range(2)]
            self.assertNotEqual(outputs[0], outputs[1])
            expected = original.replace("ock.mmc.log_level = info", "ock.mmc.log_level = debug").replace(
                "ock.mmc.log_path = /unsupported/local/path\n", "")
            self.assertEqual(outputs[0].read_text(encoding="utf-8"), expected)
            self.assertEqual(outputs[1].read_text(encoding="utf-8"), expected)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertNotIn(b"\r", outputs[0].read_bytes())

    def test_metadata_allowlist_omits_secrets_and_never_imports_accelerator(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with mock.patch("importlib.metadata.version", return_value="test-version"):
                write_metadata(folder, "P-DP0", {
                    "VLLM_LOGGING_LEVEL": "DEBUG", "TOKEN": "must-not-leak",
                    "REQUEST_PROMPT": "must-not-leak", "MMC_LOCAL_CONFIG_PATH": "/run/dp0/config.conf"})
            data = (folder / "logging-settings.json").read_text(encoding="utf-8")
            self.assertNotIn("must-not-leak", data)
            self.assertEqual(json.loads(data)["environment"]["VLLM_LOGGING_LEVEL"], "DEBUG")


@unittest.skipUnless(LINUX, "Requires Linux fd inheritance, bash and POSIX process groups; Windows unit tests do not certify Linux runtime.")
class LinuxRuntimeTests(unittest.TestCase):
    def runner(self, out, command, failure_status_write=False):
        expression = (
            "import sys;sys.path.insert(0,{root!r});import runtime;"
            "runtime.snapshot=lambda *args:None;"
            "{override}sys.exit(runtime.main({args!r},out={out!r}))"
        ).format(root=str(ROOT), out=str(out), args=["model", "--"] + command,
                 override="runtime.write_json=lambda *args:(_ for _ in ()).throw(OSError('synthetic diagnostic failure'));" if failure_status_write else "")
        return [sys.executable, "-u", "-c", expression]

    def test_business_import_failure_is_captured_before_any_business_import_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            res = subprocess.run(self.runner(out, [sys.executable, "-c", "import no_such_glm53flash_module"]),
                                 capture_output=True, timeout=10)
            self.assertEqual(res.returncode, 1)
            for path in (out / "stdout.log", out / "first-error.log"):
                self.assertIn(b"ModuleNotFoundError", path.read_bytes())
                self.assertIn(b"Traceback", path.read_bytes())
            self.assertIn(b"ModuleNotFoundError", res.stderr)
            self.assertEqual(json.loads((out / "exit-status.json").read_text())["exit_code"], 1)

    def test_exact_child_exit_and_failed_status_write_does_not_replace_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            res = subprocess.run(self.runner(temporary, [sys.executable, "-c", "print('child');raise SystemExit(29)"],
                                             failure_status_write=True), capture_output=True, timeout=10)
            self.assertEqual(res.returncode, 29)
            self.assertIn(b"child", (Path(temporary) / "stdout.log").read_bytes())

    def test_runtime_forwards_sigterm_and_captures_final_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            command = [sys.executable, "-u", "-c", (
                "import signal,time,sys;"
                "signal.signal(signal.SIGTERM,lambda *_:(print('child-final',flush=True),sys.exit(41)));"
                "print('child-ready',flush=True);time.sleep(60)")]
            proc = subprocess.Popen(self.runner(out, command), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.wait_for(lambda: (out / "stdout.log").exists() and b"child-ready" in (out / "stdout.log").read_bytes())
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 41, (stdout, stderr))
                self.assertIn(b"child-final", (out / "stdout.log").read_bytes())
                status = json.loads((out / "exit-status.json").read_text())
                self.assertEqual(status["received_signal"], signal.SIGTERM)
                self.assertEqual(status["exit_code"], 41)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

    def wait_for(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for child startup evidence")

    def launch_runner(self, out, template):
        args = ["--role", "P", "--template", str(template), "--dp-size", "2", "--tp-size", "8",
                "--dp-address", "192.0.2.21", "--vllm-start-port", "9081"]
        expression = "import sys;sys.path.insert(0,{root!r});import launch_online_dp;sys.exit(launch_online_dp.main({args!r},out={out!r}))".format(
            root=str(ROOT), args=args, out=str(out))
        env = os.environ.copy()
        env["MMC_LOCAL_CONFIG_PATH"] = str(ROOT / "config" / "mmc-local.conf")
        return [sys.executable, "-u", "-c", expression], env

    def test_dp_unexpected_zero_is_failure_and_config_used_is_rank_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            template = out / "worker.sh"
            template.write_text("#!/bin/bash\nprintf 'config=%s\\n' \"$MMC_LOCAL_CONFIG_PATH\"\nexit 0\n")
            command, env = self.launch_runner(out, template)
            res = subprocess.run(command, env=env, capture_output=True, timeout=10)
            self.assertEqual(res.returncode, 1, res.stderr)
            saw_config_output = False
            for rank in range(2):
                folder = out / ("dp" + str(rank))
                # Its peer may be terminated before producing any stdout.
                logged = (folder / "stdout.log").read_bytes()
                if logged:
                    self.assertIn(str(folder / "config" / "mmc-local.conf").encode(), logged)
                    saw_config_output = True
                settings = json.loads((folder / "logging-settings.json").read_text())
                self.assertEqual(settings["environment"]["MMC_LOCAL_CONFIG_PATH"], str(folder / "config" / "mmc-local.conf"))
            self.assertTrue(saw_config_output)

    def test_dp_signal_kills_only_owned_child_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            template = out / "worker.sh"
            python_code = (
                "import os,sys,time,subprocess,pathlib;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())+','+str(p.pid));"
                "print('worker-ready',flush=True);time.sleep(60)")
            template.write_text("#!/bin/bash\nexec " + shlex.quote(sys.executable) + " -u -c " + shlex.quote(python_code) +
                                " \"" + str(out) + "/pid-$4\"\n")
            unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
            command, env = self.launch_runner(out, template)
            proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            owned = []
            try:
                self.wait_for(lambda: all((out / ("pid-" + str(rank))).exists() for rank in range(2)))
                for rank in range(2):
                    owned += [int(pid) for pid in (out / ("pid-" + str(rank))).read_text().split(",")]
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 143, (stdout, stderr))
                self.assertIsNone(unrelated.poll())
                for pid in owned:
                    status = Path("/proc") / str(pid) / "stat"
                    self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z", str(pid))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                unrelated.terminate()
                unrelated.wait(timeout=5)
                for pid in owned:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
