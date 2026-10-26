"""Synthetic post-run review data only; no human attribution is created."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from fresh_eyes.contracts import InvalidCase
from fresh_eyes.result_reviews import template, apply_reviews


class ResultReviews(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "reviews.json"
        self.result = {"freeze_sha256": "a" * 64, "runs": [{"case_id": "synthetic-case", "group": "C",
            "run_id": "b" * 32, "acceptance_sha256": "c" * 64, "report_sha256": "d" * 64,
            "run_state": "completed", "result_status": "verified_fix", "human_review": None}]}

    def save(self, body):
        self.path.write_text(json.dumps(body))

    def approved(self, verdict="accepted"):
        body = template(self.result)
        body["reviews"][0].update(verdict=verdict, reviewer="SYNTHETIC REVIEW FIXTURE",
                                  reviewed_at="2026-10-20T12:00:00+01:00", note="Synthetic test only")
        return body

    def test_draft_template_never_approves_and_includes_only_claimed_fixes(self):
        self.result["runs"].append({**self.result["runs"][0], "run_id": "e" * 32, "result_status": "already_passes"})
        body = template(self.result)
        self.assertEqual(len(body["reviews"]), 1)
        self.save(body)
        apply_reviews(self.result, self.path)
        self.assertTrue(all(r["human_review"] is None for r in self.result["runs"]))

    def test_accepted_and_rejected_are_attached_without_rewriting_engine_result(self):
        for verdict in ("accepted", "rejected"):
            self.save(self.approved(verdict))
            apply_reviews(self.result, self.path)
            r = self.result["runs"][0]
            self.assertEqual(r["human_review"]["verdict"], verdict)
            self.assertEqual(r["human_review"]["reviewer"], "SYNTHETIC REVIEW FIXTURE")
            self.assertEqual(r["result_status"], "verified_fix")

    def test_identity_hash_freeze_and_duplicates_are_rejected(self):
        original = self.approved()
        for key in ("case_id", "group", "run_id", "acceptance_sha256", "report_sha256"):
            body = copy.deepcopy(original)
            body["reviews"][0][key] = "wrong"
            self.save(body)
            with self.assertRaises(InvalidCase):
                apply_reviews(self.result, self.path)
        body = copy.deepcopy(original)
        body["freeze_sha256"] = "wrong"
        self.save(body)
        with self.assertRaises(InvalidCase):
            apply_reviews(self.result, self.path)
        body = copy.deepcopy(original)
        body["reviews"] *= 2
        self.save(body)
        with self.assertRaises(InvalidCase):
            apply_reviews(self.result, self.path)

    def test_missing_reviewer_timezone_or_invalid_verdict_is_rejected(self):
        for key, value in (("reviewer", ""), ("reviewed_at", "2026-10-20T12:00:00"), ("verdict", "pass")):
            body = self.approved()
            body["reviews"][0][key] = value
            self.save(body)
            with self.assertRaises(ValueError):
                apply_reviews(self.result, self.path)
