"""Public replay integrity and rebuilding without ignored/private owner files."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_build", PROJECT / "web/demo/build.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
from recordings import model_fix_highlights
LATEST = "e9a04f641acb4c71b7ff0932e58f87e8"


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "clean"
        self.root.mkdir()
        for name in ("web", "cases", "api/examples", "evidence"):
            # These fixtures exercise development replays without formal results.
            # The owner's later evaluation capture must not enter this fixture.
            shutil.copytree(PROJECT / name, self.root / name,
                            ignore=shutil.ignore_patterns("evaluation-recordings") if name == "web" else None)
        # This fixture represents the seven release-candidate replay cases. New owner-only
        # challenge drafts must not silently change that fixed publication fixture.
        published_development = {"mistletoe-markdown", "md2html-markdown", "json2yaml-yaml2json", "envdoctor-check"}
        for path in (self.root / "cases").glob("*.json"):
            data = json.loads(path.read_text())
            if isinstance(data, dict) and "fixtures" in data and data.get("case_id") not in published_development:
                path.unlink()
        self.out = Path(self.temp.name) / "site"

    def test_build_without_reports_database_env_or_runner(self):
        result = demo.build(self.root, self.out)
        self.assertEqual((result["cases"], result["approved"], result["recorded_runs"]), (7, 7, 24))
        boundary = [c for c in json.loads((self.out / "data/cases.json").read_text())["items"] if c.get("group") == "boundary"]
        self.assertEqual(sorted(c["case_id"] for c in boundary), ["gfm-to-html-convert", "j2cli-render", "yq-query"])
        self.assertTrue(all(c["review"]["reviewed_by"] == "kevin" and not c["can_run"] for c in boundary))
        self.assertFalse((self.root / "reports").exists())
        self.assertFalse((self.out / "evaluation.json").exists())
        self.assertFalse((self.out / "examples/two-round-success.json").exists())
        health = json.loads((self.out / "data/health.json").read_text())
        self.assertTrue(health["read_only"])
        self.assertFalse(health["paid_calls_enabled"])
        self.assertFalse(health["execution_enabled"])
        for record in (self.out / "data/runs").glob("*.json"):
            r = json.loads(record.read_text())
            self.assertEqual(r["data_origin"], "recorded_replay")
            self.assertFalse(r["evaluation_eligible"])
            self.assertFalse(r["costs"]["paid_calls_enabled"])
        r = json.loads((self.out / f"data/runs/{LATEST}.json").read_text())
        self.assertEqual(r["result_status"], "needs_review")
        rounds = r["evidence"]["repair_rounds"]
        self.assertEqual([r["status"] for r in rounds], ["rejected", "no_candidate"])
        self.assertTrue(rounds[0]["candidate_patch"]["patch"]["edits"])
        self.assertNotIn("candidate_patch", rounds[1])
        self.assertEqual(r["evidence"]["model_calls"], 4)
        self.assertEqual(str(r["evidence"]["model_ledger"]["calculated_usd"]), "0.01362438")
        self.assertIsNone(r["evidence"]["model_ledger"]["billed_usd"])
        manifest = json.loads((self.out / "data/manifest.json").read_text())
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((self.out / name).read_bytes()).hexdigest(), digest)
        examples = json.loads((self.out / "data/examples.json").read_text())["items"]
        self.assertEqual(len(examples), 10)
        for name, body in examples.items():
            self.assertEqual(json.loads((self.out / f"data/examples/{name}.json").read_text()), body)

    def test_maintainer_note_attaches_only_to_the_exact_verified_patch(self):
        fixed = "563db9062b7b4e48a4490e41b6792ff6"
        record = json.loads((self.root / f"web/demo/recordings/runs/{fixed}.json").read_text())
        patch = record["evidence"]["patch"]
        note = {"title": "Synthetic test note", "body": "Synthetic body for a build test, not model output.",
                "footer": "Synthetic footer.", "diff": patch["diff"], "patch_sha256": patch["sha256"],
                "run_id": record["evidence"]["run_id"], "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                "status": "draft", "posted": False, "human_review_required": True, "ledger": {"calls": ["private"]}}
        notes = self.root / "web/demo/recordings/notes"
        shutil.rmtree(notes,ignore_errors=True)
        notes.mkdir()
        manifest_path = notes.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"] = {k: v for k, v in manifest["files"].items() if not k.startswith("notes/")}
        manifest_path.write_text(json.dumps(manifest))
        (notes / "note.json").write_text(json.dumps(note))
        demo.build(self.root, self.out)
        shown = json.loads((self.out / f"data/runs/{fixed}.json").read_text())["maintainer_note"]
        self.assertEqual((shown["title"], shown["posted"]), ("Synthetic test note", False))
        self.assertNotIn("ledger", shown)
        for bad in ({"posted": True}, {"patch_sha256": "0" * 64}, {"diff": "other"}, {"run_id": LATEST}, {"status": "sent"}):
            (notes / "note.json").write_text(json.dumps({**note, **bad}))
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "Maintainer note"):
                demo.build(self.root, Path(self.temp.name) / "other")

    def test_boundary_case_needs_a_matching_human_approval(self):
        path = self.root / "web/demo/boundary-approvals.json"
        snapshot = json.loads(path.read_text())
        snapshot["approvals"][0]["acceptance_sha256"] = "0" * 64
        path.write_text(json.dumps(snapshot))
        demo.build(self.root, self.out)
        shown = {c["case_id"] for c in json.loads((self.out / "data/cases.json").read_text())["items"]}
        self.assertNotIn(snapshot["approvals"][0]["case_id"], shown)
        self.assertEqual(len(shown), 6)

    def test_envdoctor_is_locked_to_chris_and_does_not_replace_first_ai_story(self):
        demo.build(self.root, self.out)
        cases = json.loads((self.out / "data/cases.json").read_text())["items"]
        case = next(c for c in cases if c["case_id"] == "envdoctor-check")
        self.assertEqual(case["review"]["reviewed_by"], "Chris")
        self.assertEqual(case["review"]["status"], "approved")
        self.assertEqual(case["acceptance_sha256"], "ae27aa64c7176738139bd59305aa2c230b2f2ccbcf4083b2be486af04c4ee85e")
        self.assertTrue(all(f["cwd_files"] for f in case["fixtures"]))
        health = json.loads((self.out / "data/health.json").read_text())
        self.assertEqual(health["demo_highlights"][:2], [
            {"key": "model_fix", "run_id": "5c225204bcd946228a7ef4700c8c0c8c"},
            {"key": "model_fix_more", "run_id": "2c3377fca8be40e69ce3d1994a0f15ab"}])
        record = json.loads((self.out / "data/runs/2c3377fca8be40e69ce3d1994a0f15ab.json").read_text())
        self.assertEqual(record["result_status"], "verified_fix")
        self.assertFalse(record["evaluation_eligible"])

    def test_ai_highlights_count_repositories_rather_than_case_ids(self):
        rows = [{"case_id": "one", "repo_url": "https://github.com/Owner/Repo.git", "run_id": "old", "finished_at": "2026-10-24"},
                {"case_id": "other-case", "repo_url": "https://github.com/owner/repo/", "run_id": "new", "finished_at": "2026-10-25"},
                {"case_id": "two", "repo_url": "https://github.com/owner/second", "run_id": "second", "finished_at": "2026-10-26"}]
        self.assertEqual(model_fix_highlights(rows), [
            {"key": "model_fix", "run_id": "new"}, {"key": "model_fix_more", "run_id": "second"}])

    def test_startup_record_index_preserves_commit_for_matching_readme_preview(self):
        demo.build(self.root, self.out)
        index = json.loads((self.out / 'data/runs.json').read_text())['items']
        row = next(r for r in index if r['run_id'] == 'ebd915311a8f4f4d9b2373afdc718c9f')
        detail = json.loads((self.out / f"data/runs/{row['run_id']}.json").read_text())
        self.assertEqual(row['commit_sha'], detail['commit_sha'])
        self.assertEqual(row['commit_sha'], '46f4d2469097be395558768714a5f07ebccf1412')

    def test_recording_tampering_stops_before_output(self):
        p = self.root / f"web/demo/recordings/runs/{LATEST}.json"
        p.write_bytes(p.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "hash changed"):
            demo.build(self.root, self.out)
        self.assertFalse(self.out.exists())

    def test_stale_approval_hash_does_not_lock_a_case(self):
        p = self.root / "evidence/approval-history-verified.json"
        proof = json.loads(p.read_text())
        event = proof["approval_events"][0]
        case_id = event["case_id"]
        event["acceptance_sha256"] = "0" * 64
        p.write_text(json.dumps(proof))
        cases, history = demo.cases_and_history(self.root)
        case = next(c for c in cases if c["case_id"] == case_id)
        self.assertNotEqual(case["review"]["status"], "approved")
        self.assertNotIn(case_id, history)

    def test_failed_public_scan_preserves_previous_good_demo(self):
        demo.build(self.root, self.out)
        before = (self.out / "data/manifest.json").read_bytes()
        for leaked in ("/Users/synthetic-private-path", "NEBIUS_API_KEY=synthetic-control-secret"):
            p = self.root / "web/app.js"
            p.write_text(p.read_text() + "\n// " + leaked)
            with self.assertRaisesRegex(ValueError, "Possible local path or credential"):
                demo.build(self.root, self.out, replace=True)
            self.assertEqual((self.out / "data/manifest.json").read_bytes(), before)
            shutil.copyfile(PROJECT / "web/app.js", p)

    def test_development_data_cannot_become_formal_evaluation(self):
        (self.root / "evaluation").mkdir()
        (self.root / "evaluation/results.json").write_text('{"data_origin":"example"}')
        with self.assertRaisesRegex(ValueError, "Only formal evaluation"):
            demo.build(self.root, self.out)
        self.assertFalse(self.out.exists())

    def test_does_not_replace_unmarked_directory(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("unrelated")
        with self.assertRaisesRegex(ValueError, "recognized"):
            demo.build(self.root, self.out, replace=True)
        self.assertEqual((self.out / "keep.txt").read_text(), "unrelated")

    def test_synthetic_calls_are_rejected_even_if_file_hash_updated(self):
        path = self.root / f"web/demo/recordings/runs/{LATEST}.json"
        r = json.loads(path.read_text())
        r["evidence"]["model_ledger"]["calls"][0]["data_origin"] = "engineering_test"
        path.write_text(json.dumps(r))
        self.rehash(path)
        with self.assertRaisesRegex(ValueError, "synthetic or inconsistent"):
            demo.build(self.root, self.out)

    def test_manual_patch_must_still_match_evidence_hash(self):
        path = next((self.root / "web/demo/recordings/patches").glob("*.diff"))
        path.write_text(path.read_text() + "\n")
        self.rehash(path)
        with self.assertRaisesRegex(ValueError, "patch differs"):
            demo.build(self.root, self.out)

    def rehash(self, path):
        root = self.root / "web/demo/recordings"
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"][path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))

    def test_startup_replay_keeps_null_acceptance_and_no_functional_claim(self):
        demo.build(self.root, self.out)
        r = json.loads((self.out / 'data/runs/ebd915311a8f4f4d9b2373afdc718c9f.json').read_text())
        self.assertIsNone(r['case_id'])
        self.assertIsNone(r['acceptance_sha256'])
        self.assertEqual(r['review']['status'], 'draft')
        self.assertEqual(r['result_status'], 'startup_only')
        self.assertFalse(r['evaluation_eligible'])

    def test_startup_cannot_be_relabelled_functional_even_after_rehash(self):
        path = self.root / 'web/demo/recordings/runs/ebd915311a8f4f4d9b2373afdc718c9f.json'
        r = json.loads(path.read_text())
        r['result_status'] = r['evidence']['status'] = 'verified_fix'
        path.write_text(json.dumps(r))
        self.rehash(path)
        with self.assertRaisesRegex(ValueError, 'Startup replay'):
            demo.build(self.root, self.out)

    def test_boundary_baselines_keep_scope_and_cannot_become_ai_repairs(self):
        path = self.root / 'web/demo/recordings/runs/514ca0a68ff84d6790d60b41ce6d1bd8.json'
        r = json.loads(path.read_text())
        self.assertEqual(r['recording_scope'], 'boundary_baseline')
        self.assertFalse(r['counted_in_fix_rate'])
        r['repair_mode'] = 'model'
        path.write_text(json.dumps(r))
        self.rehash(path)
        with self.assertRaisesRegex(ValueError, 'Boundary baseline'):
            demo.build(self.root, self.out)

    def test_manual_recheck_has_original_patch_and_two_clean_passes(self):
        demo.build(self.root, self.out)
        r = json.loads((self.out / 'data/runs/10cef22de8974ff7b5134635e39ee0ae.json').read_text())
        self.assertEqual(r['repair_author'], 'manual')
        self.assertEqual(r['repair_mode'], 'manual_candidate')
        self.assertEqual(r['evidence']['model_calls'], 0)
        self.assertEqual([a['status'] for a in r['evidence']['attempts']], ['failed', 'passed', 'passed'])
        patch = (self.out / 'data/patches/10cef22de8974ff7b5134635e39ee0ae.diff').read_bytes()
        self.assertEqual(hashlib.sha256(patch).hexdigest(), r['evidence']['patch']['sha256'])

    def test_real_uninterrupted_ai_fix_is_highlighted_without_saved_candidate_provenance(self):
        demo.build(self.root,self.out)
        fixed=json.loads((self.out/'data/runs/5c225204bcd946228a7ef4700c8c0c8c.json').read_text())
        self.assertEqual((fixed['result_status'],fixed['repair_author']),('verified_fix','model'))
        self.assertEqual([a['status'] for a in fixed['evidence']['attempts']],['failed','passed','passed'])
        self.assertEqual(fixed['evidence']['model_calls'],2)
        self.assertNotIn('candidate_provenance',fixed['evidence'])
        self.assertEqual({c['data_origin'] for c in fixed['costs']['model_calls']},{'nebius_api'})
        health=json.loads((self.out/'data/health.json').read_text())
        self.assertEqual(health['demo_highlights'][0],{'key':'model_fix','run_id':fixed['run_id']})

    def test_saved_model_verification_links_real_source_without_charging_calls_twice(self):
        demo.build(self.root,self.out)
        saved=json.loads((self.out/'data/runs/5ac511039adb4977b32ef129e86ee006.json').read_text())
        source=json.loads((self.out/'data/runs/1a5f5135c92f4d15a5ff6dabe0a78b64.json').read_text())
        self.assertEqual(saved['result_status'],'verified_fix')
        self.assertEqual(source['result_status'],'infrastructure_error')
        self.assertEqual(saved['evidence']['model_calls'],0)
        self.assertEqual(source['evidence']['model_calls'],2)
        self.assertEqual(saved['evidence']['patch']['sha256'],source['evidence']['patch']['sha256'])
        self.assertFalse((self.out/'evaluation.json').exists())

    def test_saved_candidate_cannot_borrow_provenance_after_editing_the_proposal(self):
        path=self.root/'web/demo/recordings/runs/5ac511039adb4977b32ef129e86ee006.json'
        r=json.loads(path.read_text());r['evidence']['candidate_patch']['patch']['edits'][0]['new']='invented'
        path.write_text(json.dumps(r));self.rehash(path)
        with self.assertRaisesRegex(ValueError,'Saved candidate provenance'):
            demo.build(self.root,self.out)

    def test_saved_verification_does_not_relabel_original_diagnosis(self):
        path=self.root/'web/demo/recordings/runs/5ac511039adb4977b32ef129e86ee006.json'
        r=json.loads(path.read_text());r['source_model_diagnosis']['classification_model']='invented-model'
        path.write_text(json.dumps(r));self.rehash(path)
        with self.assertRaisesRegex(ValueError,'Saved candidate provenance'):
            demo.build(self.root,self.out)
