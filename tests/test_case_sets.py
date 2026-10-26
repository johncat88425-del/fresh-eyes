"""Freeze integrity checks never execute or approve held-out workflows."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fresh_eyes.case_sets import verify_holdout
from fresh_eyes.contracts import Contract, InvalidCase
from test_core import case_data


class FrozenCaseSets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.case = self.root / "unit.json"
        data = case_data()
        data["review"] = {"status": "draft"}
        self.case.write_text(json.dumps(data))
        contract = Contract.from_dict(data)
        self.entry = {"file": "unit.json", "file_sha256": hashlib.sha256(self.case.read_bytes()).hexdigest(),
                      "case_id": data["case_id"], "contract_sha256": contract.sha256,
                      "acceptance_sha256": contract.acceptance_sha256}
        self.manifest = self.root / "freeze.json"
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps({"schema_version": 1, "kind": "holdout", "cases": [self.entry]}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_verified_hashes_do_not_approve_or_execute_a_draft(self):
        report = verify_holdout(self.manifest)
        self.assertTrue(report["integrity_verified"])
        self.assertEqual(report["case_count"], 1)
        self.assertEqual(report["cases"][0]["review_status"], "draft")
        self.assertFalse(report["executed"])
        self.assertFalse(report["human_approval_granted"])

    def test_changed_bytes_or_acceptance_are_rejected(self):
        data = json.loads(self.case.read_text())
        data["fixtures"][0]["expected"] = {"n": 999}
        self.case.write_text(json.dumps(data))
        with self.assertRaisesRegex(InvalidCase, "bytes changed"):
            verify_holdout(self.manifest)
        self.entry["file_sha256"] = hashlib.sha256(self.case.read_bytes()).hexdigest()
        self.entry["contract_sha256"] = Contract.from_dict(data).sha256
        self.write_manifest()
        with self.assertRaisesRegex(InvalidCase, "identity or acceptance"):
            verify_holdout(self.manifest)

    def test_missing_or_extra_case_cannot_change_the_denominator_silently(self):
        extra = self.root / "extra.json"
        extra.write_bytes(self.case.read_bytes())
        with self.assertRaisesRegex(InvalidCase, "frozen inventory"):
            verify_holdout(self.manifest)
        extra.unlink()
        self.case.unlink()
        with self.assertRaisesRegex(InvalidCase, "missing"):
            verify_holdout(self.manifest)

    def test_manifest_cannot_escape_its_directory_or_follow_a_case_symlink(self):
        self.entry["file"] = "../unit.json"
        self.write_manifest()
        with self.assertRaisesRegex(InvalidCase, "basenames"):
            verify_holdout(self.manifest)
        self.entry["file"] = "unit.json"
        self.write_manifest()
        target = self.root / "original.txt"
        target.write_bytes(self.case.read_bytes())
        self.case.unlink()
        self.case.symlink_to(target)
        with self.assertRaisesRegex(InvalidCase, "regular"):
            verify_holdout(self.manifest)


if __name__ == "__main__":
    unittest.main()
