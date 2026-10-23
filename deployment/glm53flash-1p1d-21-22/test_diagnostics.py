"""Local bundle/selection tests. Docker, host commands and HTTP are fixtures."""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

import diagnostics as diag

CID = "c" * 64


def command_result(argv, status="ok", truncated=False):
    return {"argv": argv, "status": status, "returncode": 0 if status == "ok" else 1,
            "truncated": {"stdout": truncated, "stderr": False}, "pipe_drain_incomplete": False}


class BundleTests(unittest.TestCase):
    def test_copy_budget_and_file_cap_mark_partial_with_real_snapshot_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "a.log").write_bytes(b"a" * 12)
            (source / "b.log").write_bytes(b"b" * 9)
            bundle = diag.Bundle(root / "evidence", max_total=10, max_file=8)
            bundle.copy_tree(source, Path("runs") / "fixture")
            self.assertEqual(bundle.used, 10)
            self.assertEqual(sum(record["bytes"] for record in bundle.files), 10)
            self.assertTrue(bundle.issues)
            self.assertTrue(all(record["truncated"] for record in bundle.files))
            for record in bundle.files:
                saved = (bundle.output / record["path"]).read_bytes()
                self.assertEqual(record["sha256"], hashlib.sha256(saved).hexdigest())
                self.assertLessEqual(len(saved), 8)

    def test_initial_first_error_segment_wins_over_expendable_rolls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "stdout.log.1").write_bytes(b"roll" * 20)
            (source / "stdout.log.first").write_bytes(b"FIRST")
            bundle = diag.Bundle(root / "evidence", max_total=5)
            bundle.copy_tree(source, "runs/fixture")
            self.assertEqual((bundle.output / "runs/fixture/stdout.log.first").read_bytes(), b"FIRST")
            self.assertFalse((bundle.output / "runs/fixture/stdout.log.1").exists())
            self.assertTrue(any("cap" in issue["reason"] for issue in bundle.issues))

    def test_symlink_file_and_directory_are_skipped_without_reading_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, private = root / "source", root / "outside"
            source.mkdir()
            private.mkdir()
            (private / "secret.txt").write_bytes(b"outside fixture, must not be copied")
            (source / "normal.log").write_bytes(b"normal fixture")
            try:
                os.symlink(str(private / "secret.txt"), str(source / "linked.log"))
                os.symlink(str(private), str(source / "linked-directory"), target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest("Local OS cannot create fixture symlinks: " + str(error))
            bundle = diag.Bundle(root / "evidence")
            bundle.copy_tree(source, "runs/fixture")
            self.assertEqual(len(bundle.files), 1)
            self.assertEqual((bundle.output / "runs/fixture/normal.log").read_bytes(), b"normal fixture")
            self.assertGreaterEqual(len([issue for issue in bundle.issues if "symlink" in issue["reason"]]), 2)
            self.assertFalse((bundle.output / "runs/fixture/linked.log").exists())
            self.assertFalse((bundle.output / "runs/fixture/linked-directory").exists())

    def test_finish_manifest_and_archive_verify_every_saved_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "stdout.log").write_bytes(b"fixture log\n")
            bundle = diag.Bundle(root / "evidence")
            bundle.copy_tree(source, "runs/fixture")
            archive = bundle.finish()
            manifest = json.loads((bundle.output / "manifest.sha256.json").read_text(encoding="utf-8"))
            manifest_paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("collection-report.json", manifest_paths)
            self.assertIn("runs/fixture/stdout.log", manifest_paths)
            self.assertNotIn("manifest.sha256.json", manifest_paths)
            for entry in manifest["files"]:
                payload = (bundle.output / entry["path"]).read_bytes()
                self.assertEqual(entry["bytes"], len(payload))
                self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())
            checksum = Path(str(archive) + ".sha256").read_text().split()[0]
            self.assertEqual(checksum, hashlib.sha256(archive.read_bytes()).hexdigest())
            with tarfile.open(archive, "r:gz") as package:
                names = package.getnames()
                self.assertIn("evidence/manifest.sha256.json", names)
                self.assertTrue(all(name == "evidence" or name.startswith("evidence/") for name in names))
                self.assertEqual(package.extractfile("evidence/runs/fixture/stdout.log").read(), b"fixture log\n")

    def test_truncated_command_is_reported_as_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = diag.Bundle(Path(temporary) / "evidence")
            argv = ["docker", "logs", CID]
            with mock.patch.object(diag, "execute", return_value=(command_result(argv, truncated=True), b"first chunk", b"")):
                bundle.command("docker-logs", argv)
            self.assertTrue(bundle.issues, "Truncated Docker output must make the bundle partial")

    def test_failed_command_is_reported_as_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = diag.Bundle(Path(temporary) / "evidence")
            argv = ["journalctl", "-b"]
            with mock.patch.object(diag, "execute", return_value=(command_result(argv, status="command_failed"), b"", b"permission denied")):
                bundle.command("journal", argv)
            self.assertTrue(bundle.issues, "Failed log acquisition must make the bundle partial")

    def test_incomplete_pipe_drain_is_reported_as_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = diag.Bundle(Path(temporary) / "evidence")
            argv = ["journalctl", "-b"]
            result = command_result(argv)
            result["pipe_drain_incomplete"] = True
            with mock.patch.object(diag, "execute", return_value=(result, b"partial fixture", b"")):
                bundle.command("journal", argv)
            self.assertTrue(bundle.issues, "Incomplete pipe draining must make the bundle partial")

    def test_invalid_budget_fails_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            with self.assertRaises(ValueError):
                diag.Bundle(output, max_total=0)
            self.assertFalse(output.exists())


class CollectionSelectionTests(unittest.TestCase):
    def make_run(self, logroot, name, node=21, service="model", deployment=diag.DEPLOYMENT):
        folder = logroot / "runs" / name
        folder.mkdir(parents=True)
        plan = {"node": node, "service": service, "deployment": deployment,
                "container": "glm53flash-21-22-{}-{}".format(service, node),
                "image": "quay.io/ascend/vllm-ascend:v0.30.0rc2-a3"}
        (folder / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (folder / "stdout.log").write_text("fixture-" + name, encoding="utf-8")
        return folder

    def collect_fixture(self, root, logroot, source=None, running=True, labels_valid=True,
                        missing=False, service="model"):
        output = root / "evidence"
        calls = []
        identity = {"id": CID, "state": {"Running": running, "Status": "running" if running else "exited", "ExitCode": 23},
                    "image": "sha256:fixture", "deployment": diag.DEPLOYMENT if labels_valid else "unrelated",
                    "service": service, "mounts": []}
        if source is not None:
            identity["mounts"] = [{"Destination": "/run/glm53flash", "Source": str(source)}]

        def execute(argv, timeout, limit=0):
            calls.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                if missing:
                    return command_result(argv, "command_failed"), b"", b"no such container"
                return command_result(argv), json.dumps(identity).encode(), b""
            if "collect_server.py" in " ".join(argv):
                profile = Path(argv[argv.index("--output") + 1])
                profile.mkdir(parents=True)
                (profile / "fixture.json").write_text('{"fixture":true}', encoding="utf-8")
            return command_result(argv), b"fixture output\n", b""

        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(b"fixture_metric 1\n")
        native_fixture = root / "native-driver-fixture"
        native_fixture.mkdir()
        copy_tree = diag.Bundle.copy_tree

        def fixture_copy_tree(bundle, source_path, relative):
            # Never read a real host's driver logs, including when run on Linux.
            actual = native_fixture if str(source_path).replace("\\", "/") == "/var/log/npu" else source_path
            return copy_tree(bundle, actual, relative)

        with mock.patch.object(diag, "execute", side_effect=execute), \
             mock.patch.object(diag.Bundle, "copy_tree", new=fixture_copy_tree), \
             mock.patch.object(diag.urllib.request, "build_opener", return_value=opener):
            archive, partial = diag.collect(21, service=service, logroot=logroot, output=output)
        return output, archive, partial, calls

    def test_stopped_container_logs_and_mounted_run_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            source = self.make_run(logroot, "stopped-model-21")
            output, _, _, calls = self.collect_fixture(root, logroot, source, running=False)
            self.assertEqual((output / "runs/stopped-model-21/stdout.log").read_text(), "fixture-stopped-model-21")
            docker_logs = [argv for argv in calls if argv[:2] == ["docker", "logs"]]
            self.assertEqual(len(docker_logs), 1)
            self.assertEqual(docker_logs[0][-1], CID)
            host_command = next(argv for argv in calls if "collect_server.py" in " ".join(argv))
            self.assertNotIn("--container", host_command)

    def test_removed_container_failed_run_is_still_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            self.make_run(logroot, "previous-failed-model-21")
            output, _, partial, calls = self.collect_fixture(root, logroot, missing=True)
            self.assertTrue((output / "runs/previous-failed-model-21/stdout.log").exists())
            self.assertTrue(partial)
            self.assertFalse(any(argv[:2] == ["docker", "logs"] for argv in calls))

    def test_wrong_labels_prevent_docker_logs_and_mount_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            outside = root / "unrelated-run"
            outside.mkdir()
            (outside / "stdout.log").write_text("must not be copied")
            output, _, partial, calls = self.collect_fixture(root, logroot, outside, labels_valid=False)
            self.assertTrue(partial)
            self.assertFalse(any(argv[:2] == ["docker", "logs"] for argv in calls))
            self.assertFalse((output / "runs/unrelated-run").exists())

    def test_mount_outside_runs_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            outside = root / "unrelated-run"
            outside.mkdir()
            (outside / "stdout.log").write_text("must not be copied")
            output, _, partial, _ = self.collect_fixture(root, logroot, outside)
            self.assertTrue(partial)
            self.assertFalse((output / "runs/unrelated-run").exists())

    def test_historical_selection_excludes_other_node_service_and_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            self.make_run(logroot, "owned-model-21")
            self.make_run(logroot, "other-node", node=22)
            self.make_run(logroot, "other-service", service="meta")
            self.make_run(logroot, "other-deployment", deployment="unrelated")
            output, _, _, _ = self.collect_fixture(root, logroot, missing=True)
            self.assertTrue((output / "runs/owned-model-21/stdout.log").exists())
            for name in ("other-node", "other-service", "other-deployment"):
                self.assertFalse((output / "runs" / name).exists(), "Unselected run copied: " + name)

    def test_observer_partial_coverage_propagates_to_bundle_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            source = self.make_run(logroot, "owned-model-21")
            (source / "os").mkdir()
            (source / "os/observer.json").write_text(json.dumps({
                "container": CID, "status": "finished",
                "partial": ["fixture kernel journal denied"],
                "finish_reason": "container_exited"}), encoding="utf-8")
            output, _, partial, _ = self.collect_fixture(root, logroot, source, running=False)
            report = json.loads((output / "collection-report.json").read_text(encoding="utf-8"))
            self.assertTrue(partial)
            self.assertTrue(report["partial"])
            self.assertIn("fixture kernel journal denied", json.dumps(report["issues"]))

    def test_legacy_own_plan_is_eligible_but_wrong_container_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            owned = self.make_run(logroot, "legacy-model-21")
            plan = json.loads((owned / "plan.json").read_text())
            del plan["deployment"]
            (owned / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(diag.run_plan(owned, logroot, 21, "model")["container"], plan["container"])
            plan["container"] = "unrelated-model-container"
            (owned / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(ValueError):
                diag.run_plan(owned, logroot, 21, "model")

    def test_runs_root_and_nested_directory_are_not_mount_run_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            owned = self.make_run(logroot, "owned-model-21")
            nested = owned / "nested"
            nested.mkdir()
            for candidate in (logroot / "runs", nested):
                with self.assertRaises(ValueError):
                    diag.run_plan(candidate, logroot, 21, "model")

    def test_malformed_historical_plan_does_not_abort_other_failed_run_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            self.make_run(logroot, "good-historical")
            bad = self.make_run(logroot, "bad-historical")
            (bad / "plan.json").write_text("[]", encoding="utf-8")
            output, archive, partial, _ = self.collect_fixture(root, logroot, missing=True)
            self.assertTrue(archive.is_file())
            self.assertTrue(partial)
            self.assertTrue((output / "runs/good-historical/stdout.log").is_file())
            self.assertFalse((output / "runs/bad-historical").exists())

    def test_malformed_observer_json_marks_partial_without_aborting_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logroot = root / diag.DEPLOYMENT
            source = self.make_run(logroot, "owned-model-21")
            (source / "os").mkdir()
            (source / "os/observer.json").write_text("[]", encoding="utf-8")
            output, archive, partial, _ = self.collect_fixture(root, logroot, source)
            report = json.loads((output / "collection-report.json").read_text(encoding="utf-8"))
            self.assertTrue(archive.is_file())
            self.assertTrue(partial)
            self.assertIn("observer metadata", json.dumps(report["issues"]))


if __name__ == "__main__":
    unittest.main()
