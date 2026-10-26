"""Portable report regressions: broken links must stop packaging."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from fresh_eyes.handoff import report_files, validate_links, write_archive


class PortableReports(unittest.TestCase):
    def test_live_json2yaml_complete_report_is_portable_and_mismatch_stops_packaging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pilot = root / "reports/live-json2yaml-synthetic"
            pilot.mkdir(parents=True)
            report = {"run_id": "synthetic-json2yaml", "status": "already_passes", "model_calls": 0,
                      "dependency_mode": "pypi_live", "finished_at": "2026-10-20T16:00:00Z"}
            record = {"case_id": "json2yaml-yaml2json", "data_origin": "execution", "run_state": "completed",
                      "evaluation_eligible": False, "run_id": report["run_id"], "result_status": report["status"], "evidence": report}
            (pilot / "report.json").write_text(json.dumps(report))
            (pilot / "run-record.json").write_text(json.dumps(record))
            (pilot / "report.html").write_text("<p>SYNTHETIC serialized fixture, not a real execution</p>")
            files = report_files(root)
            self.assertIn("reports/live-json2yaml-synthetic/report.html", files)
            record["result_status"] = "verified_fix"
            (pilot / "run-record.json").write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "disagree"):
                report_files(root)

    def test_development_trial_is_packaged_with_identity_checks_and_no_private_state(self):
        # Serialized execution-shaped fixture only; no runner/provider is used.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trial = root / "reports/model-trial-synthetic"
            trial.mkdir(parents=True)
            report = {"run_id": "SYNTHETIC-RECORDED-FIXTURE", "status": "already_passes", "model_calls": 0,
                      "repair_route": "nano_ultra", "max_patch_rounds": 2, "dependency_mode": "pypi_live"}
            path = trial / "report.json"
            path.write_text(json.dumps(report))
            record = {"run_id": report["run_id"], "result_status": report["status"], "run_state": "completed",
                      "evaluation_eligible": False, "data_origin": "execution", "evidence": report, "costs": {"model_calls": []}}
            binding = {"scope": "development", "evaluation_eligible": False, "run_state": "completed",
                       "run_id": report["run_id"], "result_status": report["status"], "model_calls": 0,
                       "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, body in (("run-record.json", record), ("trial-binding.json", binding), ("trial-plan.json", {})):
                (trial / name).write_text(json.dumps(body))
            (trial / "report.html").write_text("<p>SYNTHETIC recorded fixture</p>")
            (trial / "private-ledger.sqlite3").write_bytes(b"synthetic private file, never packaged")
            files = report_files(root)
            self.assertIn("reports/model-trial-synthetic/run-record.json", files)
            self.assertFalse(any(name.endswith(".sqlite3") for name in files))
            record["data_origin"] = "engineering_test"
            (trial / "run-record.json").write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "engineering"):
                report_files(root)
            record["data_origin"] = "execution"
            (trial / "run-record.json").write_text(json.dumps(record))
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "disagree"):
                report_files(root)

    def test_model_report_costs_and_failure_survive_packaging_without_private_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pilot = root / "reports/model-pilot-synthetic"
            pilot.mkdir(parents=True)
            report = {"finished_at": "2026-10-19T00:00:00Z", "dependency_mode": "pypi_live", "model_calls": 2,
                      "status": "needs_review", "reason": "SYNTHETIC rejected patch"}
            ledger = {"model_calls": 2, "calculated_usd": "0.001", "billed_usd": None, "calls": [
                {"data_origin": "nebius_api", "dispatch_started_at": "synthetic"} for _ in range(2)]}
            for name, body in [("report.json", report), ("model-ledger.json", ledger),
                               ("authorization.json", {}), ("preflight.json", {})]:
                (pilot / name).write_text(json.dumps(body))
            (pilot / "report.html").write_text("<p>SYNTHETIC report</p>")
            (pilot / "private-ledger.sqlite3").write_bytes(b"not a database; synthetic secret state")
            files = report_files(root)
            self.assertIn(b"needs_review", files["OPEN_REPORTS.html"])
            self.assertIn(b"0.001", files["OPEN_REPORTS.html"])
            self.assertFalse(any(name.endswith(".sqlite3") for name in files))
            with self.assertRaisesRegex(ValueError, "No complete"):
                report_files(root, "offline_snapshot")
            ledger["model_calls"] = 1
            (pilot / "model-ledger.json").write_text(json.dumps(ledger))
            with self.assertRaisesRegex(ValueError, "disagree"):
                report_files(root)

    def test_missing_or_escaping_local_link_is_rejected(self):
        for link in ["missing/report.html", "../outside.html", "%2e%2e/outside.html"]:
            with self.subTest(link=link), self.assertRaisesRegex(ValueError, "Broken report"):
                validate_links({"OPEN_REPORTS.html": ("<a href='" + link + "'>report</a>").encode()})

    def test_offline_entry_and_manifest_survive_zip_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pilot = root / "reports/live-pilot-synthetic"
            directory = pilot / "state/reports/test-id"
            directory.mkdir(parents=True)
            report = directory / "report.html"
            report.write_text("<html><p>SYNTHETIC TEST REPORT</p></html>")
            report.with_suffix(".json").write_text(json.dumps({"status": "already_passes"}))
            (pilot / "unit.json").write_text(json.dumps({"run_id": "test-id", "result_status": "already_passes"}))
            (pilot / "summary.json").write_text(json.dumps({"data_origin": "execution", "dependency_mode": "pypi_live",
                "runs": [{"label": "unit", "run_id": "test-id", "result_status": "already_passes", "report": str(report)}]}))
            files = report_files(root)
            self.assertIn("OPEN_REPORTS.html", files)
            self.assertNotIn(b"<script", files["OPEN_REPORTS.html"])
            path = root / "bundle.zip"
            write_archive(path, files)
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("fresh-eyes-report-review/manifest.json"))
                restored = {name: archive.read("fresh-eyes-report-review/" + name) for name in manifest["files"]}
                for name, raw in restored.items():
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest["files"][name])
                validate_links(restored)
            report.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                report_files(root)


if __name__ == "__main__":
    unittest.main()
