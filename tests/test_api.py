"""Real loopback HTTP tests; synthetic source and explicit runner doubles only."""

import http.client
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fresh_eyes.api import Service, Store, make_server
from fresh_eyes.runner import InfrastructureError
from test_core import ScriptedRunner, case_data


class TeammateAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / "project"
        (self.project / "cases").mkdir(parents=True)
        shipped = Path(__file__).resolve().parents[1]
        shutil.copytree(shipped / "api", self.project / "api")
        self.source = self.root / "sources" / "unit"
        self.source.mkdir(parents=True)
        (self.source / "README.md").write_text("pip install old\n")
        for args in [["git", "init", "-q", str(self.source)], ["git", "-C", str(self.source), "add", "README.md"],
                     ["git", "-C", str(self.source), "-c", "user.name=Fresh Eyes Tests", "-c", "user.email=tests@example.invalid",
                      "-c", "core.hooksPath=/dev/null", "commit", "-qm", "synthetic API fixture"]]:
            subprocess.run(args, check=True, capture_output=True)
        sha = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        self.data = case_data(sha)
        (self.project / "cases" / "synthetic.json").write_text(json.dumps(self.data))
        (self.project / "cases" / "unit-candidate-patch.json").write_text(json.dumps({
            "edits": [{"path": "README.md", "old": "old", "new": "new"}]}))
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        self.service = self.server = self.thread = None
        self.case_id = self.data["case_id"]

    def start(self, *, enabled=False, development=True, runner_factory=None, enable_live_pypi=False, origins=None, **options):
        self.service = Service(self.project, self.root / "state", enable_runner=enabled, development=development,
                               source_root=self.root / "sources", wheelhouse=self.wheels,
                               runner_factory=runner_factory or (lambda c: ScriptedRunner([True])),
                               enable_live_pypi=enable_live_pypi, **options)
        self.server = make_server(self.service, port=0, origins=origins)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.thread.join(timeout=2)
            self.server.server_close()
            self.server = None
        if self.service:
            self.service.close()
            self.service = None

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        encoded = json.dumps(body) if body is not None else raw
        h = {"Content-Type": "application/json"} if body is not None or raw is not None else {}
        h.update(headers or {})
        connection.request(method, path, body=encoded, headers=h)
        response = connection.getresponse()
        data = response.read().decode()
        info = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, json.loads(data) if info.get("Content-Type", "").startswith("application/json") else data, info

    def submit(self, **extra):
        return self.request("POST", "/api/runs", {"case_id": self.case_id, **extra})

    def finished(self, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, record, _ = self.request("GET", "/api/runs/" + run_id)
            self.assertEqual(status, 200)
            if record["run_state"] in {"completed", "failed"}:
                return record
            time.sleep(0.01)
        self.fail("Synthetic API run did not finish")

    def test_contracts_and_capabilities_work_without_docker_or_keys(self):
        self.start()
        status, health, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["api_version"], "0.2.1")
        self.assertEqual(health["display_timezone"], "Europe/London")
        self.assertFalse(health["execution_enabled"])
        self.assertFalse(health["paid_calls_enabled"])
        self.assertFalse(health["arbitrary_repo_execution"])
        _, payload, _ = self.request("GET", "/api/cases")
        self.assertEqual(payload["items"][0]["fixtures"], self.data["fixtures"])
        self.assertFalse(payload["items"][0]["upstream_maintainer_approval"])
        status, error, _ = self.submit()
        self.assertEqual(status, 503)
        self.assertEqual(error["error"]["code"], "runner_disabled")

    def test_approval_is_idempotent_and_correction_preserves_attribution_history(self):
        self.start()
        endpoint = f"/api/cases/{self.case_id}/approval"
        body = {"acceptance_sha256": self.service.contract(self.case_id).acceptance_sha256,
                "reviewed_by": "Synthetic Reviewer One"}
        original = self.request("POST", endpoint, body)[1]["review"]
        again = self.request("POST", endpoint, body)[1]["review"]
        self.assertEqual(original, again)
        changed = {**body, "reviewed_by": "Synthetic Reviewer Two"}
        status, error, _ = self.request("POST", endpoint, changed)
        self.assertEqual((status, error["error"]["code"]), (409, "approval_exists"))
        correction = {**changed, "expected_revision": 1, "corrected_by": "Synthetic Operator",
                      "reason": "Synthetic attribution typo correction"}
        self.assertEqual(self.request("POST", endpoint + "/correction", {**correction, "reason": ""})[0], 400)
        fixed = self.request("POST", endpoint + "/correction", correction)[1]["review"]
        self.assertEqual(fixed["review_revision"], 2)
        self.assertEqual(fixed["approved_at"], original["approved_at"])
        self.assertEqual(self.request("POST", endpoint + "/correction", correction)[0], 409)
        events = self.request("GET", endpoint + "/history")[1]["items"]
        self.assertEqual([e["review"]["reviewed_by"] for e in events], ["Synthetic Reviewer One", "Synthetic Reviewer Two"])
        self.assertEqual([e["event_type"] for e in events], ["approved", "correction"])
        self.assertEqual(events[1]["review"]["correction"]["corrected_by"], "Synthetic Operator")

    def test_startup_validates_repo_and_ref_before_unsupported_response(self):
        self.start()
        for url in ('not a url', 'https://other.invalid/owner/repo', 'https://github.com/owner/repo?secret=test'):
            status, error, _ = self.submit(verification_level='startup', repo_url=url)
            self.assertEqual((status,error['error']['code']),(400,'invalid_repo_url'))
        status, error, _ = self.submit(verification_level='startup', repo_url='https://github.com/owner/repo', ref='main')
        self.assertEqual((status,error['error']['code']),(400,'invalid_ref'))
        self.assertEqual(self.submit(verification_level='startup', repo_url='https://github.com/owner/repo')[0],501)

    def test_static_examples_are_labelled_and_never_in_real_history(self):
        self.start()
        status, examples, _ = self.request("GET", "/api/examples")
        self.assertEqual(status, 200)
        self.assertEqual(len(examples["items"]), 10)
        for name, example in examples["items"].items():
            self.assertEqual(example["data_origin"], "example")
            self.assertFalse(example["executed"])
            self.assertEqual(self.request("GET", "/api/examples/" + name)[1], example)
        self.assertEqual(self.request("GET", "/api/runs")[1]["items"], [])
        fake_id = examples["items"]["verified-fix"]["run_id"]
        self.assertEqual(self.request("GET", "/api/runs/" + fake_id)[0], 404)

    def test_github_import_disabled_does_not_download_or_activate_runner(self):
        from unittest.mock import Mock
        inspector = Mock()
        self.start(startup_inspector=inspector)
        status, error, _ = self.request('POST','/api/repo-inspections',{'repo_url':'https://github.com/unit/project'})
        self.assertEqual((status,error['error']['code']), (503,'github_import_disabled'))
        inspector.assert_not_called()

    def test_readonly_github_inspection_is_queued_and_persisted_without_human_approval(self):
        from test_github_startup import Reader
        from fresh_eyes.startup import inspect_repo
        def inspector(url, ref, source):
            return inspect_repo(url,ref,source,reader=Reader())
        self.start(enable_github_import=True,startup_inspector=inspector)
        status, record, _ = self.request('POST','/api/repo-inspections',{'repo_url':'https://github.com/unit/project'})
        self.assertEqual(status,202)
        final = self.finished(record['run_id'])
        self.assertEqual(final['result_status'],'needs_input')
        self.assertEqual(final['commit_sha'],'a'*40)
        self.assertEqual(final['evidence']['plan']['status'],'ready')
        self.assertFalse(final['evidence']['executed'])
        self.assertIsNone(final['acceptance_sha256'])
        self.assertEqual(final['review']['status'],'draft')
        self.assertFalse(self.service.capabilities()['paid_calls_enabled'])
        self.assertFalse(self.service.capabilities()['startup_checks'])

    def test_github_startup_has_distinct_flags_and_can_only_return_startup_only(self):
        from test_github_startup import Reader, Runner
        from fresh_eyes.startup import inspect_repo
        runner = Runner()
        self.start(enabled=True, enable_live_pypi=True, enable_github_import=True,enable_startup_checks=True,
                   startup_inspector=lambda u,r,s:inspect_repo(u,r,s,reader=Reader()),startup_runner_factory=lambda:runner)
        body={'repo_url':'https://github.com/unit/project','verification_level':'startup','dependency_mode':'pypi_live','repair_mode':'none'}
        status, record, _=self.request('POST','/api/runs',body)
        self.assertEqual(status,202)
        final=self.finished(record['run_id'])
        self.assertEqual(final['result_status'],'startup_only')
        self.assertEqual(runner.calls,1)
        self.assertFalse(final['evaluation_eligible'])
        self.assertIsNone(final['acceptance_sha256'])
        self.assertEqual(final['costs']['model_usd'],0.0)
        self.assertFalse(final['patch_available'])
        self.assertEqual(self.request('GET','/api/runs/'+record['run_id']+'/patch')[0],404)
        self.assertEqual(self.request('POST','/api/runs',{**body,'repair_mode':'model'})[0],400)
        self.assertEqual(self.request('POST','/api/runs',{**body,'dependency_mode':'offline_snapshot'})[0],400)

    def test_startup_execution_cannot_be_enabled_without_all_prerequisites(self):
        from fresh_eyes.contracts import InvalidCase
        with self.assertRaises(InvalidCase):
            Service(self.project, self.root/'not-started',enable_startup_checks=True)
        self.assertFalse((self.root/'not-started').exists())

    def test_example_baseline_uses_original_readme_not_the_candidate_fix(self):
        self.start()
        example = self.request("GET", "/api/examples/verified-fix")[1]
        attempts = example["evidence"]["attempts"]
        self.assertEqual(attempts[0]["result"]["steps"][0]["argv"], ["pip", "install", "-r", "requirements.txt"])
        for attempt in attempts[1:]:
            self.assertEqual(attempt["result"]["steps"][0]["argv"], ["pip", "install", "."])
        self.assertEqual(self.request("GET", "/api/examples/needs-input")[1]["result_status"], "needs_input")
        health = self.request("GET", "/api/health")[1]
        self.assertIn("needs_input", health["result_statuses"])

    def test_live_mode_is_explicit_and_does_not_reuse_offline_wheels(self):
        self.start(enabled=True)
        status, error, _ = self.submit(dependency_mode="pypi_live")
        self.assertEqual(status, 403)
        self.assertEqual(error["error"]["code"], "live_pypi_disabled")
        self.assertEqual(self.submit(dependency_mode=[])[0], 400)
        self.stop()
        self.wheels.rmdir()
        self.start(enabled=True, enable_live_pypi=True)
        self.assertEqual(self.submit()[0], 503)
        status, queued, _ = self.submit(dependency_mode="pypi_live")
        self.assertEqual(status, 202)
        record = self.finished(queued["run_id"])
        self.assertEqual(record["dependency_mode"], "pypi_live")
        self.assertEqual(record["evidence"]["dependency_mode"], "pypi_live")
        self.assertEqual(record["evidence"]["wheelhouse"], [])
        self.assertTrue(record["evidence"]["environment"]["test_double"])

    def test_approval_requires_current_hash_and_is_persisted(self):
        self.start(enabled=True, development=False)
        self.assertEqual(self.submit()[1]["error"]["code"], "needs_review")
        endpoint = f"/api/cases/{self.case_id}/approval"
        self.assertEqual(self.request("POST", endpoint, {"reviewed_by": "Test reviewer", "acceptance_sha256": "0" * 64})[0], 409)
        acceptance = self.request("GET", "/api/cases")[1]["items"][0]["acceptance_sha256"]
        self.assertEqual(self.request("POST", endpoint, {"reviewed_by": "  ", "acceptance_sha256": acceptance})[0], 400)
        status, case, _ = self.request("POST", endpoint, {"reviewed_by": "Test reviewer", "acceptance_sha256": acceptance})
        self.assertEqual(status, 200)
        self.assertEqual(case["review"]["scope"], "team_acceptance_review")
        self.assertFalse(case["upstream_maintainer_approval"])
        self.stop()
        self.start(enabled=True, development=False)
        status, queued, _ = self.submit()
        self.assertEqual(status, 202)
        self.assertEqual(self.finished(queued["run_id"])["result_status"], "already_passes")

    def test_real_queue_and_report_keep_functional_result_separate_from_job_state(self):
        self.start(enabled=True, runner_factory=lambda c: ScriptedRunner([False]))
        status, queued, _ = self.submit()
        self.assertEqual(status, 202)
        self.assertEqual(queued["run_state"], "queued")
        self.assertIsNone(queued["result_status"])
        record = self.finished(queued["run_id"])
        self.assertEqual(record["run_state"], "completed")
        self.assertEqual(record["result_status"], "unresolved")
        self.assertEqual(record["data_origin"], "execution")
        self.assertEqual(record["evidence"]["model_calls"], 0)
        self.assertEqual(record["evidence"]["attempts"][0]["validation"]["passed"], False)
        self.assertIsNone(record["costs"]["runner_usd"])
        self.assertEqual(self.request("GET", "/api/runs/" + record["run_id"] + "/patch")[0], 404)
        self.stop()
        self.start()
        persisted = self.request("GET", "/api/runs/" + record["run_id"])[1]
        self.assertEqual(persisted["result_status"], "unresolved")

    def test_verified_manual_patch_needs_both_trials_and_downloads_actual_diff(self):
        self.start(enabled=True, runner_factory=lambda c: ScriptedRunner([False, True, True]))
        status, queued, _ = self.submit(repair_mode="manual_candidate")
        self.assertEqual(status, 202)
        record = self.finished(queued["run_id"])
        self.assertEqual(record["result_status"], "verified_fix")
        self.assertEqual(record["repair_author"], "manual")
        self.assertEqual([a["phase"] for a in record["evidence"]["attempts"]],
                         ["original_readme", "candidate_patch", "fresh_verification"])
        status, patch, headers = self.request("GET", "/api/runs/" + record["run_id"] + "/patch")
        self.assertEqual(status, 200)
        self.assertIn("text/x-diff", headers["Content-Type"])
        self.assertIn("+pip install new", patch)

    def test_failed_final_trial_never_becomes_verified_fix(self):
        self.start(enabled=True, runner_factory=lambda c: ScriptedRunner([False, True, False]))
        _, queued, _ = self.submit(repair_mode="manual_candidate")
        record = self.finished(queued["run_id"])
        self.assertEqual(record["result_status"], "unresolved")
        self.assertTrue(record["patch_available"])

    def test_missing_runtime_is_an_infrastructure_result(self):
        class Missing:
            def preflight(self):
                raise InfrastructureError("Synthetic unavailable runtime")
        self.start(enabled=True, runner_factory=lambda c: Missing())
        _, queued, _ = self.submit()
        record = self.finished(queued["run_id"])
        self.assertEqual(record["result_status"], "infrastructure_error")
        self.assertEqual(record["evidence"]["attempts"], [])

    def test_source_and_arbitrary_patch_inputs_cannot_override_registry(self):
        self.start(enabled=True)
        self.assertEqual(self.submit(repo_url="https://example.invalid/another")[0], 409)
        self.assertEqual(self.submit(ref="b" * 40)[0], 409)
        self.assertEqual(self.submit(patch={"edits": []})[0], 400)
        self.assertEqual(self.submit(repair_mode=[])[0], 400)
        self.assertEqual(self.request("POST", "/api/runs", {"case_id": "missing"})[0], 404)
        self.assertEqual(self.request("POST", "/api/runs", ["not an object"])[0], 400)
        self.assertEqual(self.request("POST", "/api/runs", {"verification_level": "functional"})[0], 400)

    def test_github_startup_remains_explicitly_disabled_by_default(self):
        self.start(enabled=True)
        status, error, _ = self.request("POST", "/api/runs", {"repo_url": "https://github.com/example/repo", "verification_level": "startup"})
        self.assertEqual(status, 501)
        self.assertEqual(error["error"]["code"], "startup_not_enabled")
        self.assertEqual(self.request("GET", "/api/runs")[1]["items"], [])

    def test_loopback_host_and_frontend_origins_are_checked(self):
        self.start()
        status, _, headers = self.request("OPTIONS", "/api/runs", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://localhost:5173")
        self.assertEqual(self.request("GET", "/api/cases", headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/cases", headers={"Host": "example.invalid"})[0], 403)

    def test_extra_frontend_origins_preserve_defaults_and_reject_unlisted_origins(self):
        extra = ["http://localhost:8080", "http://127.0.0.1:8766"]
        self.start(origins=extra)
        allowed = extra + ["http://localhost:5173", "http://127.0.0.1:5173",
                           "http://localhost:3000", "http://127.0.0.1:3000"]
        for origin in allowed:
            for method, path, expected in [("GET", "/api/cases", 200), ("OPTIONS", "/api/runs", 204)]:
                with self.subTest(origin=origin, method=method):
                    status, _, headers = self.request(method, path, headers={"Origin": origin})
                    self.assertEqual(status, expected)
                    self.assertEqual(headers["Access-Control-Allow-Origin"], origin)
                    self.assertEqual(headers["Vary"], "Origin")
        for origin in ["http://localhost:9999", "https://localhost:5173", "https://example.invalid", "null"]:
            status, error, headers = self.request("GET", "/api/cases", headers={"Origin": origin})
            self.assertEqual(status, 403)
            self.assertEqual(error["error"]["code"], "origin_not_allowed")
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_malformed_json_and_large_requests_are_rejected(self):
        self.start()
        self.assertEqual(self.request("POST", "/api/runs", raw="{")[0], 400)
        self.assertEqual(self.request("POST", "/api/runs", raw="{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/runs", raw="{}", headers={"Content-Length": "65537"})[0], 413)

    def test_single_worker_queue_is_bounded(self):
        release = threading.Event()
        class Blocking(ScriptedRunner):
            def preflight(self):
                release.wait(timeout=3)
                return super().preflight()
        self.start(enabled=True, runner_factory=lambda c: Blocking([True]))
        try:
            ids = [self.submit()[1]["run_id"] for _ in range(4)]
            status, error, _ = self.submit()
            self.assertEqual(status, 429)
            self.assertEqual(error["error"]["code"], "queue_full")
        finally:
            release.set()
        for run_id in ids:
            self.assertEqual(self.finished(run_id)["result_status"], "already_passes")

    def test_interrupted_job_is_recovered_without_automatic_execution(self):
        self.start()
        example = self.request("GET", "/api/examples/running")[1]
        example.update(data_origin="execution", run_id="f" * 32)
        self.service.store.save(example)
        self.stop()
        self.start()
        record = self.request("GET", "/api/runs/" + "f" * 32)[1]
        self.assertEqual(record["run_state"], "failed")
        self.assertEqual(record["result_status"], "infrastructure_error")
        self.assertIn("stopped", record["reason"])

    def test_second_service_cannot_recover_an_active_instances_jobs(self):
        self.start()
        record = self.request("GET", "/api/examples/running")[1]
        record.update(data_origin="execution", run_id="e" * 32)
        self.service.store.save(record)
        with self.assertRaisesRegex(ValueError, "Another Fresh Eyes API"):
            Service(self.project, self.root / "state")
        self.assertEqual(self.service.store.get("e" * 32)["run_state"], "running")

    def test_openapi_is_served_and_all_schema_references_exist(self):
        self.start()
        status, spec, _ = self.request("GET", "/api/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(spec["openapi"], "3.1.0")
        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    self.assertIn(value["$ref"].split("/")[-1], spec["components"]["schemas"])
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(spec)


class SQLiteLifecycle(unittest.TestCase):
    def test_all_connections_close_and_exceptions_roll_back(self):
        original_connect = sqlite3.connect
        opened = []
        class Traced(sqlite3.Connection):
            closed = False
            def close(self):
                super().close()
                self.closed = True
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs, factory=Traced)
            opened.append(connection)
            return connection
        with tempfile.TemporaryDirectory() as temp, patch("fresh_eyes.api.sqlite3.connect", side_effect=connect):
            store = Store(Path(temp))
            record = {"run_id": "a" * 32, "created_at": "2026-10-18T12:00:00+00:00", "run_state": "completed"}
            store.save(record)
            self.assertEqual(store.get(record["run_id"]), record)
            self.assertEqual(len(store.records()), 1)
            store.approve("case", "hash", {"reviewed_by": "unit test"})
            self.assertEqual(store.approval("case", "hash")["reviewed_by"], "unit test")
            with self.assertRaises(RuntimeError):
                with store.connection() as db:
                    db.execute("DELETE FROM runs")
                    raise RuntimeError("exercise rollback/close")
            self.assertEqual(len(store.records()), 1)
            self.assertTrue(all(connection.closed for connection in opened))
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
