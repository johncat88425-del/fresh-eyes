"""Boundary entry uses synthetic source and runner; real cases are never executed."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import test_model_trial as fixture
from fresh_eyes.boundary import plan, review_template, run
from fresh_eyes.contracts import Contract, InvalidCase
from test_core import ScriptedRunner


class BoundaryEntry(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ModelTrialTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        root = f.project / "cases/boundary"
        root.mkdir()
        data = f.contract.data
        data["review"] = {"status": "draft"}
        self.case = root / "unit.json"
        self.case.write_text(json.dumps(data))
        c = Contract.load(self.case)
        self.manifest = root / "expected.json"
        self.manifest.write_text(json.dumps({"schema_version": 1, "kind": "boundary", "cases": [{
            "file": "unit.json", "case_id": data["case_id"], "file_sha256": hashlib.sha256(self.case.read_bytes()).hexdigest(),
            "contract_sha256": c.sha256, "acceptance_sha256": c.acceptance_sha256,
            "expected_result_status": ["unresolved", "unsupported"], "counted_in_fix_rate": False}]}))
        self.saved = plan(f.project, self.manifest, "sha256:" + "1" * 64)
        self.path, self.reviews, self.output = f.root / "boundary-plan.json", f.root / "boundary-reviews.json", f.root / "boundary-output"
        self.path.write_text(json.dumps(self.saved))
        self.reviews.write_text(json.dumps(review_template(self.saved)))
        self.sources = f.root / "sources"
        self.sources.mkdir()
        f.source.rename(self.sources / "unit")

    def test_plan_is_integrity_only_and_draft_review_blocks_before_execution(self):
        runner = Mock()
        with self.assertRaisesRegex(InvalidCase, "human approval"):
            run(self.fixture.project, self.manifest, self.path, self.reviews, self.sources, self.output, runner_factory=runner)
        runner.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.saved["executed"])
        self.assertFalse(self.saved["counted_in_fix_rate"])

    def test_changed_contract_or_code_cannot_use_old_plan(self):
        self.case.write_bytes(self.case.read_bytes() + b" ")
        with self.assertRaises(InvalidCase):
            plan(self.fixture.project, self.manifest, self.saved["runner_id"])

    def test_synthetic_baseline_is_separate_from_model_repair_and_scores(self):
        body = json.loads(self.reviews.read_text())
        body["approvals"][0].update(status="approved", reviewed_by="SYNTHETIC REVIEW FIXTURE", approved_at="2026-10-20T12:00:00Z")
        self.reviews.write_text(json.dumps(body))
        def runner(contract):
            result = ScriptedRunner([False])
            result.dependency_mode = "pypi_live"
            return result
        result = run(self.fixture.project, self.manifest, self.path, self.reviews, self.sources, self.output, runner_factory=runner)
        row = result["cases"][0]
        self.assertEqual(row["result_status"], "unresolved")
        self.assertEqual(row["data_origin"], "engineering_test")
        self.assertTrue(row["expected_status_match"])
        self.assertFalse(result["counted_in_fix_rate"])
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse((self.output / row["case_id"] / "candidate.diff").exists())
