"""Local tests: address visibility/capacity, bounded execution and fixture collection."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import analyze_iomem
import collect_server


class IomemTests(unittest.TestCase):
    def test_masked_addresses_are_unknown_not_zero_capacity(self):
        result = analyze_iomem.analyze("00000000-00000000 : System RAM\n  00000000-00000000 : Kernel code\n")
        self.assertEqual(result["visibility"], "addresses_hidden")
        self.assertIsNone(result["iomem_system_ram_bytes"])

    def test_masked_ram_even_with_visible_pci_resource(self):
        result = analyze_iomem.analyze("0-0 : System RAM\nf000-f0ff : PCI Bus 0000:00\n")
        self.assertEqual(result["visibility"], "addresses_hidden")

    def test_overlaps_and_nested_ram_count_once(self):
        result = analyze_iomem.analyze("1000-1fff : System RAM\n  1100-11ff : Kernel code\n  1200-12ff : System RAM\n1800-2fff : System RAM\n")
        self.assertEqual(result["iomem_system_ram_bytes"], 0x2000)

    def test_windows_cross_nodes_do_not_guess_node_and_union_deduplicates(self):
        windows = {"source_commit": "example-only", "windows": [
            {"name": "a", "start": 0x1800, "end_inclusive": 0x2800},
            {"name": "b", "start": 0x2000, "end_inclusive": 0x3fff}]}
        result = analyze_iomem.analyze("1000-1fff : System RAM\n2000-2fff : System RAM\n", windows)
        self.assertEqual(result["windows"][0]["iomem_system_ram_intersection_bytes"], 0x1001)
        self.assertEqual(result["window_union_system_ram_intersection_bytes"], 0x1800)
        self.assertIn("unknown", result["windows"][0]["numa_node"])

    def test_malformed_input_never_reports_confident_capacity(self):
        result = analyze_iomem.analyze("1000-1fff : System RAM\ntruncated junk\n")
        self.assertIsNone(result["iomem_system_ram_bytes"])
        self.assertEqual(result["malformed_line_numbers"], [2])

    def test_no_windows_has_no_implicit_contract(self):
        result = analyze_iomem.analyze("1000-1fff : System RAM\n")
        self.assertEqual(result["windows"], [])
        self.assertIsNone(result["window_union_system_ram_intersection_bytes"])

    def test_uncovered_window_is_known_zero_only_for_visible_ram(self):
        windows = {"windows": [{"name": "outside", "start": 0x4000, "end_inclusive": 0x4fff}]}
        result = analyze_iomem.analyze("1000-1fff : System RAM\n", windows)
        self.assertEqual(result["windows"][0]["iomem_system_ram_intersection_bytes"], 0)
        hidden = analyze_iomem.analyze("0-0 : System RAM\n", windows)
        self.assertIsNone(hidden["windows"][0]["iomem_system_ram_intersection_bytes"])

    def test_invalid_window_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "windows.json"
            file.write_text(json.dumps({"windows": [{"name": "bad", "start": "0x4000", "end_inclusive": "0x2000"}]}))
            with self.assertRaises(ValueError):
                analyze_iomem.load_windows(file)


class CommandTests(unittest.TestCase):
    def test_missing_command_has_explicit_stderr_and_status(self):
        result, stdout, stderr = collect_server.execute(["definitely_nonexistent_hccs_command_92047"], 1)
        self.assertEqual(result["status"], "missing_command")
        self.assertTrue(stderr)

    def test_failed_command_preserves_stderr_and_code(self):
        result, stdout, stderr = collect_server.execute([sys.executable, "-c", "import sys; print('reason',file=sys.stderr); sys.exit(7)"], 3)
        self.assertEqual(result["status"], "command_failed")
        self.assertEqual(result["returncode"], 7)
        self.assertIn(b"reason", stderr)

    def test_timeout_is_bounded_and_explained(self):
        result, stdout, stderr = collect_server.execute([sys.executable, "-c", "import time; time.sleep(30)"], 0.15)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(result["elapsed_seconds"], 6)
        self.assertIn(b"timeout", stderr)

    def test_large_both_streams_bounded_without_deadlock(self):
        result, stdout, stderr = collect_server.execute([sys.executable, "-c", "import sys; sys.stdout.write('x'*1000000); sys.stderr.write('y'*1000000)"], 5, limit=1024)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(stdout), 1024)
        self.assertEqual(len(stderr), 1024)
        self.assertTrue(result["truncated"]["stdout"])
        self.assertTrue(result["truncated"]["stderr"])

    def test_container_metadata_does_not_import_runtime_or_collect_arbitrary_env(self):
        script = collect_server.container_python()
        self.assertNotIn("import torch", script)
        self.assertNotIn("environ.items", script)
        self.assertNotIn("TOKEN", collect_server.ENV_ALLOW)
        env = dict(os.environ, TEST_SECRET_TOKEN="must-not-be-collected", FULL_MEMCACHE="1")
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        metadata = json.loads(result.stdout)
        self.assertEqual(metadata["environment"]["FULL_MEMCACHE"], "1")
        self.assertNotIn("must-not-be-collected", result.stdout.decode())

    @unittest.skipUnless(os.name == "posix", "Linux SIGALRM is verified on Linux only")
    def test_container_self_deadline_terminates_its_own_process(self):
        script = collect_server.container_python(timeout=1)
        alarm_line = script.splitlines()[0]
        result = subprocess.run([sys.executable, "-c", alarm_line + "\nimport time; time.sleep(30)"], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, -14)


class FixtureCollectionTests(unittest.TestCase):
    def test_truncated_iomem_cannot_leave_numeric_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / "fixture", Path(directory) / "collected"
            (root / "proc").mkdir(parents=True)
            (root / "proc/iomem").write_text("1000-1fff : System RAM\n" + "\n" * collect_server.MAX_BYTES)
            args = argparse.Namespace(output=str(output), fs_root=str(root), timeout=1,
                                      pid=[], container=[], windows_json=None, no_commands=True)
            with contextlib.redirect_stdout(io.StringIO()):
                collect_server.collect(args)
            report = json.loads((output / "iomem_analysis.json").read_text())
            self.assertEqual(report["visibility"], "unknown_or_incomplete")
            self.assertIsNone(report["iomem_system_ram_bytes"])

    def test_linux_file_fixture_manifest_and_sensitive_boot_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / "fixture", Path(directory) / "collected"
            (root / "proc").mkdir(parents=True)
            (root / "proc/iomem").write_text("1000-1fff : System RAM\n")
            (root / "proc/cmdline").write_text("amd_iommu=on hugepagesz=1G root_password=do-not-copy\n")
            args = argparse.Namespace(output=str(output), fs_root=str(root), timeout=1,
                                      pid=[], container=[], windows_json=None, no_commands=True)
            with contextlib.redirect_stdout(io.StringIO()):
                collect_server.collect(args)
            status = json.loads((output / "collection_status.json").read_text())
            self.assertTrue(status["read_only"])
            self.assertFalse(status["network_or_upload"])
            self.assertEqual(status["commands"], [])
            self.assertTrue(any(item["status"] == "read_failed" for item in status["files"]))
            self.assertEqual(json.loads((output / "iomem_analysis.json").read_text())["iomem_system_ram_bytes"], 0x1000)
            boot = (output / "raw/boot_memory_options.json").read_text()
            self.assertIn("amd_iommu=on", boot)
            self.assertNotIn("do-not-copy", boot)
            self.assertFalse((output / "raw/files/proc/cmdline").exists())
            manifest = json.loads((output / "manifest.sha256.json").read_text())
            for item in manifest["files"]:
                self.assertEqual(hashlib.sha256((output / item["path"]).read_bytes()).hexdigest(), item["sha256"])
            if os.name == "posix":
                self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                collect_server.Collector(output, root=str(root))


if __name__ == "__main__":
    unittest.main()
