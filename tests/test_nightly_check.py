"""Nightly provenance, failure honesty and bounded evidence; synthetic execution only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nightly_check", ROOT / "scripts/nightly-check.py")
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)
REPO = "https://github.com/Textualize/rich-cli"


class NightlyTests(unittest.TestCase):
    def check(self, status="startup_only", executed=True, event="workflow_dispatch", **extra):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "new"
            def run(path, environ):
                self.assertEqual(set(environ), {"GITHUB_REPOSITORY", "GITHUB_RUN_ID", "REQUEST_REPO"})
                path.mkdir()
                result = {"status": status, "executed": executed, "model_calls": 0,
                          "repo_url": REPO, "commit_sha": "b" * 40, **extra}
                (path / "result.json").write_text(json.dumps(result))
                return result
            env = {"GITHUB_EVENT_NAME": event, "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "123",
                   "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ATTEMPT": "1", "NEBIUS_API_KEY": "synthetic-secret"}
            record, code, text = nightly.monitor(REPO, out, environ=env, check=run, prepare=lambda: None)
            manifest = json.loads((out / "manifest.json").read_text())
            for name, checksum in manifest["files"].items():
                self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), checksum)
            self.assertNotIn("synthetic-secret", "".join(x.read_text() for x in out.iterdir() if x.is_file()))
            return record, code, text

    def test_manual_validation_never_claims_scheduled_execution(self):
        r, code, text = self.check()
        self.assertEqual((code, r["trigger"], r["verdict"]), (0, "manual", "passed"))
        self.assertFalse(r["functional_output_verified"])
        self.assertEqual(r["resolved_commit"], "b" * 40)
        self.assertEqual(r["tool_commit"], "a" * 40)
        self.assertIn("Manual validation is not evidence", text)

    def test_schedule_is_attributed_only_to_actual_schedule_event(self):
        self.assertEqual(self.check(event="schedule")[0]["trigger"], "scheduled")
        self.assertEqual(self.check(event="push")[0]["trigger"], "local")

    def test_readme_failure_and_runner_failure_are_distinct(self):
        r, code, _ = self.check(status="unresolved")
        self.assertEqual((r["verdict"], code), ("readme_failed", 1))
        r, code, _ = self.check(status="infrastructure_error", executed=False)
        self.assertEqual((r["verdict"], code), ("runner_error", 2))

    def test_no_execution_and_inconsistent_success_cannot_make_green(self):
        for status in ("needs_input", "unsupported", "startup_only"):
            with self.subTest(status=status):
                self.assertNotEqual(self.check(status=status, executed=False)[1], 0)

    def test_nonzero_or_missing_model_usage_fails_closed(self):
        for value in (1, None, False):
            with self.subTest(value=value):
                r, code, _ = self.check(model_calls=value)
                self.assertEqual((r["verdict"], code), ("runner_error", 2))

    def test_pull_failure_preserves_report_without_running_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "new"
            def broken(): raise OSError("synthetic image pull failure")
            def forbidden(*args, **kwargs): self.fail("must not run after a failed pull")
            r, code, _ = nightly.monitor(REPO, out, environ={}, check=forbidden, prepare=broken)
            self.assertEqual((r["verdict"], code, r["repository_executed"]), ("runner_error", 2, False))
            self.assertTrue((out / "manifest.json").is_file())

    def test_invalid_repository_and_existing_evidence_stop_before_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            def forbidden(): self.fail("must stop before image preparation")
            with self.assertRaises(nightly.gc.RequestError):
                nightly.monitor("https://github.com/o/r/issues/1", Path(tmp)/"new", prepare=forbidden)
            saved = Path(tmp)/"saved"; saved.mkdir(); (saved/"result.json").write_text("original")
            with self.assertRaises(FileExistsError):
                nightly.monitor(REPO, saved, prepare=forbidden)
            self.assertEqual((saved/"result.json").read_text(), "original")

    def test_workflow_has_no_write_permission_or_provider_secret(self):
        text = (ROOT / ".github/workflows/nightly-readme.yml").read_text()
        self.assertIn("timezone: Europe/London", text)
        self.assertIn("timeout-minutes: 15", text)
        self.assertIn("if: always()", text)
        self.assertIn("persist-credentials: false", text)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("pull_request_target", text)
