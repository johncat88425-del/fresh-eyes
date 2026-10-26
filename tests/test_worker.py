"""Exercise our own interpreter bootstrap; never execute third-party source on host."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fresh_eyes import worker


class WorkerInterpreter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name).resolve()
        cls.venv = cls.root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(cls.venv)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_global_pip_shadow_cannot_receive_a_documented_install(self):
        trap = self.root / "trap"
        trap.mkdir(exist_ok=True)
        (trap / "pip3").write_text("#!/bin/sh\necho WRONG_GLOBAL_PIP >&2\nexit 91\n")
        (trap / "pip3").chmod(0o755)
        (self.root / "pip.py").write_text("raise SystemExit('WRONG_REPOSITORY_PIP')\n")
        env = {"PATH": str(trap), "PIP_NO_INDEX": "1", "PIP_REQUIRE_VIRTUALENV": "1", "PIP_USER": "0",
               "PYTHONDONTWRITEBYTECODE": "1", "PIP_CONFIG_FILE": os.devnull}
        argv = ["pip3", "install", "--help"]
        result = worker.command(argv, self.root, env, self.root / "pip-probe",
                                worker.resolve_python_command(argv, self.venv))
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("WRONG_GLOBAL_PIP", result["stderr"])
        self.assertNotIn("WRONG_REPOSITORY_PIP", result["stderr"])
        self.assertEqual(result["argv"], argv)
        self.assertIn(str(self.venv / "bin/python"), result["executed_argv"])

    def test_probe_confirms_real_prefix_and_pip_location(self):
        result = worker.probe_environment(self.venv, self.root, {"PATH": "/usr/bin:/bin"}, self.root / "probe-ok")
        self.assertEqual(result["exit_code"], 0)
        data = json.loads(result["stdout"])
        self.assertEqual(Path(data["prefix"]), self.venv)
        self.assertTrue(Path(data["pip_module"]).is_relative_to(self.venv))
        self.assertTrue(data["workspace_executable"])

    def test_base_interpreter_masquerading_as_a_venv_is_rejected(self):
        fake = self.root / "fake"
        (fake / "bin").mkdir(parents=True)
        (fake / "bin/python").symlink_to(sys.executable)
        result = worker.probe_environment(fake, self.root, {"PATH": "/usr/bin:/bin"}, self.root / "probe-bad")
        self.assertNotEqual(result["exit_code"], 0)

    def test_bootstrap_failure_stops_before_repository_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap = root / "bootstrap"
            (bootstrap / "repo").mkdir(parents=True)
            (bootstrap / "payload.json").write_text(json.dumps({"setup": [{"argv": ["pip", "install", "."]}], "fixtures": []}))
            work = root / "work"
            work.mkdir()
            with patch.object(worker, "command", return_value={"exit_code": 1, "truncated": False}) as command:
                result = worker.main(bootstrap, work)
            self.assertEqual(command.call_count, 1)
            self.assertFalse(result["setup_ok"])
            self.assertIn("infrastructure_error", result)
            self.assertEqual(result["outputs"], [])

    def test_live_environment_uses_current_pypi_without_offline_links_or_cache(self):
        env = worker.execution_environment(self.root, self.root / "bootstrap", "pypi_live")
        self.assertNotIn("PIP_NO_INDEX", env)
        self.assertNotIn("PIP_FIND_LINKS", env)
        self.assertEqual(env["PIP_NO_CACHE_DIR"], "1")
        self.assertEqual(env["PIP_INDEX_URL"], "https://pypi.org/simple")
        self.assertEqual(env["HTTPS_PROXY"], "http://pypi-proxy:8080")
        offline = worker.execution_environment(self.root, self.root / "bootstrap", "offline_snapshot")
        self.assertEqual(offline["PIP_NO_INDEX"], "1")
        self.assertNotIn("HTTPS_PROXY", offline)

    def test_missing_offline_distribution_is_infrastructure_but_a_missing_readme_file_is_not(self):
        for mode, message, infrastructure in [
            ("offline_snapshot", "ERROR: No matching distribution found for synthetic-package", True),
            ("pypi_live", "ERROR: No matching distribution found for synthetic-package", False),
            ("offline_snapshot", "ERROR: Could not open requirements file: requirements.txt", False),
        ]:
            with self.subTest(mode=mode, message=message), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                bootstrap = root / "bootstrap"
                (bootstrap / "repo").mkdir(parents=True)
                (bootstrap / "payload.json").write_text(json.dumps({"dependency_mode": mode,
                    "setup": [{"argv": ["pip", "install", "synthetic-package"]}], "fixtures": []}))
                work = root / "work"; work.mkdir()
                ok = {"exit_code": 0, "truncated": False, "stdout": "", "stderr": ""}
                failed = {**ok, "exit_code": 1, "stderr": message}
                with patch.object(worker, "command", side_effect=[ok, failed, ok]), \
                     patch.object(worker, "probe_environment", return_value=ok), patch.object(worker, "check_proxy", return_value=True):
                    report = worker.main(bootstrap, work)
                self.assertEqual("infrastructure_error" in report, infrastructure)
                self.assertEqual(report["outputs"], [])
                self.assertIsNotNone(report["pip_freeze"])

    def test_unavailable_required_proxy_stops_before_any_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap = root / "bootstrap"
            (bootstrap / "repo").mkdir(parents=True)
            (bootstrap / "payload.json").write_text(json.dumps({"dependency_mode": "pypi_live"}))
            work = root / "work"
            work.mkdir()
            with patch.object(worker, "check_proxy", return_value=False), patch.object(worker, "command") as command:
                result = worker.main(bootstrap, work)
            command.assert_not_called()
            self.assertIn("proxy", result["infrastructure_error"])


if __name__ == "__main__":
    unittest.main()
