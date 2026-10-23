"""Portable behavior tests; no assertion of Linux/NPU runtime acceptance."""
import datetime
import json
import itertools
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import host_logging as host

CID = "a" * 64


class HostLoggingTests(unittest.TestCase):
    def test_rotation_keeps_bounded_recent_window_and_first_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "kernel.log"
            writer = host.RotatingLog(path, max_bytes=10, backups=2, first_bytes=15)
            payload = b"FIRST-ERROR" + b"0123456789" * 6
            writer.write(payload)
            self.assertEqual(path.with_name("kernel.log.first").read_bytes(), payload[:15])
            recent = b"".join(path.with_name("kernel.log." + str(index)).read_bytes()
                              for index in (2, 1)) + path.read_bytes()
            self.assertTrue(payload.endswith(recent))
            self.assertLessEqual(sum(item.stat().st_size for item in Path(temporary).glob("*")), 45)
            writer = host.RotatingLog(path, max_bytes=10, backups=2, first_bytes=15)
            writer.write(b"more")
            self.assertEqual(path.with_name("kernel.log.first").read_bytes(), payload[:15])

    def test_execute_drains_both_pipes_and_records_real_truncation(self):
        info, output, error = host.execute(
            [sys.executable, "-c", "import os; os.write(1,b'A'*100000); os.write(2,b'B'*90000)"],
            limit=4096)
        self.assertEqual(info["returncode"], 0)
        self.assertEqual(output, b"A" * 4096)
        self.assertEqual(error, b"B" * 4096)
        self.assertEqual(info["bytes_seen"], {"stdout": 100000, "stderr": 90000})
        self.assertEqual(info["truncated"], {"stdout": True, "stderr": True})
        self.assertTrue(info["pipe_drain_complete"])
        self.assertIn("utc", info["started"])
        self.assertIn("monotonic_seconds", info["finished"])

    def test_execute_timeout_terminates_only_owned_child(self):
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            began = time.monotonic()
            info, _, _ = host.execute([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
            self.assertEqual(info["status"], "timeout")
            self.assertIsNotNone(info["returncode"])
            self.assertLess(time.monotonic() - began, 6)
            self.assertIsNone(sentinel.poll())
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=5)

    def test_execute_missing_command_is_explicit_partial_evidence(self):
        info, output, _ = host.execute(["codex-host-observer-intentionally-absent-command"])
        self.assertEqual(info["status"], "missing_command")
        self.assertIsNone(info["returncode"])
        self.assertEqual(output, b"")

    def test_reject_short_cid_before_running_docker(self):
        with mock.patch.object(host, "execute") as execute:
            with self.assertRaises(ValueError):
                host.inspect_container("abc123")
            execute.assert_not_called()

    def test_inspect_does_not_dump_env_or_command(self):
        identity = {"id": CID, "deployment": host.DEPLOYMENT, "service": "model",
                    "state": {"Running": True, "Status": "running"}}
        with mock.patch.object(host, "execute", return_value=({"returncode": 0}, json.dumps(identity).encode(), b"")) as execute:
            self.assertEqual(host.inspect_container(CID, "model"), identity)
            command = execute.call_args[0][0]
            self.assertEqual(command[-1], CID)
            self.assertNotIn("Env", command[3])
            self.assertNotIn("Cmd", command[3])
            with self.assertRaises(RuntimeError):
                host.inspect_container(CID, "meta")

    def test_wrong_labels_refuse_observation(self):
        identity = {"id": CID, "deployment": "another-deployment", "service": "model", "state": {}}
        with mock.patch.object(host, "execute", return_value=({"returncode": 0}, json.dumps(identity).encode(), b"")):
            with self.assertRaises(RuntimeError):
                host.inspect_container(CID)

    def test_pid_birth_handles_spaces_and_parentheses_in_comm(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / "321").mkdir()
            (proc / "sys/kernel/random").mkdir(parents=True)
            (proc / "sys/kernel/random/boot_id").write_text("boot-test")
            fields = ["S"] + [str(number) for number in range(4, 23)]
            (proc / "321/stat").write_text("321 (name (contains) spaces) " + " ".join(fields))
            self.assertEqual(host.process_birth(321, proc), {"pid": 321, "boot_id": "boot-test", "start_ticks": "22"})

    def test_duplicate_observer_cannot_overwrite_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            birth = {"pid": os.getpid(), "start_ticks": "123", "boot_id": "boot"}
            with mock.patch.object(host, "process_birth", return_value=birth):
                first = host.ObserverLock(temporary, CID)
                first.acquire()
                original = first.path.read_bytes()
                duplicate = host.ObserverLock(temporary, CID)
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    duplicate.acquire()
                self.assertEqual(first.path.read_bytes(), original)
                first.release()
                self.assertFalse(first.path.exists())

    def test_stale_lock_is_reported_without_unlink_race(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(host, "process_birth", return_value={"pid": 123, "start_ticks": "123", "boot_id": "boot"}):
                stale = host.ObserverLock(temporary, CID)
                stale.acquire()
            with mock.patch.object(host, "process_birth", return_value={"pid": 123, "start_ticks": "456", "boot_id": "boot"}):
                duplicate = host.ObserverLock(temporary, CID)
                with self.assertRaisesRegex(RuntimeError, "Stale observer.lock"):
                    duplicate.acquire()
            self.assertTrue(stale.path.exists())

    def test_follow_flushes_sparse_lines_before_exit_and_stops_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            events = host.RotatingLog(directory / "events")
            follower = host.Follow("kernel", [sys.executable, "-u", "-c",
                                              "import sys,time; print('first failure',flush=True); time.sleep(30)"],
                                   directory, events)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (directory / "kernel.stdout.log").exists():
                    time.sleep(0.05)
                self.assertIn(b"first failure", (directory / "kernel.stdout.log").read_bytes())
                self.assertTrue(follower.alive())
            finally:
                follower.close()
            self.assertIsNotNone(follower.process.poll())
            data = (directory / "events").read_text()
            self.assertIn('"follow_started"', data)
            self.assertIn('"follow_finished"', data)

    def test_since_utc_requires_timezone(self):
        self.assertEqual(host.parse_since("1970-01-01T00:00:00Z"), 0)
        with self.assertRaises(ValueError):
            host.parse_since("2026-10-23T10:00:00")

    def test_kernel_missing_journal_falls_back_and_marks_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            observer = host.HostObserver(CID, temporary)
            with mock.patch.object(observer, "command_snapshot", return_value=({"returncode": 1}, b"", b"denied")), \
                 mock.patch.object(observer, "add_follow", return_value="fallback") as follow:
                self.assertEqual(observer.start_kernel(), "fallback")
            self.assertEqual(follow.call_args[0][0], "dmesg-follow")
            self.assertTrue(any("dmesg ring" in reason for reason in observer.partial))

    def test_system_and_kernel_follow_include_original_start_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            observer = host.HostObserver(CID, temporary, since_utc="1970-01-01T00:01:00Z")
            with mock.patch.object(observer, "command_snapshot", return_value=({"returncode": 0}, b"kernel boot log", b"")), \
                 mock.patch.object(observer, "add_follow") as follow:
                observer.start_kernel()
                command = follow.call_args[0][1]
                self.assertEqual(command[command.index("--since") + 1], "@60")
                observer.system_logs()
                command = follow.call_args[0][1]
                self.assertEqual(command[command.index("--since") + 1], "@60")
                self.assertNotIn("-k", command)

    def test_non_linux_run_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(host.platform, "system", return_value="Windows"):
                with self.assertRaisesRegex(RuntimeError, "requires Linux"):
                    host.HostObserver(CID, temporary).run(once=True)

    def test_once_registers_identity_saves_final_state_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            identity = {"id": CID, "image": "sha256:test", "deployment": host.DEPLOYMENT,
                        "service": "model", "state": {"Status": "running", "Pid": 1}}
            birth = {"pid": os.getpid(), "start_ticks": "123", "boot_id": "test-boot"}
            observer = host.HostObserver(CID, directory)
            follower = mock.Mock()
            follower.name = "journal-kernel"
            follower.alive.return_value = True
            observer.followers.append(follower)
            with mock.patch.object(host.platform, "system", return_value="Linux"), \
                 mock.patch.object(host, "inspect_container", return_value=identity), \
                 mock.patch.object(host, "process_birth", return_value=birth), \
                 mock.patch.object(host.os, "chmod"), \
                 mock.patch.object(host.os, "nice", create=True), \
                 mock.patch.object(observer, "start_kernel", return_value=follower), \
                 mock.patch.object(observer, "system_logs"), \
                 mock.patch.object(observer, "add_follow", return_value=follower), \
                 mock.patch.object(observer, "command_snapshot"), \
                 mock.patch.object(observer, "snapshot") as snapshot:
                self.assertEqual(observer.run(once=True), 0)
            data = json.loads((directory / "observer.json").read_text())
            self.assertEqual(data["status"], "finished")
            self.assertEqual(data["observer_identity"], birth)
            self.assertEqual(data["finish_reason"], "once")
            self.assertEqual(data["last_container_state"]["Status"], "running")
            self.assertTrue((directory / "observer-registered.json").is_file())
            self.assertTrue((directory / "observer-ready.json").is_file())
            self.assertFalse((directory / "observer.lock").exists())
            self.assertEqual([call.args[0] for call in snapshot.call_args_list], ["initial", "final"])
            follower.close.assert_called_once()

    def test_follow_logging_failure_terminates_its_already_started_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            events = mock.Mock()
            events.record.side_effect = OSError("disk full")
            processes = []
            original_popen = subprocess.Popen

            def capture_popen(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)
                return process

            with mock.patch.object(host.subprocess, "Popen", side_effect=capture_popen):
                with self.assertRaisesRegex(OSError, "disk full"):
                    host.Follow("kernel", [sys.executable, "-c", "import time; time.sleep(30)"],
                                Path(temporary), events)
            self.assertIsNotNone(processes[0].poll())

    def test_container_exit_finishes_observer_and_keeps_business_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            initial = {"id": CID, "deployment": host.DEPLOYMENT, "service": "model",
                       "state": {"Status": "running", "Running": True}}
            exited = dict(initial, state={"Status": "exited", "Running": False, "ExitCode": 23})
            birth = {"pid": os.getpid(), "start_ticks": "123", "boot_id": "test-boot"}
            observer = host.HostObserver(CID, directory)
            follower = mock.Mock()
            follower.name = "journal-kernel"
            follower.alive.return_value = True
            observer.followers.append(follower)
            with mock.patch.object(host.platform, "system", return_value="Linux"), \
                 mock.patch.object(host, "inspect_container", side_effect=[initial, exited]), \
                 mock.patch.object(host, "process_birth", return_value=birth), \
                 mock.patch.object(host.os, "chmod"), \
                 mock.patch.object(host.os, "nice", create=True), \
                 mock.patch.object(host.time, "monotonic", side_effect=itertools.count(0, 10)), \
                 mock.patch.object(observer, "start_kernel", return_value=follower), \
                 mock.patch.object(observer, "system_logs"), \
                 mock.patch.object(observer, "add_follow", return_value=follower), \
                 mock.patch.object(observer, "command_snapshot"), \
                 mock.patch.object(observer, "snapshot"):
                self.assertEqual(observer.run(), 0)
            data = json.loads((directory / "observer.json").read_text())
            self.assertEqual(data["finish_reason"], "container_exited")
            self.assertEqual(data["last_container_state"]["ExitCode"], 23)
            self.assertFalse((directory / "observer.lock").exists())
            follower.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
