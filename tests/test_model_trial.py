"""Development entry-point invariants: self-owned repo and fake models only."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch

from fresh_eyes.approval_io import CORE, FORMAT as APPROVAL_FORMAT, SCOPE
from fresh_eyes.contracts import Contract, InvalidCase, digest
from fresh_eyes.diagnostics import RepairPlanner
from fresh_eyes.model_trial import prepare_trial, run_trial, load_public_specs
from fresh_eyes.models import ModelCaller, ModelError, ModelLedger
from fresh_eyes.protocols import PROTOCOLS
from fresh_eyes.runner import InfrastructureError
from test_core import ScriptedRunner, case_data
from test_models import SyntheticTransport, spec


class ModelTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        (self.project / "cases").mkdir(parents=True)
        (self.project / "src").mkdir()
        (self.project / "src/fixture.py").write_text("# synthetic version marker\n")
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "README.md").write_text("pip install old\n")
        for argv in (["git", "init", "-q", str(self.source)], ["git", "-C", str(self.source), "add", "README.md"],
                     ["git", "-C", str(self.source), "-c", "user.name=Synthetic Trial", "-c", "user.email=trial@example.invalid",
                      "-c", "core.hooksPath=/dev/null", "commit", "-qm", "synthetic input"]):
            subprocess.run(argv, check=True, capture_output=True)
        sha = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        self.case = self.project / "cases/unit.json"
        self.case.write_text(json.dumps(case_data(sha)))
        self.contract = Contract.load(self.case)
        review = {"status": "approved", "reviewed_by": "SYNTHETIC HUMAN FIXTURE", "approved_at": "2026-10-20T00:00:00Z",
                  "scope": SCOPE, "acceptance_sha256": self.contract.acceptance_sha256}
        row = {key: review[key] for key in CORE}
        row.update(case_id=self.contract.data["case_id"], repo_url=self.contract.data["repo"]["url"],
                   commit_sha=sha, upstream_maintainer_approval=False)
        self.approvals = self.root / "approvals.json"
        self.approvals.write_text(json.dumps({"format": APPROVAL_FORMAT, "schema_version": 1, "scope": SCOPE,
            "exported_at": "2026-10-20T00:00:00Z", "approvals": [row], "payload_sha256": digest([row]), "trust_notice": "SYNTHETIC"}))
        self.specs = {"nano": spec(), "ultra": spec("ultra-test", "1", "3")}
        self.config = self.root / "models.json"
        self.config.write_text(json.dumps({k: asdict(v) for k, v in self.specs.items()}))
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        self.output = self.root / "trial"
        self.plan_path = self.root / "plan.json"
        self.planners = []

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, **changes):
        options = dict(case_id=self.contract.data["case_id"], approvals=self.approvals, source=self.source,
                       model_config=self.config, output_root=self.output, runner_id="sha256:" + "1" * 64,
                       wheelhouse=self.wheels, budget_usd="0.02", time_limit_s=60)
        options.update(changes)
        value = prepare_trial(self.project, **options)
        self.plan_path.write_text(json.dumps(value))
        return value

    def builder(self, specs, env_file, reference_dir, *, enabled, web_research_enabled=False):
        self.assertIs(enabled, True)
        owner = Mock()
        owner.preflight = {"synthetic": True, "metadata_only": True}
        def create(contract, location, **options):
            answers = [{"category": "documentation", "reason": "synthetic failure"},
                       {"explanation": "synthetic proposal", "patch": {"edits": [{"path": "README.md", "old": "old", "new": "new"}]}}] * 2
            transport = SyntheticTransport(answers)
            ledger = ModelLedger(location / "private-ledger.sqlite3", options["run_id"], options["max_usd"])
            planner = RepairPlanner(ModelCaller(transport, ledger, deadline=options["deadline"]),
                                    specs["nano"], specs["ultra"], route=PROTOCOLS[options["group"]]["route"])
            if web_research_enabled:
                from fresh_eyes.web_research import WebResearcher
                from test_web_research import SyntheticTavily, RESPONSE
                planner.researcher = WebResearcher(SyntheticTavily(RESPONSE))
            self.planners.append(planner)
            return planner
        owner.side_effect = create
        return owner

    def execute(self, verdicts, **options):
        return run_trial(self.project, self.plan_path, execute=True, enable_paid_models=True,
                         env_file=self.root / "NOT_READ.env", reference_dir=self.root / "NOT_READ-references",
                         factory_builder=self.builder, runner_factory=lambda c: ScriptedRunner(verdicts), **options)

    def test_plan_and_preview_do_not_read_keys_start_docker_or_create_trial_output(self):
        with patch("fresh_eyes.model_trial.OwnerModelFactory") as owner, patch("fresh_eyes.model_trial.DockerRunner") as docker:
            value = self.plan()
            result = run_trial(self.project, self.plan_path, env_file=self.root / "missing.env")
        owner.assert_not_called()
        docker.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(result["key_read"])
        self.assertFalse(value["evaluation_eligible"])
        self.assertEqual(value["max_model_requests"], 2)
        self.assertEqual(self.contract.data["review"]["status"], "development_reviewed")

    def test_holdout_or_draft_review_cannot_enter_trial(self):
        (self.project / "cases/holdout").mkdir()
        (self.project / "cases/holdout/unit.json").write_bytes(self.case.read_bytes())
        self.case.unlink()
        with self.assertRaisesRegex(InvalidCase, "holdouts"):
            self.plan()
        self.case.write_text(json.dumps(self.contract.data))
        payload = json.loads(self.approvals.read_text())
        payload["approvals"][0]["status"] = "draft"
        payload["payload_sha256"] = digest(payload["approvals"])
        self.approvals.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "attributed"):
            self.plan()

    def test_public_model_config_rejects_credentials_and_duplicate_fields(self):
        value = json.loads(self.config.read_text())
        value["nano"]["api_key"] = "SYNTHETIC-SECRET"
        self.config.write_text(json.dumps(value))
        with self.assertRaisesRegex(InvalidCase, "public"):
            load_public_specs(self.config)
        self.config.write_text('{"nano":{},"nano":{},"ultra":{}}')
        with self.assertRaisesRegex(InvalidCase, "Duplicate"):
            load_public_specs(self.config)

    def test_execution_requires_all_paid_flags_and_positive_budget_before_output_or_key(self):
        self.plan(budget_usd="0")
        with patch("fresh_eyes.model_trial.OwnerModelFactory") as owner:
            for options in ({"execute": True}, {"enable_paid_models": True},
                            {"execute": True, "enable_paid_models": True, "env_file": "unused", "reference_dir": "unused"}):
                with self.assertRaises(InvalidCase):
                    run_trial(self.project, self.plan_path, **options)
        owner.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_changed_code_or_wheels_stops_before_activation(self):
        self.plan()
        marker = self.project / "src/fixture.py"
        marker.write_text("# changed\n")
        with self.assertRaisesRegex(InvalidCase, "changed"):
            self.execute([False, True, True])
        self.assertFalse(self.output.exists())
        self.plan()
        (self.wheels / "synthetic.whl").write_bytes(b"not a real wheel, never installed")
        with self.assertRaisesRegex(InvalidCase, "changed"):
            self.execute([False, True, True])
        self.assertEqual(self.planners, [])

    def test_pinned_source_and_immutable_image_are_required(self):
        for changes in ({"runner_id": "python:3.12-slim"}, {"source": self.root / "missing"}):
            with self.assertRaises(InvalidCase):
                self.plan(**changes)
        data = self.contract.data
        data["repo"]["commit"] = "b" * 40
        self.case.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.plan()

    def test_healthy_baseline_does_not_activate_or_read_key(self):
        self.plan()
        result = self.execute([True])
        self.assertEqual(result["status"], "already_passes")
        self.assertEqual(self.planners, [])
        self.assertFalse(result["key_read"])
        self.assertFalse((self.output / "private-ledger.sqlite3").exists())

    def test_research_option_is_bound_in_preview_and_synthetic_trial_report(self):
        value = self.plan(web_research_enabled=True)
        preview = run_trial(self.project, self.plan_path)
        self.assertTrue(value['inputs']['web_research_enabled'])
        self.assertEqual(preview['max_research_credits'], 2)
        self.assertFalse(preview['key_read'])
        self.execute([False, True, True])
        report = json.loads((self.output / 'report.json').read_text())
        self.assertEqual(report['diagnosis']['web_research'][0]['credits'], 1)
        self.assertEqual(report['diagnosis']['web_research'][0]['data_origin'], 'engineering_test')
        self.assertEqual(report['model_calls'], 0)

    def test_unavailable_runtime_never_activates_models(self):
        self.plan()
        runner = Mock()
        runner.dependency_mode = "offline_snapshot"
        runner.preflight.side_effect = InfrastructureError("synthetic unavailable runtime")
        with patch("fresh_eyes.model_trial.OwnerModelFactory") as owner:
            result = run_trial(self.project, self.plan_path, execute=True, enable_paid_models=True,
                               env_file="unused", reference_dir="unused", runner_factory=lambda c: runner)
        owner.assert_not_called()
        self.assertEqual(result["status"], "infrastructure_error")
        self.assertFalse(result["key_read"])

    def test_two_round_trial_retains_full_candidates_and_never_enters_evaluation(self):
        self.plan(max_patch_rounds=2)
        result = self.execute([False, False, True, True])
        report = json.loads((self.output / "report.json").read_text())
        record = json.loads((self.output / "run-record.json").read_text())
        ledger = json.loads((self.output / "model-ledger.json").read_text())
        self.assertEqual([r["status"] for r in report["repair_rounds"]], ["validation_failed", "verified_fix"])
        self.assertEqual(len(self.planners[0].caller.transport.payloads), 4)
        self.assertEqual(len(ledger["calls"]), 4)
        self.assertEqual(ledger["model_calls"], 0)
        self.assertEqual(ledger["engineering_test_calls"], 4)
        self.assertEqual(record["data_origin"], "engineering_test")
        self.assertEqual(record["costs"]["billing_status"], "engineering_test")
        self.assertFalse(record["evaluation_eligible"])
        self.assertTrue(all(r["candidate_patch"]["patch"]["edits"] for r in report["repair_rounds"]))
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse((self.output / "binding.json").exists())
        with zipfile.ZipFile(result["package"]) as archive:
            self.assertIsNone(archive.testzip())
            self.assertTrue(any(name.endswith("run-record.json") for name in archive.namelist()))
            self.assertFalse(any(name.endswith((".sqlite3", ".env")) for name in archive.namelist()))
        with self.assertRaisesRegex(InvalidCase, "already exists"):
            self.execute([False, True, True])

    def test_all_ultra_uses_ultra_for_classification(self):
        self.plan(route="all_ultra")
        self.execute([False, True, True])
        self.assertTrue(all(p["model"] == "ultra-test" for p in self.planners[0].caller.transport.payloads))

    def test_rejected_first_candidate_is_retained_when_second_round_passes(self):
        self.plan(max_patch_rounds=2)
        original = self.builder
        def rejected(*args, **kwargs):
            factory = original(*args, **kwargs)
            create = factory.side_effect
            def wrapped(*a, **kw):
                planner = create(*a, **kw)
                def transform(response):
                    if len(planner.caller.transport.payloads) == 2:
                        answer = json.loads(response["choices"][0]["message"]["content"])
                        answer["patch"]["edits"][0]["old"] = "MISSING-SYNTHETIC-TEXT"
                        response["choices"][0]["message"]["content"] = json.dumps(answer)
                    return response
                planner.caller.transport.transform = transform
                return planner
            factory.side_effect = wrapped
            return factory
        self.builder = rejected
        self.execute([False, True, True])
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual([r["status"] for r in report["repair_rounds"]], ["rejected", "verified_fix"])
        self.assertEqual(report["repair_rounds"][0]["candidate_patch"]["patch"]["edits"][0]["old"], "MISSING-SYNTHETIC-TEXT")
        self.assertEqual(len(report["attempts"]), 3)

    def test_cli_preview_does_not_implicitly_enable_execution(self):
        from fresh_eyes.cli import main
        with patch("sys.argv", ["fresh-eyes", "model-trial-run", "--plan", str(self.plan_path)]), \
             patch("fresh_eyes.model_trial.run_trial", return_value={"action": "preview", "model_calls": 0}) as run, \
             patch("builtins.print"):
            self.assertEqual(main(), 0)
        self.assertFalse(run.call_args.kwargs["execute"])
        self.assertFalse(run.call_args.kwargs["enable_paid_models"])
        self.assertIsNone(run.call_args.kwargs["env_file"])

    def test_unknown_charge_stops_without_second_round_or_retry(self):
        self.plan(max_patch_rounds=2)
        original = self.builder
        def missing_usage(*args, **kwargs):
            factory = original(*args, **kwargs)
            create = factory.side_effect
            def wrapped(*a, **kw):
                planner = create(*a, **kw)
                planner.caller.transport.transform = lambda r: {k: v for k, v in r.items() if k != "usage"}
                return planner
            factory.side_effect = wrapped
            return factory
        self.builder = missing_usage
        result = self.execute([False])
        self.assertEqual(len(self.planners[0].caller.transport.payloads), 1)
        self.assertEqual(result["stop_reason"], "billing_unresolved")
        record = json.loads((self.output / "run-record.json").read_text())
        self.assertIsNone(record["costs"]["model_usd"])

    def test_catalog_failure_is_redacted_and_activation_is_not_reported_as_key_unread(self):
        self.plan()
        with patch("fresh_eyes.model_trial.OwnerModelFactory", side_effect=ModelError("catalog_unverified", "SYNTHETIC-SECRET")):
            result = run_trial(self.project, self.plan_path, execute=True, enable_paid_models=True,
                               env_file="unused", reference_dir="unused", runner_factory=lambda c: ScriptedRunner([False]))
        self.assertEqual(result["status"], "infrastructure_error")
        self.assertIsNone(result["key_read"])
        for name in ("trial-binding.json", "report.json", "run-record.json"):
            self.assertNotIn("SYNTHETIC-SECRET", (self.output / name).read_text())

    def test_interrupt_preserves_ledger_and_does_not_invent_a_final_result(self):
        self.plan()
        original = self.builder
        def interrupted(*args, **kwargs):
            factory = original(*args, **kwargs)
            create = factory.side_effect
            def wrapped(*a, **kw):
                planner = create(*a, **kw)
                planner.propose = Mock(side_effect=KeyboardInterrupt)
                return planner
            factory.side_effect = wrapped
            return factory
        self.builder = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.execute([False])
        binding = json.loads((self.output / "trial-binding.json").read_text())
        self.assertEqual(binding["run_state"], "interrupted")
        self.assertTrue((self.output / "model-ledger.json").exists())
        self.assertFalse((self.output / "run-record.json").exists())


if __name__ == "__main__":
    unittest.main()
