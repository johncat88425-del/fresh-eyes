"""Public evaluation packaging with serialized synthetic fixtures, never real holdouts."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import test_evaluation as fixture
import test_demo as demo_fixture

demo = demo_fixture.demo
snapshots = demo.evaluation_recordings


class EvaluationDemoTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.EvaluationPipeline()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.approve_synthetic()
        self.f.run_infrastructure_fixture()
        self.project = self.f.project
        for name in ("web", "evidence"):
            # This suite creates its own synthetic holdout inventory and capture;
            # never borrow the owner's unrelated real evaluation snapshot.
            shutil.copytree(demo_fixture.PROJECT / name, self.project / name,
                            ignore=shutil.ignore_patterns("evaluation-recordings") if name == "web" else None)
        shutil.copytree(demo_fixture.PROJECT / "api/examples", self.project / "api/examples")
        # Development cases keep normal demo inputs; the holdout inventory is synthetic.
        for path in (demo_fixture.PROJECT / "cases").glob("*.json"):
            shutil.copy2(path, self.project / "cases" / path.name)
        self.results = self.project / "evaluation/results.json"
        fixture.export_results(self.project, self.f.manifest, self.f.frozen, self.f.records, self.results)
        self.out = self.f.root / "site"

    def test_exported_reports_open_and_snapshot_rebuilds_without_private_records(self):
        result = demo.build(self.project, self.out, evaluation_records=[self.f.records])
        self.assertEqual(result["recorded_runs"], 25)  # 24 saved real runs + 1 serialized synthetic record.
        data = json.loads(self.results.read_text())
        run_id = data["runs"][0]["run_id"]
        report = json.loads((self.out / f"data/runs/{run_id}.json").read_text())
        self.assertEqual(report["evaluation_group"], "A")
        self.assertEqual(report["result_status"], "infrastructure_error")
        self.assertFalse(report["costs"]["paid_calls_enabled"])
        self.assertFalse(report["case_available"])
        self.assertIn("result_review", report)  # Report UI uses this field, not the metrics' human_review.
        self.assertIsNone(report["result_review"])
        row = next(r for r in json.loads((self.out / "data/runs.json").read_text())["items"] if r["run_id"] == run_id)
        self.assertEqual(row["evaluation_group"], "A")
        self.assertIn("result_review", row)  # Beginner lists must retain the review field too.
        shutil.rmtree(self.f.records)
        self.results.unlink()
        rebuilt = demo.build(self.project, self.f.root / "offline-site")
        self.assertEqual(rebuilt["recorded_runs"], result["recorded_runs"])
        self.assertEqual(json.loads((self.f.root / "offline-site/evaluation.json").read_text()), data)

    def test_changed_report_stops_before_replacing_previous_demo(self):
        demo.build(self.project, self.out, evaluation_records=[self.f.records])
        before = (self.out / "data/manifest.json").read_bytes()
        report = next(self.f.records.glob("*/report.json"))
        report.write_bytes(report.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "hash or origin"):
            demo.build(self.project, self.out, replace=True, evaluation_records=[self.f.records])
        self.assertEqual((self.out / "data/manifest.json").read_bytes(), before)

    def test_changed_result_summary_cannot_be_published_as_original_evidence(self):
        body = json.loads(self.results.read_text())
        body["runs"][0]["duration_s"] = 999
        self.results.write_text(json.dumps(body))
        with self.assertRaisesRegex(ValueError, "differ from the evidence-bound export"):
            snapshots.collect(self.project, [self.f.records])

    def test_results_without_report_snapshot_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "openable captured reports"):
            demo.build(self.project, self.out)
        self.assertFalse(self.out.exists())

    def test_changed_snapshot_report_rejected_even_when_manifest_rehashed(self):
        demo.refresh_evaluation(self.project, [self.f.records])
        root = self.project / "web/demo/evaluation-recordings"
        manifest = json.loads((root / "manifest.json").read_text())
        row = manifest["runs"][0]
        report = root / row["evidence_file"]
        report.write_bytes(report.read_bytes() + b" ")
        manifest["files"][row["evidence_file"]] = hashlib.sha256(report.read_bytes()).hexdigest()
        (root / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "disagree"):
            snapshots.load(root)

    def test_fake_results_or_missing_evidence_cannot_enter_capture(self):
        with self.assertRaisesRegex(ValueError, "record directories"):
            snapshots.collect(self.project, [])
        body = json.loads(self.results.read_text())
        body["data_origin"] = "engineering_test"
        self.results.write_text(json.dumps(body))
        with self.assertRaisesRegex(ValueError, "Only formal evaluation"):
            snapshots.collect(self.project, [self.f.records])

    def test_invented_report_review_rejected_even_if_snapshot_is_rehashed(self):
        demo.refresh_evaluation(self.project, [self.f.records])
        root = self.project / "web/demo/evaluation-recordings"
        manifest = json.loads((root / "manifest.json").read_text())
        name = manifest["runs"][0]["file"]
        record = json.loads((root / name).read_text())
        record["result_review"] = {"verdict": "accepted", "reviewer": "SYNTHETIC FORGERY"}
        (root / name).write_text(json.dumps(record))
        manifest["files"][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        (root / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "disagree"):
            snapshots.load(root)
