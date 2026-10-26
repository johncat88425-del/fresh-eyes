"""Locked companion inputs: no third-party code, network or provider calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from fresh_eyes.contracts import Contract, InvalidCase, digest
from fresh_eyes.engine import run_case
from fresh_eyes.patches import apply_patch
from fresh_eyes import worker
from test_core import case_data


class CwdFileContracts(unittest.TestCase):
    def test_companions_are_hashed_and_only_inputs_enter_payload(self):
        data = case_data()
        old = Contract.from_dict(data)
        self.assertEqual(old.acceptance_sha256, digest({k: data[k] for k in
                         ("goal", "fixtures", "repo", "environment")}))
        self.assertEqual(old.execution_payload()["fixtures"][0], {"name": "one.json", "input": '{"n":1}'})
        data["fixtures"][0]["cwd_files"] = {".env.example": "KEY=\n"}
        new = Contract.from_dict(data)
        self.assertNotEqual(old.acceptance_sha256, new.acceptance_sha256)
        payload = new.execution_payload()
        self.assertEqual(payload["fixtures"][0]["cwd_files"], {".env.example": "KEY=\n"})
        self.assertNotIn("expected", json.dumps(payload))
        self.assertNotIn("validator", json.dumps(payload))
        changed = copy.deepcopy(data)
        changed["fixtures"][0]["cwd_files"][".env.example"] = "OTHER=\n"
        self.assertNotEqual(new.acceptance_sha256, Contract.from_dict(changed).acceptance_sha256)

    def test_invalid_names_types_sizes_and_counts_are_rejected(self):
        invalid = [{n: "x"} for n in [".", "..", ".git", "../x", "x/y", "x\\y", "/x", "x\x00", "a" * 65]]
        invalid += [[], {"a": None}, {"a": "é" * 32768}, {str(i): "x" for i in range(4)}, {"A": "x", "a": "y"}]
        for files in invalid:
            with self.subTest(files=str(files)[:80]), self.assertRaises(InvalidCase):
                data = case_data(); data["fixtures"][0]["cwd_files"] = files
                Contract.from_dict(data)

    def test_output_cannot_overwrite_a_companion(self):
        data = case_data(); data["fixtures"][0]["cwd_files"] = {"result.txt": "locked"}
        data["workflow"]["output"] = "result.txt"
        with self.assertRaises(InvalidCase): Contract.from_dict(data)

    def test_checkout_collisions_include_files_directories_and_dangling_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for kind in ("file", "directory", "link"):
                target = root / ".env.example"
                if kind == "file": target.write_text("original")
                elif kind == "directory": target.mkdir()
                else: target.symlink_to(root / "absent")
                data = case_data(); data["fixtures"][0]["cwd_files"] = {".env.example": "new"}
                with self.subTest(kind=kind), self.assertRaises(InvalidCase):
                    Contract.from_dict(data).check_cwd_files(root)
                if kind == "directory": target.rmdir()
                else: target.unlink()

    def test_engine_rejects_pinned_collision_before_running_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); data = case_data()
            data["fixtures"][0]["cwd_files"] = {".env.example": "new"}
            runner = Mock(); runner.dependency_mode = "pypi_live"; runner.preflight.return_value = {}
            def export(source, sha, destination):
                destination.mkdir(); (destination / ".env.example").write_text("pinned checkout")
                return {"commit_sha": sha}
            with patch("fresh_eyes.engine.export_source", side_effect=export), self.assertRaises(InvalidCase):
                run_case(Contract.from_dict(data), root, None, root / "report", runner=runner, development=True)
            runner.run.assert_not_called()

    def test_patch_cannot_change_locked_companion_even_if_normally_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root / "requirements.txt"; path.write_text("locked\n")
            with self.assertRaisesRegex(InvalidCase, "locked cwd_files"):
                apply_patch(root, {"edits": [{"path": path.name, "old": "locked", "new": "changed"}]},
                            protected_paths={path.name})
            self.assertEqual(path.read_text(), "locked\n")


class CwdFileWorker(unittest.TestCase):
    def test_main_separates_fixtures_and_removes_all_companions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bootstrap = root / "bootstrap"; (bootstrap / "repo").mkdir(parents=True)
            work = root / "work"; work.mkdir()
            (bootstrap / "payload.json").write_text(json.dumps({"setup": [],
                "workflow": {"argv": ["synthetic-tool", "{input}"], "output": "stdout"},
                "fixtures": [{"name": "one.env", "input": "A=1\n", "cwd_files": {".env.example": "A=\n"}},
                             {"name": "two.env", "input": "B=1\n"}]}))
            seen = []
            def fake(argv, cwd, env, prefix, executable_argv=None):
                if argv[0] == "synthetic-tool": seen.append(sorted(p.name for p in Path(cwd).iterdir()))
                return {"argv": argv, "exit_code": 0, "stdout": "ok", "stderr": "", "truncated": False}
            with patch.object(worker, "command", side_effect=fake), \
                 patch.object(worker, "probe_environment", return_value={"exit_code": 0, "truncated": False}):
                report = worker.main(bootstrap, work)
            self.assertNotIn("infrastructure_error", report)
            self.assertEqual(seen, [[".env.example"], []])
            self.assertEqual(len(report["outputs"]), 2)
            self.assertEqual(list((work / "repo").iterdir()), [])

    def test_main_reports_setup_collision_and_runs_no_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bootstrap = root / "bootstrap"; (bootstrap / "repo").mkdir(parents=True)
            (bootstrap / "repo/.env.example").write_text("original")
            (bootstrap / "payload.json").write_text(json.dumps({"setup": [],
                "workflow": {"argv": ["synthetic-tool"]}, "fixtures": [{"name": "one", "input": "",
                "cwd_files": {".env.example": "replacement"}}]}))
            work = root / "work"; work.mkdir()
            ok = {"exit_code": 0, "stdout": "", "stderr": "", "truncated": False}
            with patch.object(worker, "command", return_value=ok) as command, \
                 patch.object(worker, "probe_environment", return_value=ok):
                report = worker.main(bootstrap, work)
            self.assertIn("infrastructure_error", report)
            self.assertEqual(report["outputs"], [])
            self.assertFalse(any(c.args[0][0] == "synthetic-tool" for c in command.call_args_list))
            self.assertEqual((work / "repo/.env.example").read_text(), "original")

    def test_actual_self_written_process_reads_each_fixture_and_cleanup_is_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir(); inputs = root / "inputs"; inputs.mkdir()
            script = "from pathlib import Path;import sys;print(Path('.env.example').read_text()+Path(sys.argv[1]).read_text())"
            flow = {"argv": [sys.executable, "-I", "-c", script, "{input}"], "output": "stdout"}
            for i, value in enumerate(["FIRST=\n", "SECOND=\n"]):
                fixture = {"name": f"{i}.env", "input": "VALUE\n", "cwd_files": {".env.example": value}}
                result = worker.run_fixture(fixture, flow, repo, inputs, {}, root, root / "venv", i)
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["content"], value + "VALUE\n\n")
                self.assertFalse((repo / ".env.example").exists())

    def test_cleanup_occurs_when_command_raises_or_reports_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir(); inputs = root / "inputs"; inputs.mkdir()
            fixture = {"name": "x", "input": "x", "cwd_files": {".env.example": "A=\n"}}
            flow = {"argv": ["synthetic-tool", "{input}"], "output": "stdout"}
            with patch.object(worker, "command", side_effect=RuntimeError("synthetic failure")), self.assertRaises(RuntimeError):
                worker.run_fixture(fixture, flow, repo, inputs, {}, root, root / "venv", 0)
            self.assertFalse((repo / ".env.example").exists())
            with patch.object(worker, "command", return_value={"exit_code": 124, "stdout": "", "truncated": False}):
                self.assertEqual(worker.run_fixture(fixture, flow, repo, inputs, {}, root, root / "venv", 1)["exit_code"], 124)
            self.assertFalse((repo / ".env.example").exists())

    def test_worker_rechecks_installer_created_file_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root / ".env.example"; path.write_text("installer-created")
            with self.assertRaises(ValueError):
                with worker.fixture_cwd_files(root, {".env.example": "fixture"}): self.fail("must not enter")
            self.assertEqual(path.read_text(), "installer-created")

    def test_cleanup_cannot_follow_a_replaced_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); outside = root / "preserve"; outside.write_text("safe")
            with worker.fixture_cwd_files(root, {".env.example": "fixture"}):
                (root / ".env.example").unlink(); (root / ".env.example").symlink_to(outside)
            self.assertEqual(outside.read_text(), "safe")
            self.assertFalse((root / ".env.example").is_symlink())

    def test_symlink_repository_and_invalid_worker_payload_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); actual = root / "actual"; actual.mkdir(); link = root / "link"; link.symlink_to(actual)
            with self.assertRaises(OSError):
                with worker.fixture_cwd_files(link, {"x": "locked"}): self.fail("must not enter")
            with self.assertRaises(ValueError):
                with worker.fixture_cwd_files(actual, {"../x": "locked"}): self.fail("must not enter")

    def test_unremovable_input_stops_before_next_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "no next fixture"):
                with worker.fixture_cwd_files(root, {".env.example": "fixture"}):
                    (root / ".env.example").unlink(); (root / ".env.example").mkdir()


if __name__ == "__main__": unittest.main()
