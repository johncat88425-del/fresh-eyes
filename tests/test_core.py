"""Invariant tests use our own synthetic repo, never third-party code on the host."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from fresh_eyes.contracts import Contract, InvalidCase, relative_path
from fresh_eyes.engine import run_case
from fresh_eyes.patches import PatchMismatch, apply_patch
from fresh_eyes.runner import DockerRunner, InfrastructureError
from fresh_eyes.validators import evaluate


def case_data(sha="a" * 40):
    return {
        "schema_version": 1, "case_id": "synthetic-unit-case", "repo": {"url": "https://example.invalid/unit", "commit": sha},
        "environment": {"platform": "linux/arm64"}, "goal": "Return the input JSON accurately", "readme_source": "README.md:L1",
        "review": {"status": "development_reviewed", "reviewed_by": "unit test"},
        "setup": [{"argv": ["pip", "install", "."], "source": "README.md:L1"}],
        "workflow": {"argv": ["unit-tool", "{input}"], "output": "stdout"},
        "fixtures": [{"name": "one.json", "input": '{"n":1}', "validator": "json", "expected": {"n": 1}},
                     {"name": "two.json", "input": '{"n":2}', "validator": "json", "expected": {"n": 2}}],
    }


def result(ok=True):
    return {"setup_ok": True, "steps": [], "outputs": [
        {"name": "one.json", "exit_code": 0, "content": '{"n":1}' if ok else '{}', "truncated": False},
        {"name": "two.json", "exit_code": 0, "content": '{"n":2}' if ok else '{}', "truncated": False},
    ]}


class ContractsAndValidation(unittest.TestCase):
    def test_contract_copy_cannot_mutate_acceptance(self):
        c = Contract.from_dict(case_data())
        digest = c.acceptance_sha256
        d = c.data
        d["fixtures"][0]["expected"] = {"n": 99}
        self.assertEqual(c.acceptance_sha256, digest)
        self.assertEqual(c.data["fixtures"][0]["expected"], {"n": 1})

    def test_expected_results_never_enter_execution_payload(self):
        payload = Contract.from_dict(case_data()).execution_payload()
        self.assertNotIn("expected", json.dumps(payload))
        self.assertNotIn("validator", json.dumps(payload))

    def test_draft_cannot_execute(self):
        d = case_data(); d["review"]["status"] = "draft"
        with self.assertRaises(InvalidCase):
            Contract.from_dict(d).check_review(development=True)

    def test_development_review_is_not_maintainer_approval(self):
        c = Contract.from_dict(case_data())
        with self.assertRaises(InvalidCase): c.check_review()
        c.check_review(development=True)

    def test_full_commit_required(self):
        d = case_data("main")
        with self.assertRaises(InvalidCase): Contract.from_dict(d)

    def test_paths_cannot_escape_workspace(self):
        for p in ["../secret", "/etc/passwd", "a/../../x", "a\\..\\x", ".", "a/./b"]:
            with self.subTest(path=p), self.assertRaises(InvalidCase): relative_path(p)

    def test_shell_install_and_index_override_are_rejected(self):
        for argv in [["sh", "-c", "true"], ["pip", "install", "--index-url=https://example.invalid", "x"], ["python", "-c", "print(1)"]]:
            d = case_data(); d["setup"][0]["argv"] = argv
            with self.subTest(argv=argv), self.assertRaises(InvalidCase): Contract.from_dict(d)

    def test_exit_zero_with_wrong_output_fails(self):
        self.assertFalse(evaluate(Contract.from_dict(case_data()), result(False))["passed"])

    def test_all_fixtures_must_pass(self):
        r = result(); r["outputs"].pop()
        self.assertFalse(evaluate(Contract.from_dict(case_data()), r)["passed"])

    def test_truncated_output_does_not_pass(self):
        r = result(); r["outputs"][1]["truncated"] = True
        self.assertFalse(evaluate(Contract.from_dict(case_data()), r)["passed"])

    def test_valid_json_output_passes(self):
        self.assertTrue(evaluate(Contract.from_dict(case_data()), result())["passed"])

    def test_json_boolean_is_not_the_number_one(self):
        r = result(); r["outputs"][0]["content"] = '{"n":true}'
        self.assertFalse(evaluate(Contract.from_dict(case_data()), r)["passed"])

    def test_text_lines_ignores_blank_lines_and_trailing_space_but_not_wording_or_order(self):
        d = case_data()
        for f in d["fixtures"]:
            f.update(validator="text_lines", expected="# Hello\n\nA **bold** start.")
        r = result()
        for content, ok in [("# Hello\nA **bold** start.\n", True), ("\n\n# Hello  \n\n\n\nA **bold** start.\n\n", True),
                            ("# Hello\n\nA **bold** start.", True), ("# Hello\n\nA **bold** start", False),
                            ("# Hello\n\nA *bold* start.", False),
                            ("A **bold** start.\n\n# Hello", False), ("# Hello\n\nA **bold** start.\n\nExtra line", False),
                            ("  # Hello\n\nA **bold** start.", False)]:
            for f in r["outputs"]: f["content"] = content
            with self.subTest(content=content):
                self.assertEqual(evaluate(Contract.from_dict(d), r)["passed"], ok)

    def test_text_lines_expectation_must_be_a_string(self):
        d = case_data()
        d["fixtures"][0].update(validator="text_lines", expected=["# Hello"])
        with self.assertRaisesRegex(InvalidCase, "Text expectations must be strings"):
            Contract.from_dict(d)

    def test_html_requires_expected_elements(self):
        d = case_data()
        for f in d["fixtures"]:
            f.update(validator="html_elements", expected=[["h1", "Hello"], ["strong", "world"]])
        r = result()
        for f in r["outputs"]: f["content"] = '<h1>Hello</h1><p><strong>world</strong></p>'
        self.assertTrue(evaluate(Contract.from_dict(d), r)["passed"])
        r["outputs"][0]["content"] = '<h1>Hello</h1>'
        self.assertFalse(evaluate(Contract.from_dict(d), r)["passed"])


class PatchPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "README.md").write_text("pip install old\n")
        (self.root / "main.py").write_text("print('original')\n")
        (self.root / "pyproject.toml").write_text('[project]\nname="unit"\ndependencies=["old"]\n[project.scripts]\nunit="unit:main"\n')

    def tearDown(self): self.tmp.cleanup()

    def test_source_changes_rejected(self):
        with self.assertRaises(InvalidCase):
            apply_patch(self.root, {"edits": [{"path": "main.py", "old": "original", "new": "fake"}]})

    def test_new_shell_wrapper_rejected(self):
        with self.assertRaises(InvalidCase):
            apply_patch(self.root, {"edits": [{"path": "run.sh", "old": "x", "new": "true"}]})

    def test_entrypoint_changes_rejected(self):
        with self.assertRaises(InvalidCase):
            apply_patch(self.root, {"edits": [{"path": "pyproject.toml", "old": 'unit="unit:main"', "new": 'unit="fake:main"'}]})

    def test_dependency_edit_allowed(self):
        diff = apply_patch(self.root, {"edits": [{"path": "pyproject.toml", "old": '["old"]', "new": '["new"]'}]})
        self.assertIn('+dependencies=["new"]', diff)

    def test_invalid_toml_is_rejected_without_mutating_either_target(self):
        before=(self.root/'pyproject.toml').read_bytes()
        with self.assertRaisesRegex(InvalidCase,'valid TOML'):
            apply_patch(self.root,{'edits':[{'path':'README.md','old':'old','new':'new'},
                {'path':'pyproject.toml','old':'["old"]','new':'["unterminated'}]})
        self.assertEqual((self.root/'pyproject.toml').read_bytes(),before)
        self.assertEqual((self.root/'README.md').read_text(),'pip install old\n')

    def test_patch_is_atomic_when_later_edit_invalid(self):
        with self.assertRaises(InvalidCase):
            apply_patch(self.root, {"edits": [{"path": "README.md", "old": "old", "new": "new"}, {"path": "main.py", "old": "original", "new": "fake"}]})
        self.assertEqual((self.root / "README.md").read_text(), "pip install old\n")

    def test_line_edit_replaces_named_lines_and_ignores_spacing_only(self):
        (self.root / "README.md").write_text("# Unit\n\n```bash\npip install -r requirements.txt\n```\nrun unit\n")
        diff = apply_patch(self.root, {"edits": [{"path": "README.md", "start": 4, "end": 4,
                                                  "old": "  pip install  -r requirements.txt", "new": "pip install ."}]})
        self.assertEqual((self.root / "README.md").read_text(), "# Unit\n\n```bash\npip install .\n```\nrun unit\n")
        self.assertIn("+pip install .", diff)

    def test_line_edit_on_wrong_lines_is_revisable_and_reports_what_they_say(self):
        with self.assertRaisesRegex(PatchMismatch, "pip install old"):
            apply_patch(self.root, {"edits": [{"path": "README.md", "start": 1, "end": 1, "old": "something else", "new": "x"}]})
        self.assertEqual((self.root / "README.md").read_text(), "pip install old\n")

    def test_line_edits_use_original_numbers_and_may_not_overlap(self):
        (self.root / "README.md").write_text("a\nb\nc\nd\n")
        apply_patch(self.root, {"edits": [{"path": "README.md", "start": 1, "end": 1, "old": "a", "new": "a1\na2"},
                                          {"path": "README.md", "start": 3, "end": 3, "old": "c", "new": "c1"}]})
        self.assertEqual((self.root / "README.md").read_text(), "a1\na2\nb\nc1\nd\n")
        with self.assertRaisesRegex(InvalidCase, "overlap"):
            apply_patch(self.root, {"edits": [{"path": "README.md", "start": 1, "end": 3, "old": "a1 a2 b", "new": "x"},
                                              {"path": "README.md", "start": 3, "end": 4, "old": "b c1", "new": "y"}]})

    def test_wrong_line_redacts_bare_control_secrets_before_truncation(self):
        secret = "synthetic-bare-controller-secret"
        line = "x" * 290 + secret + " trailing text\n"
        (self.root / "README.md").write_text(line)
        with self.assertRaises(PatchMismatch) as caught:
            apply_patch(self.root, {"edits": [{"path": "README.md", "start": 1, "end": 1,
                "old": "wrong quote", "new": "replacement"}]}, secrets=(secret,))
        message = str(caught.exception)
        self.assertNotIn(secret[:10], message)
        self.assertIn("[REDACTED]", message)
        self.assertEqual((self.root / "README.md").read_text(), line)

    def test_line_edit_limits(self):
        cases = [
            ({"path": "README.md", "start": 2, "end": 2, "old": "x", "new": "y"}, "outside"),
            ({"path": "README.md", "start": True, "end": 1, "old": "pip install old", "new": "y"}, "outside"),
            ({"path": "main.py", "start": 1, "end": 1, "old": "print('original')", "new": "fake"}, "outside prototype scope"),
        ]
        for edit, message in cases:
            with self.subTest(edit=edit), self.assertRaisesRegex(InvalidCase, message):
                apply_patch(self.root, {"edits": [edit]})
        with self.assertRaisesRegex(InvalidCase, "all quote exact text or all name lines"):
            apply_patch(self.root, {"edits": [{"path": "README.md", "start": 1, "end": 1, "old": "pip install old", "new": "a"},
                                              {"path": "README.md", "old": "old", "new": "b"}]})
        self.assertEqual((self.root / "main.py").read_text(), "print('original')\n")

    def test_hidden_setup_step_rejected(self):
        with self.assertRaises(InvalidCase):
            apply_patch(self.root, {"edits": [{"path": "README.md", "old": "old", "new": "new"}],
                                    "setup": [{"argv": ["pip", "install", "hidden"], "source": "README.md:L1"}]})


class ScriptedRunner:
    """Test double, not an environment, model, or benchmark result."""
    def __init__(self, verdicts): self.verdicts = iter(verdicts); self.payloads = []
    def preflight(self): return {"test_double": True, "network": "none"}
    def run(self, bundle):
        self.payloads.append(json.loads((bundle / "payload.json").read_text()))
        return result(next(self.verdicts))


class EngineInvariants(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "source"; self.source.mkdir()
        (self.source / "README.md").write_text("pip install old\n")
        for argv in [["git", "init", "-q", str(self.source)],
                     ["git", "-C", str(self.source), "add", "README.md"],
                     ["git", "-C", str(self.source), "-c", "user.name=Fresh Eyes Tests", "-c", "user.email=tests@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "synthetic test input"]]:
            subprocess.run(argv, check=True, capture_output=True)
        sha = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        self.contract = Contract.from_dict(case_data(sha))
        self.wheels = self.root / "wheels"; self.wheels.mkdir()
        self.patch = {"edits": [{"path": "README.md", "old": "old", "new": "new"}]}

    def tearDown(self): self.tmp.cleanup()

    def execute(self, runner, patch=None):
        return run_case(self.contract, self.source, self.wheels, self.root / "report", runner=runner, patch=patch, development=True)

    def test_final_clean_run_is_required(self):
        runner = ScriptedRunner([False, True, False])
        report = self.execute(runner, self.patch)
        self.assertEqual(report["status"], "unresolved")
        self.assertEqual(len(report["attempts"]), 3)
        self.assertEqual(len({a["acceptance_sha256"] for a in report["attempts"]}), 1)

    def test_both_clean_runs_needed_for_verified_fix(self):
        runner = ScriptedRunner([False, True, True])
        report = self.execute(runner, self.patch)
        self.assertEqual(report["status"], "verified_fix")
        self.assertEqual(report["repair_author"], "manual")
        self.assertEqual(report["model_calls"], 0)
        self.assertTrue(all("expected" not in json.dumps(p) for p in runner.payloads))

    def test_healthy_repo_does_not_get_patch(self):
        report = self.execute(ScriptedRunner([True]), self.patch)
        self.assertEqual(report["status"], "already_passes")
        self.assertNotIn("patch", report)

    def test_unavailable_runtime_is_not_reported_as_repo_failure(self):
        class Missing:
            def preflight(self): raise InfrastructureError("No container runtime")
        report = self.execute(Missing())
        self.assertEqual(report["status"], "infrastructure_error")
        self.assertEqual(report["attempts"], [])
        self.assertTrue((self.root / "report/report.html").exists())

    def test_source_commit_mismatch_is_rejected(self):
        wrong = Contract.from_dict(case_data("a" * 40))
        with self.assertRaises(InvalidCase):
            run_case(wrong, self.source, self.wheels, self.root / "report", runner=ScriptedRunner([]), development=True)

    def test_rejected_patch_preserves_baseline_evidence(self):
        report = self.execute(ScriptedRunner([False]), {"edits": [{"path": "run.sh", "old": "x", "new": "true"}]})
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(len(report["attempts"]), 1)
        self.assertEqual(report["attempts"][0]["phase"], "original_readme")

    def test_worker_bootstrap_failure_keeps_evidence_and_is_not_a_repo_fault(self):
        class BadEnvironment(ScriptedRunner):
            def run(self, bundle):
                return {"setup_ok": False, "outputs": [], "steps": [], "infrastructure_error": "Wrong Python prefix"}
        report = self.execute(BadEnvironment([]))
        self.assertEqual(report["status"], "infrastructure_error")
        self.assertEqual(report["attempts"][0]["status"], "infrastructure_error")

    def test_progress_observer_cannot_mutate_acceptance_or_final_report(self):
        observed = []
        def progress(report):
            observed.append(report["current_phase"])
            report["acceptance_sha256"] = "tampered"
            report["attempts"].clear()
        report = run_case(self.contract, self.source, self.wheels, self.root / "report",
                          runner=ScriptedRunner([True]), development=True, progress=progress)
        self.assertEqual(report["acceptance_sha256"], self.contract.acceptance_sha256)
        self.assertEqual(len(report["attempts"]), 1)
        self.assertIn("original_readme", observed)
        self.assertEqual(observed[-1], "finished")


class ContainerBoundaries(unittest.TestCase):
    def test_staging_is_never_executed_and_active_container_is_offline(self):
        runner = DockerRunner()
        runner._image_id = "sha256:base"
        calls = []
        def fake_call(*args, **kwargs):
            calls.append(args)
            if args[0] == "commit": return "sha256:prepared"
            if args[0] == "start": return json.dumps(result())
            return ""
        runner._call = fake_call
        with tempfile.TemporaryDirectory() as temp:
            runner.run(Path(temp))
        active = next(c for c in calls if c[0] == "create" and "--read-only" in c)
        self.assertIn("none", active)
        self.assertIn("65532:65532", active)
        self.assertIn("sha256:prepared", active)
        workspace_mount = next(arg for arg in active if arg.startswith("/work:"))
        self.assertIn("exec", workspace_mount.split(":", 1)[1].split(","))
        self.assertNotIn("noexec", workspace_mount.split(":", 1)[1].split(","))
        self.assertNotIn("--privileged", active)
        self.assertFalse(any(c[0] == "start" and c[-1].endswith("-staging") for c in calls))
        self.assertEqual(sum(c[0] == "start" for c in calls), 1)
        self.assertIn(("image", "rm", "--no-prune", "sha256:prepared"), calls)

    def test_timeout_removes_both_containers_and_prepared_image(self):
        runner = DockerRunner()
        runner._image_id = "sha256:base"
        calls = []
        def fake_call(*args, **kwargs):
            calls.append(args)
            if args[0] == "commit": return "sha256:prepared"
            if args[0] == "start": raise InfrastructureError("Timeout")
            return ""
        runner._call = fake_call
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(InfrastructureError):
            runner.run(Path(temp))
        self.assertEqual(sum(c[0] == "rm" for c in calls), 2)
        self.assertIn(("image", "rm", "--no-prune", "sha256:prepared"), calls)


if __name__ == "__main__": unittest.main()
