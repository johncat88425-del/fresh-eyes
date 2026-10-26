"""Single-use API execution: synthetic models/runner only, no credentials or provider."""
import json
import http.client
from pathlib import Path
import shutil
import time
import threading
import unittest
from unittest.mock import patch

from fresh_eyes.api import APIError, Service, make_server
from fresh_eyes.contracts import InvalidCase
from test_core import ScriptedRunner
import test_model_trial as trial_fixture


class OneShotAPITests(unittest.TestCase):
    def setUp(self):
        self.fixture = trial_fixture.ModelTrialTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        shutil.copytree(Path(__file__).resolve().parents[1] / "api", f.project / "api")
        self.plan = f.plan(max_patch_rounds=2)
        self.service = None
        self.addCleanup(self.close)

    def close(self):
        if self.service:
            self.service.close()
            self.service = None

    def start(self, verdicts=(False, False, True, True), enabled=True, state="state"):
        f = self.fixture
        self.service = Service(f.project, f.root / state, enable_runner=True, model_plan=f.plan_path,
            enable_paid_models=enabled, env_file=f.root / "NEVER_READ.env", reference_dir=f.root / "NEVER_READ-refs",
            model_factory_builder=f.builder, model_runner_factory=lambda c: ScriptedRunner(list(verdicts)))
        self.service.store.approve(f.contract.data["case_id"], f.contract.acceptance_sha256, self.plan["review"])

    def submit(self, **extra):
        return self.service.submit({"case_id": self.fixture.contract.data["case_id"], "repair_mode": "model", **extra})

    def finished(self, run_id):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            r = self.service.store.get(run_id)
            if r["run_state"] in {"completed", "failed"}:
                return r
            time.sleep(.01)
        self.fail("Synthetic one-shot did not finish")

    def test_attaching_plan_without_activation_never_loads_key_or_runs(self):
        with patch("fresh_eyes.model_trial.OwnerModelFactory") as owner:
            self.start(enabled=False)
            self.assertFalse(self.service.capabilities()["paid_calls_enabled"])
            with self.assertRaises(APIError) as error:
                self.submit()
        self.assertEqual(error.exception.code, "model_disabled")
        owner.assert_not_called()
        self.assertFalse(self.fixture.output.exists())

    def test_two_round_queue_persists_evidence_and_consumes_exactly_once(self):
        self.start()
        summary = self.service.capabilities()["model_trial_plan"]
        self.assertEqual(summary["max_model_requests"], 4)
        self.assertNotIn("source", summary)
        self.assertNotIn("env_file", summary)
        queued = self.submit()
        r = self.finished(queued["run_id"])
        self.assertEqual(r["result_status"], "verified_fix")
        self.assertEqual(r["data_origin"], "engineering_test")
        self.assertTrue(r["api_persisted"])
        self.assertFalse(r["evaluation_eligible"])
        self.assertEqual(len(r["evidence"]["repair_rounds"]), 2)
        self.assertEqual(r["evidence"]["model_ledger"]["model_calls"], 0)
        self.assertEqual(r["evidence"]["model_ledger"]["engineering_test_calls"], 4)
        self.assertTrue(r["patch_available"])
        self.assertFalse(self.service.capabilities()["paid_calls_enabled"])
        with self.assertRaises(APIError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "model_plan_consumed")

    def test_consumed_plan_cannot_rearm_after_restart_in_another_data_directory(self):
        self.start(verdicts=(True,))
        queued = self.submit()
        self.assertEqual(self.finished(queued["run_id"])["result_status"], "already_passes")
        self.close()
        # Starting with a consumed plan fails closed before a model factory exists.
        with self.assertRaises(InvalidCase):
            self.start(state="other-state")
        self.assertEqual(self.fixture.planners, [])

    def test_interrupted_claim_is_not_reset_on_restart(self):
        self.start()
        self.service.model_plan.claim("a" * 32)
        self.close()
        self.start(state="other-state")
        self.assertFalse(self.service.capabilities()["model_calls_enabled"])
        with self.assertRaises(APIError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "model_plan_consumed")
        self.assertFalse(self.fixture.output.exists())

    def test_changed_code_stops_before_claim_key_or_output(self):
        self.start()
        (self.fixture.project / "src/fixture.py").write_text("# changed after owner plan\n")
        with self.assertRaises(APIError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "model_plan_mismatch")
        self.assertFalse(self.service.model_plan.claim_path.exists())
        self.assertFalse(self.fixture.output.exists())

    def test_browser_cannot_increase_budget_or_change_model_parameters(self):
        self.start()
        for key, value in (("budget_usd", "99"), ("route", "all_ultra"), ("max_patch_rounds", 99), ("env_file", "anything")):
            with self.assertRaises(APIError) as error:
                self.submit(**{key: value})
            self.assertEqual(error.exception.code, "unknown_fields")
        self.assertFalse(self.service.model_plan.consumed)

    def test_paid_activation_without_owner_plan_is_rejected(self):
        with self.assertRaisesRegex(InvalidCase, "owner-pinned"):
            Service(self.fixture.project, self.fixture.root / "unused", enable_runner=True, enable_paid_models=True)

    def test_mismatched_review_or_dependency_mode_does_not_consume(self):
        self.start()
        # All review identity fields must match the plan's attributed snapshot.
        current = self.service.store.approval(self.fixture.contract.data["case_id"], self.fixture.contract.acceptance_sha256)
        current["reviewed_by"] = "DIFFERENT SYNTHETIC REVIEWER"
        with patch.object(self.service.store, "approval", return_value=current):
            with self.assertRaises(APIError) as error:
                self.submit()
        self.assertEqual(error.exception.code, "model_plan_mismatch")
        self.assertFalse(self.service.model_plan.consumed)

    def test_real_http_queue_report_patch_and_second_submission(self):
        self.start()
        server = make_server(self.service, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        def request(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request(method, path, body=json.dumps(body) if body is not None else None,
                             headers={"Content-Type": "application/json"} if body is not None else {})
                res = conn.getresponse()
                raw = res.read().decode()
                return res.status, json.loads(raw) if res.getheader("Content-Type", "").startswith("application/json") else raw
            finally:
                conn.close()
        try:
            self.assertTrue(request("GET", "/api/health")[1]["model_calls_enabled"])
            body = {"case_id": self.fixture.contract.data["case_id"], "repair_mode": "model"}
            status, created = request("POST", "/api/runs", body)
            self.assertEqual(status, 202)
            final = self.finished(created["run_id"])
            self.assertEqual(request("GET", "/api/runs/" + created["run_id"])[1]["evidence"], final["evidence"])
            status, patch = request("GET", "/api/runs/" + created["run_id"] + "/patch")
            self.assertEqual(status, 200)
            self.assertIn("+pip install new", patch)
            self.assertFalse(request("GET", "/api/health")[1]["paid_calls_enabled"])
            self.assertEqual(request("POST", "/api/runs", body)[1]["error"]["code"], "model_plan_consumed")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
