"""Portable deployment logging tests. No Docker, SSH, NPU or HTTP is executed."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import deploy
import diagnostics

CID = "d" * 64


class DeployLoggingTests(unittest.TestCase):
    def test_existing_container_is_refused_without_stop_remove_or_restart(self):
        plan = deploy.description("meta", 21, None)
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(deploy.shutil, "which", return_value="/fixture/tool"), \
                 mock.patch.object(deploy, "local_nic", return_value="fixture0"), \
                 mock.patch.object(deploy, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    deploy.start_checked(plan, Path(temporary))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], ["docker", "container", "inspect", plan["container"]])

    def test_preflight_failure_has_local_plan_before_and_failure_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            logroot = Path(temporary) / "logs"
            plan = deploy.description("meta", 21, None)
            captures = []

            def capture(output, container=None):
                captures.append((output, container))
                output.mkdir(parents=True)
                (output / "fixture.txt").write_text("bounded read-only evidence", encoding="utf-8")

            with mock.patch.object(deploy.platform, "system", return_value="Linux"), \
                 mock.patch.object(deploy, "LOGROOT", logroot), \
                 mock.patch.object(deploy, "capture", side_effect=capture), \
                 mock.patch.object(deploy, "start_checked", side_effect=RuntimeError("fixture preflight denial")), \
                 mock.patch.object(deploy, "run") as external:
                with self.assertRaisesRegex(RuntimeError, "fixture preflight denial"):
                    deploy.start(plan)
            external.assert_not_called()
            run_dir = next((logroot / "runs").iterdir())
            saved = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["deployment"], "glm53flash-1p1d-21-22")
            self.assertTrue(saved["started_utc"].endswith("+00:00"))
            self.assertIn("fixture preflight denial", (run_dir / "start-error.txt").read_text())
            self.assertTrue((run_dir / "before/fixture.txt").exists())
            self.assertTrue((run_dir / "failure/fixture.txt").exists())
            self.assertEqual([container for _, container in captures], [None, None])

    def test_failure_capture_error_does_not_replace_original_start_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = deploy.description("meta", 21, None)
            with mock.patch.object(deploy.platform, "system", return_value="Linux"), \
                 mock.patch.object(deploy, "LOGROOT", Path(temporary)), \
                 mock.patch.object(deploy, "capture", side_effect=[None, OSError("fixture capture failed")]), \
                 mock.patch.object(deploy, "start_checked", side_effect=RuntimeError("original startup error")):
                with self.assertRaisesRegex(RuntimeError, "^original startup error$"):
                    deploy.start(plan)
            run_dir = next((Path(temporary) / "runs").iterdir())
            self.assertIn("original startup error", (run_dir / "start-error.txt").read_text())

    def test_observer_is_detached_and_receives_original_start_window_and_cid(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "os").mkdir()
            (run_dir / "os/observer-registered.json").write_text('{"status":"initializing"}')
            plan = deploy.description("meta", 21, None)
            plan["started_utc"] = "2026-10-23T01:02:03+00:00"
            process = mock.Mock(pid=12345)
            process.poll.return_value = None
            with mock.patch.object(deploy.subprocess, "Popen", return_value=process) as popen:
                deploy.launch_observer(plan, run_dir, CID)
            argv = popen.call_args.args[0]
            self.assertEqual(argv[argv.index("--container") + 1], CID)
            self.assertEqual(argv[argv.index("--since-utc") + 1], plan["started_utc"])
            self.assertEqual(argv[argv.index("--output") + 1], str(run_dir / "os"))
            self.assertEqual(argv[argv.index("--service") + 1], "meta")
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            saved = json.loads((run_dir / "os-observer-launch.json").read_text())
            self.assertEqual(saved["container"], CID)
            self.assertIn("registered", saved["status"])

    def test_observer_process_start_failure_is_nonfatal_and_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            plan = deploy.description("meta", 21, None)
            plan["started_utc"] = "2026-10-23T01:02:03+00:00"
            with mock.patch.object(deploy.subprocess, "Popen", side_effect=OSError("fixture observer unavailable")):
                deploy.launch_observer(plan, run_dir, CID)
            saved = json.loads((run_dir / "os-observer-launch.json").read_text())
            self.assertEqual(saved["status"], "launch_failed")
            self.assertIn("fixture observer unavailable", saved["error"])

    def test_observer_metadata_write_failure_does_not_fail_running_business(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            plan = deploy.description("meta", 21, None)
            plan["started_utc"] = "2026-10-23T01:02:03+00:00"
            original = Path.write_text

            def write_text(path, *args, **kwargs):
                if path.name == "os-observer-launch.json":
                    raise OSError("fixture launch metadata disk failure")
                return original(path, *args, **kwargs)

            with mock.patch.object(deploy.subprocess, "Popen", side_effect=OSError("fixture cannot launch")), \
                 mock.patch.object(Path, "write_text", autospec=True, side_effect=write_text):
                deploy.launch_observer(plan, run_dir, CID)

    def test_created_container_has_persistent_log_mounts_before_observer_and_state_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            plan = deploy.description("meta", 21, None)
            plan["started_utc"] = "2026-10-23T01:02:03+00:00"
            commands, order = [], []

            def run(argv, check=True, timeout=30):
                commands.append(argv)
                if argv[:3] == ["docker", "container", "inspect"]:
                    return subprocess.CompletedProcess(argv, 1, "", "")
                if argv[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(argv, 0, json.dumps([{"Architecture": "amd64", "Id": "sha256:fixture"}]), "")
                if argv[:2] == ["docker", "run"]:
                    order.append("docker-run")
                    return subprocess.CompletedProcess(argv, 0, CID + "\n", "")
                if argv[:2] == ["docker", "inspect"]:
                    order.append("state-check")
                    return subprocess.CompletedProcess(argv, 0, '{"Running":true,"ExitCode":0}', "")
                raise AssertionError("Unexpected external command: " + repr(argv))

            def observer(*args):
                self.assertEqual((run_dir / "container-id.txt").read_text().strip(), CID)
                order.append("observer")

            with mock.patch.object(deploy.shutil, "which", return_value="/fixture/tool"), \
                 mock.patch.object(deploy, "local_nic", return_value="fixture0"), \
                 mock.patch.object(deploy, "check_ports"), \
                 mock.patch.object(deploy, "run", side_effect=run), \
                 mock.patch.object(deploy, "launch_observer", side_effect=observer), \
                 mock.patch.object(deploy.time, "sleep"), \
                 mock.patch.object(Path, "exists", return_value=True):
                deploy.start_checked(plan, run_dir)
            self.assertEqual(order, ["docker-run", "observer", "state-check"])
            argv = next(command for command in commands if command[:2] == ["docker", "run"])
            self.assertEqual(argv[argv.index("--pull") + 1], "never")
            mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--volume"]
            self.assertIn(str(run_dir) + ":/run/glm53flash", mounts)
            self.assertIn(str(run_dir / "native-ascend") + ":/root/ascend/log", mounts)
            self.assertIn(str(run_dir / "native-memfabric") + ":/var/log/memfabric_hybrid", mounts)
            self.assertNotIn("--rm", argv)
            entry = argv.index("--entrypoint")
            self.assertEqual(argv[entry + 1], "python3")
            self.assertEqual(argv[entry + 3:entry + 8],
                             ["/opt/glm53flash/runtime.py", "meta", "--", "/bin/bash",
                              "/opt/glm53flash/container-env.sh"])
            environment = dict(argv[index + 1].split("=", 1)
                               for index, value in enumerate(argv) if value == "--env")
            self.assertEqual(environment["VLLM_LOGGING_LEVEL"], "DEBUG")
            self.assertEqual(environment["ASCEND_GLOBAL_LOG_LEVEL"], "0")
            self.assertEqual(environment["MF_LOG_LEVEL"], "0")
            self.assertEqual(environment["MC_LOG_LEVEL"], "TRACE")
            self.assertEqual(environment["MC_LOG_DIR"], "/run/glm53flash/mooncake")

    def test_collect_defaults_all_and_returns_two_for_partial_bundle(self):
        with mock.patch.object(sys, "argv", ["deploy.py", "collect", "--node", "21"]), \
             mock.patch.object(deploy.platform, "system", return_value="Linux"), \
             mock.patch.object(deploy, "local_nic", return_value="fixture0"), \
             mock.patch.object(diagnostics, "collect", return_value=(Path("fixture.tar.gz"), True)) as collect, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(deploy.main(), 2)
        collect.assert_called_once_with(21, "all")
        self.assertIn("partial", output.getvalue())

    def test_collect_complete_bundle_returns_zero_and_service_override_is_preserved(self):
        with mock.patch.object(sys, "argv", ["deploy.py", "collect", "--node", "22", "--service", "model"]), \
             mock.patch.object(deploy.platform, "system", return_value="Linux"), \
             mock.patch.object(deploy, "local_nic", return_value="fixture0"), \
             mock.patch.object(diagnostics, "collect", return_value=(Path("fixture.tar.gz"), False)) as collect, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(deploy.main(), 0)
        collect.assert_called_once_with(22, "model")

    def test_service_all_cannot_start_services(self):
        with mock.patch.object(sys, "argv", ["deploy.py", "start", "--node", "21", "--service", "all"]), \
             mock.patch.object(deploy, "start") as start, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                deploy.main()
        self.assertEqual(error.exception.code, 2)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
