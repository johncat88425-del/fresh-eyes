"""The GitHub Actions check takes one URL from an untrusted issue and replies without letting repository text escape."""
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("github_check", ROOT / "scripts/github-check.py")
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)

FORM = """### Public GitHub repository

https://github.com/Textualize/rich-cli

### Commit (optional, full 40 characters)

_No response_

### Scope

- [X] I understand this checks installation and start-up only, and the result will be public.
"""
SHA = "46f4d2469097be395558768714a5f07ebccf1412"


def report(status="startup_only", stderr=""):
    return {"status": status, "reason": "The documented installation and CLI startup exited successfully; functional output has not been verified.",
            "executed": True, "model_calls": 0, "repo": {"commit_sha": SHA},
            "readme_plan": {"readme": "README.md", "setup": [{"argv": ["python", "-m", "pip", "install", "rich-cli"], "source": "README.md:L34"}],
                            "startup": {"argv": ["rich", "--help"], "source": "README.md:L48"}},
            "attempts": [{"result": {"steps": [{"argv": ["/usr/local/bin/python", "-m", "venv", "/work/venv"], "exit_code": 0},
                                               {"argv": ["python", "-m", "pip", "install", "rich-cli"], "exit_code": 0}],
                                     "outputs": [{"exit_code": 0, "process": {"argv": ["rich", "--help"], "stdout": "Usage: rich", "stderr": stderr}}]}}],
            "environment": {"platform": "linux/arm64", "image_id": "sha256:" + "a" * 64, "network": "pypi_proxy_isolated"}}


class GitHubCheckTests(unittest.TestCase):
    def test_issue_form_gives_one_repository_and_an_optional_commit(self):
        self.assertEqual(gc.parse_request(FORM), {"repo_url": "https://github.com/Textualize/rich-cli", "ref": None})
        self.assertEqual(gc.parse_request(FORM.replace("_No response_", SHA))["ref"], SHA)
        self.assertEqual(gc.parse_request("see https://github.com/a/b.git/ please")["repo_url"], "https://github.com/a/b")
        for bad in ("", "no link", "https://gitlab.com/a/b", FORM + "\nhttps://github.com/other/repo",
                    FORM.replace("_No response_", SHA + " " + "b" * 40), "https://github.com/a"):
            with self.subTest(bad=bad[:40]), self.assertRaises(gc.RequestError):
                gc.parse_request(bad)

    def test_check_uses_the_local_inspect_and_startup_path_and_records_no_model(self):
        calls = []
        def inspect(url, ref, source):
            calls.append((url, ref)); return {"plan": {}}
        def start(inspection, source, output, runner):
            calls.append("start"); return report()
        with tempfile.TemporaryDirectory() as tmp:
            result = gc.check(Path(tmp) / "out", {"ISSUE_BODY": FORM, "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7"},
                              inspect=inspect, start=start, runner=object())
            saved = json.loads((Path(tmp) / "out/result.json").read_text())
        self.assertEqual(calls, [("https://github.com/Textualize/rich-cli", None), "start"])
        self.assertEqual((saved["status"], saved["model_calls"], saved["commit_sha"]), ("startup_only", 0, SHA))
        self.assertEqual(saved["steps"], [{"command": "python -m pip install rich-cli", "exit_code": 0}])
        self.assertEqual(saved["run_url"], "https://github.com/o/r/actions/runs/7")

    def test_bad_requests_and_crashes_still_leave_a_result_to_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = gc.check(Path(tmp) / "a", {"ISSUE_BODY": "hello"})
            def boom(*args, **kwargs):
                raise OSError("disk")
            crashed = gc.check(Path(tmp) / "b", {"ISSUE_BODY": FORM}, inspect=boom, start=boom, runner=object())
        self.assertEqual(bad["status"], "invalid_request")
        self.assertIn("Couldn't read the request", gc.comment(bad))  # no run URL outside Actions
        self.assertIn("Our runner failed", gc.comment(crashed))
        self.assertEqual((crashed["status"], crashed["reason"]), ("infrastructure_error", "The check stopped: OSError"))

    def test_reply_keeps_repository_text_inside_code_blocks(self):
        hostile = "```\n# Fake heading\n[click](https://evil.example) @someone <img src=x>\n````"
        text = gc.comment({**gc.summarise(report(stderr=hostile)), "repo_url": "https://github.com/Textualize/rich-cli",
                           "run_url": "https://github.com/o/r/actions/runs/7"})
        self.assertIn("## ✅ Installed and started as the README says", text)
        self.assertIn("README.md line 34", text)
        self.assertIn("No AI model was called", text)
        # Every line of the hostile text sits inside a fence longer than any backtick run it contains.
        block = re.search(r"(`{5,})text\n(.*?)\n\1", text, re.S)
        self.assertIsNotNone(block)
        self.assertIn("# Fake heading", block[2])
        outside = text.replace(block[0], "")
        self.assertNotIn("evil.example", outside)
        self.assertNotIn("@someone", outside)

    def test_footer_says_what_actually_happened(self):
        ran = gc.comment({**gc.summarise(report()), "repo_url": "https://github.com/Textualize/rich-cli"})
        self.assertIn("in a fresh, non-root linux/arm64 container", ran)
        self.assertIn("Only PyPI was reachable", ran)
        for status in ("invalid_request", "needs_input", "unsupported", "infrastructure_error"):
            text = gc.comment({"status": status, "reason": "x", "executed": False})
            with self.subTest(status):
                self.assertIn("did not run anything from this repository", text)
                self.assertNotIn("container on a GitHub-hosted runner", text)
                self.assertNotIn("Only PyPI was reachable", text)
        # Executed but with no recorded environment: no claim about isolation.
        self.assertNotIn("Only PyPI", gc.comment({"status": "unresolved", "executed": True}))

    def test_workflow_links_must_be_exact_run_pages(self):
        for bad in ("https://github.com/", "https://github.com/o/r/actions/runs/7)[x](https://evil.example)",
                    "https://github.com/o/r/actions/runs/7 extra", "https://github.com/o/r/actions/runs/abc",
                    "https://github.com/o/r/pull/7"):
            with self.subTest(bad=bad):
                self.assertNotIn("Workflow run", gc.comment({"status": "needs_input", "run_url": bad}))
        self.assertIn("[Workflow run](https://github.com/o/r/actions/runs/7)", gc.comment({"status": "needs_input", "run_url": "https://github.com/o/r/actions/runs/7"}))
        with tempfile.TemporaryDirectory() as tmp:
            bad_env = gc.check(Path(tmp) / "x", {"ISSUE_BODY": "hi", "GITHUB_REPOSITORY": "o/r)[x](y", "GITHUB_RUN_ID": "7"})
        self.assertIsNone(bad_env["run_url"])

    def test_reply_drops_values_that_are_not_what_they_claim(self):
        text = gc.comment({"status": "startup_only", "repo_url": "javascript:alert(1)", "commit_sha": "not-a-sha",
                           "readme": "../../etc", "run_url": "https://evil.example/x"})
        self.assertNotIn("javascript", text)
        self.assertNotIn("not-a-sha", text)
        self.assertNotIn("evil.example", text)

    def test_manual_inputs_reject_bad_commit_before_inspection(self):
        self.assertEqual(gc.parse_inputs("https://github.com/Textualize/rich-cli", SHA)["ref"], SHA)
        with tempfile.TemporaryDirectory() as tmp:
            def no_inspect(*args):
                self.fail("Invalid input must stop before any repository download")
            result = gc.check(Path(tmp) / "bad", {"REQUEST_REPO": "https://github.com/a/b", "REQUEST_COMMIT": "not-a-commit"}, inspect=no_inspect)
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["model_calls"], 0)
        self.assertIn("exactly 40", result["reason"])
        for bad in ("https://github.com/a/b/issues/1", "https://github.com/a/b https://github.com/c/d"):
            with self.subTest(repo=bad), self.assertRaises(gc.RequestError):
                gc.parse_inputs(bad, "")

    def test_workflow_keeps_write_permission_away_from_repository_code(self):
        workflow = (ROOT / ".github/workflows/fresh-eyes-check.yml").read_text()
        check_job = workflow.split("\n  check:\n")[1].split("\n  summary:\n")[0]
        self.assertIn("workflow_dispatch", workflow)
        self.assertNotIn("\n  issues:", workflow)
        self.assertNotIn("\n  reply:", workflow)
        self.assertNotIn("issues: write", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("gh issue comment", workflow)
        self.assertIn("group: fresh-eyes-public-check", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("contents: read", check_job)
        self.assertIn("persist-credentials: false", check_job)
        self.assertIn("REQUEST_REPO: ${{ inputs.repo_url }}", check_job)
        self.assertIn("REQUEST_COMMIT: ${{ inputs.commit }}", check_job)
        self.assertIn("permissions: {}", workflow)
        for run in re.findall(r"run: (\|\n(?:          .*\n)+|.*)", workflow):
            self.assertNotIn("${{ inputs.", run)
            self.assertNotIn("${{ github.event", run)
        self.assertEqual(workflow.count('elif [ -f fresh-eyes-source.zip ]'), 2)
        self.assertEqual(workflow.count('echo "SRC=." >> "$GITHUB_ENV"'), 2)
        self.assertIn("path: result/", workflow)


if __name__ == "__main__":
    unittest.main()
