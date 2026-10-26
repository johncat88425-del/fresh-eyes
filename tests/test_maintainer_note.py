"""Maintainer notes stay drafts built on verified evidence; synthetic model responses only."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fresh_eyes.maintainer_note import MaintainerNoteWriter, evidence_for, validate_note
from fresh_eyes.models import ModelCaller, ModelError, ModelLedger
import test_models as model_tests

ROOT = Path(__file__).resolve().parents[1]
DIFF = "--- a/README.md\n+++ b/README.md\n@@ -1,3 +1,3 @@\n ```bash\n-pip install -r requirements.txt\n+pip install .\n ```\n"


def report():
    failed = {"phase": "original_readme", "status": "failed",
              "result": {"steps": [{"argv": ["/usr/local/bin/python", "-m", "venv", "/work/venv"], "exit_code": 0},
                                   {"argv": ["pip", "install", "-r", "requirements.txt"], "exit_code": 0}],
                         "outputs": [{"name": "intro.md", "exit_code": 127,
                                      "process": {"argv": ["tool", "/work/inputs/intro.md"], "stderr": "No such file: 'tool' API_KEY=leak"}}]},
              "validation": {"passed": False, "checks": [{"name": "intro.md", "passed": False}]}}
    return {"run_id": "synthetic-run", "status": "verified_fix", "goal": "Convert a note", "repair_author": "manual",
            "repo": {"url": "https://github.com/unit/project", "commit": "a" * 40},
            "attempts": [failed, {"phase": "candidate_patch", "status": "passed"}, {"phase": "fresh_verification", "status": "passed"}],
            "candidate_patch": {"patch": {"hypothesis": "Install the project so its CLI exists."}},
            "patch": {"diff": DIFF, "sha256": "b" * 64}}


NOTE = {"title": "README install step does not create the tool command",
        "body": "Following the install section on a clean machine installs the requirements, but the tool command "
                "is never created, so the usage example fails. Installing the project itself fixes it."}


class MaintainerNoteTests(unittest.TestCase):
    def test_only_a_fix_proven_by_two_clean_runs_can_be_described(self):
        self.assertEqual(evidence_for(report())["readme_commands_as_written"],
                         [{"command": "pip install -r requirements.txt", "exit_code": 0}])
        for mutate in (lambda r: r.update(status="unresolved"),
                       lambda r: r["attempts"][2].update(status="failed"),
                       lambda r: r["attempts"].pop(2),
                       lambda r: r["patch"].update(diff=""),
                       lambda r: r["patch"].update(diff="x" * 7000)):
            value = report(); mutate(value)
            with self.subTest(), self.assertRaises(ModelError):
                evidence_for(value)

    def test_evidence_is_scrubbed_before_the_model_sees_it(self):
        self.assertNotIn("leak", json.dumps(evidence_for(report())))

    def test_links_mentions_fences_headings_and_bad_lengths_are_refused(self):
        self.assertEqual(validate_note(json.dumps(NOTE))["title"], NOTE["title"])
        for bad in ({**NOTE, "body": NOTE["body"] + " See https://example.com"}, {**NOTE, "body": NOTE["body"] + " cc @owner"},
                    {**NOTE, "body": "```\npip install .\n```\n" + NOTE["body"]}, {**NOTE, "body": "# Fix\n" + NOTE["body"]},
                    {**NOTE, "title": "Fix"}, {**NOTE, "title": "two\nlines here"}, {**NOTE, "body": "short"},
                    {**NOTE, "extra": "field"}, {"title": NOTE["title"]}):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                validate_note(json.dumps(bad))

    def test_writer_attaches_the_exact_diff_and_stays_an_unposted_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = model_tests.SyntheticTransport([NOTE])
            caller = ModelCaller(transport, ModelLedger(Path(tmp) / "ledger.sqlite3", "note-run", "1"))
            raw = json.dumps(report()).encode()
            note = MaintainerNoteWriter(caller, model_tests.spec()).write(report(), raw)
        self.assertEqual((note["status"], note["posted"], note["human_review_required"]), ("draft", False, True))
        self.assertEqual(note["diff"], DIFF)
        self.assertIn("```diff\n" + DIFF.rstrip(), note["markdown"])
        self.assertIn("written by a person", note["footer"])
        self.assertEqual(note["ledger"]["calls"][0]["stage"], "maintainer_note")
        sent = json.loads(transport.payloads[0]["messages"][1]["content"])["evidence"]
        self.assertEqual(sent["verified_change"], DIFF)

    def test_cli_preview_reads_no_key_and_refuses_a_non_fix_before_any_call(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps(report()))
            base = [sys.executable, "-B", "-m", "fresh_eyes", "maintainer-note", "--report", str(path),
                    "--model-config", str(ROOT / "evaluation/models-development-output4096-20261022.json"), "--output", str(Path(tmp) / "out")]
            preview = subprocess.run(base, capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(json.loads(preview.stdout)["key_read"], False)
            self.assertFalse((Path(tmp) / "out").exists())
            bad = copy.deepcopy(report()); bad["status"] = "unresolved"
            path.write_text(json.dumps(bad))
            refused = subprocess.run(base + ["--enable-paid-models", "--budget-usd", "1", "--env-file", str(Path(tmp) / "missing.env"),
                                             "--tokenizer-references", tmp], capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("verified fix", refused.stderr)
            self.assertFalse((Path(tmp) / "out").exists())


if __name__ == "__main__":
    unittest.main()
