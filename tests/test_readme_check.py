"""The maintainer command reports in plain words and fails CI only on the chosen results; synthetic runner, no Docker."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
import io

from test_github_startup import README, Runner

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("readme_check", ROOT / "scripts/readme-check.py")
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)


class ReadmeCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text(README)
        (self.repo / "pyproject.toml").write_text("[project]\nname='unit'\n")

    def run_check(self, *args, runner=None, environ=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rc.main([str(self.repo), "--out", str(Path(tempfile.mkdtemp(dir=self.temp.name)) / "w"), *args],
                           environ=environ or {}, runner=runner or Runner())
        return code, out.getvalue()

    def test_working_readme_passes_and_says_start_up_only(self):
        summary = Path(self.temp.name) / "summary.md"
        code, text = self.run_check("--summary", str(summary), environ={"CI": "true", "GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "a" * 40})
        self.assertEqual(code, 0)
        self.assertIn("Installed and started as the README says", text)
        self.assertIn("start-up check only", text)
        self.assertIn("No AI model was called", text)
        self.assertEqual(summary.read_text(), text)

    def test_broken_start_up_fails_ci_by_default(self):
        code, text = self.run_check(runner=Runner(False))
        self.assertEqual(code, 1)
        self.assertIn("failed", text)

    def test_unclear_readme_runs_nothing_and_fails_only_when_asked(self):
        (self.repo / "README.md").write_text("No commands.\n")
        code, text = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("did not run anything", text)
        self.assertEqual(self.run_check("--fail-on", "unresolved,needs_input")[0], 1)

    def test_a_runner_that_breaks_is_reported_not_crashed(self):
        class Broken(Runner):
            def preflight(self):
                raise OSError("docker: command not found")
        code, text = self.run_check(runner=Broken())
        self.assertEqual(code, 2)
        self.assertIn("Our runner failed", text)
        self.assertIn("did not run anything", text)

    def test_failed_image_pull_never_falls_back_to_a_cached_image(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        target = Path(self.temp.name) / "failed-pull"
        with patch.object(rc.subprocess, "run", return_value=SimpleNamespace(returncode=1)), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = rc.main([str(self.repo), "--out", str(target)], environ={})
        self.assertEqual(code, 2)
        result = __import__("json").loads((target / "report/report.json").read_text())
        self.assertFalse(result["executed"])
        self.assertEqual(result["model_calls"], 0)
        self.assertIn("could not prepare", result["reason"])

    def test_existing_evidence_directory_is_never_overwritten(self):
        target = Path(self.temp.name) / "prior"
        target.mkdir(); (target / "report.json").write_text("original evidence")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = rc.main([str(self.repo), "--out", str(target)], environ={}, runner=Runner())
        self.assertEqual(code, 2)
        self.assertEqual((target / "report.json").read_text(), "original evidence")
        self.assertFalse((target / "source").exists())

    def test_bad_fail_on_is_refused(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            rc.main([str(self.repo), "--fail-on", "verified_fix"], environ={}, runner=Runner())


if __name__ == "__main__":
    unittest.main()


class ActionDefinitionTests(unittest.TestCase):
    def test_action_passes_inputs_through_the_environment_and_reads_no_secrets(self):
        action = (ROOT / "action.yml").read_text()
        self.assertIn("using: composite", action)
        for run in __import__("re").findall(r"run: \|\n((?:        .*\n)+)", action):
            self.assertNotIn("${{", run)
        self.assertNotIn("secrets.", action)
        self.assertIn('--summary "$GITHUB_STEP_SUMMARY"', action)
        self.assertIn("fresh-eyes-source.zip", action)
        self.assertLess(action.index('src/fresh_eyes/startup.py'), action.index('elif [ -f'))
        self.assertIn('CHECK_DIR=$(mktemp -d', action)
        self.assertIn('cp "$ACTION_DIR/scripts/readme-check.py"', action)
