"""Cross-machine transport tests use synthetic reviewers/cases, never real approvals."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fresh_eyes.api import Service
from fresh_eyes.approval_io import export_approvals, import_approvals
from fresh_eyes.contracts import digest
from test_core import case_data


class ApprovalTransport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "cases").mkdir(parents=True)
        self.data = case_data("a" * 40)
        self.path = self.project / "cases/unit.json"
        self.path.write_text(json.dumps(self.data))
        self.source = Service(self.project, self.root / "machine-b")
        self.addCleanup(self.source.close)
        self.case_id = self.data["case_id"]
        self.acceptance = self.source.contract(self.case_id).acceptance_sha256
        self.file = self.root / "approval-transfer.json"

    def export(self):
        self.source.approve(self.case_id, {"reviewed_by": "SYNTHETIC TEST REVIEWER",
                                          "acceptance_sha256": self.acceptance})
        export_approvals(self.project, self.root / "machine-b", self.file)
        return json.loads(self.file.read_text())

    def rewrite(self, payload):
        payload["payload_sha256"] = digest(payload["approvals"])
        self.file.write_text(json.dumps(payload))

    def test_round_trip_updates_running_api_without_changing_contract_or_original_reviewer_time(self):
        before = self.path.read_bytes()
        payload = self.export()
        target = Service(self.project, self.root / "machine-a")
        self.addCleanup(target.close)
        preview = import_approvals(self.project, target.data_dir, self.file)
        self.assertEqual(preview["would_import_count"], 1)
        self.assertNotEqual(target.case_view(self.case_id)["review"]["status"], "approved")
        applied = import_approvals(self.project, target.data_dir, self.file, apply=True)
        self.assertEqual(applied["imported_count"], 1)
        review = target.case_view(self.case_id)["review"]
        self.assertEqual(review["reviewed_by"], payload["approvals"][0]["reviewed_by"])
        self.assertEqual(review["approved_at"], payload["approvals"][0]["approved_at"])
        self.assertFalse(review["transfer"]["identity_verified"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(target.store.records(), [])
        again = import_approvals(self.project, target.data_dir, self.file, apply=True)
        self.assertEqual(again["imported_count"], 0)
        self.assertEqual(again["unchanged_count"], 1)

    def test_dry_run_and_empty_export_do_not_create_a_database_or_promote_development_review(self):
        missing = self.root / "not-started"
        result = export_approvals(self.project, missing, self.file)
        self.assertEqual(result["approval_count"], 0)
        self.assertFalse(missing.exists())
        self.export()
        result = import_approvals(self.project, missing, self.file)
        self.assertFalse(result["applied"])
        self.assertEqual(result["would_import_count"], 1)
        self.assertFalse(missing.exists())

    def test_changed_repo_commit_or_acceptance_is_rejected_even_with_recomputed_checksum(self):
        payload = self.export()
        for field, value in [("repo_url", "https://github.com/other/repo"), ("commit_sha", "b" * 40),
                             ("acceptance_sha256", "0" * 64)]:
            with self.subTest(field=field):
                changed = copy.deepcopy(payload)
                changed["approvals"][0][field] = value
                self.rewrite(changed)
                target = self.root / ("target-" + field)
                with self.assertRaisesRegex(ValueError, "changed"):
                    import_approvals(self.project, target, self.file, apply=True)
                self.assertFalse(target.exists())

    def test_corrupt_duplicate_or_unattributed_records_are_rejected(self):
        payload = self.export()
        payload["payload_sha256"] = "0" * 64
        self.file.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "checksum"):
            import_approvals(self.project, self.root / "target", self.file)
        payload = self.export()
        changed = copy.deepcopy(payload)
        changed["approvals"].append(copy.deepcopy(changed["approvals"][0]))
        self.rewrite(changed)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            import_approvals(self.project, self.root / "target", self.file)
        for field, value in [("reviewed_by", ""), ("approved_at", "2026-10-19T12:00:00"),
                             ("scope", "upstream_review"), ("status", "development_reviewed")]:
            with self.subTest(field=field):
                changed = copy.deepcopy(payload)
                changed["approvals"][0][field] = value
                self.rewrite(changed)
                with self.assertRaises(ValueError):
                    import_approvals(self.project, self.root / "target", self.file, apply=True)
        self.assertFalse((self.root / "target").exists())

    def test_conflicting_target_review_is_not_overwritten(self):
        self.export()
        target = Service(self.project, self.root / "machine-a")
        self.addCleanup(target.close)
        target.approve(self.case_id, {"reviewed_by": "OTHER SYNTHETIC REVIEWER", "acceptance_sha256": self.acceptance})
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            import_approvals(self.project, target.data_dir, self.file, apply=True)
        self.assertEqual(target.case_view(self.case_id)["review"]["reviewed_by"], "OTHER SYNTHETIC REVIEWER")


if __name__ == "__main__":
    unittest.main()
