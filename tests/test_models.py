"""Synthetic model responses only. No inference service or model credits."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from fresh_eyes.contracts import Contract
from fresh_eyes.diagnostics import RepairPlanner, candidate, context, repair_plan, scrub
from fresh_eyes.engine import run_case
from fresh_eyes.models import (ENDPOINT, ModelCaller, ModelError, ModelLedger,
                               ModelSpec, NebiusTransport, usage_and_cost)
from test_core import ScriptedRunner, case_data
from test_spending import verified_guard


def spec(name="nano-test", input_rate="0.06", output_rate="0.24", **extra):
    return ModelSpec(name, 4096, 512, input_rate, output_rate, "synthetic test rates; not a provider quote", **extra)


class SyntheticTransport:
    enabled = True
    data_origin = "engineering_test"
    api_key = "synthetic-control-secret"

    def __init__(self, answers, transform=None):
        self.answers = iter(answers)
        self.payloads = []
        self.transform = transform or (lambda response: response)

    def complete(self, payload, **kwargs):
        self.payloads.append(payload)
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        response = {"id": "chatcmpl-synthetic", "model": payload["model"],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}
        return self.transform(response)


class ModelLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_paid_disabled_and_zero_budget_both_prevent_network_dispatch(self):
        transport = NebiusTransport("synthetic-key",spend_guard=verified_guard(self.path.parent/"global"))
        ledger = ModelLedger(self.path, "test-run", "0")
        caller = ModelCaller(transport, ledger)
        with patch.object(transport.opener, "open") as opened:
            with self.assertRaisesRegex(ModelError, "disabled"):
                caller.call("classify", spec(), [{"role": "user", "content": "JSON"}])
            transport.enabled = True
            with self.assertRaisesRegex(ModelError, "insufficient"):
                caller.call("classify", spec(), [{"role": "user", "content": "JSON"}])
        opened.assert_not_called()
        self.assertEqual(ledger.summary()["calls"], [])

    def test_reservation_is_atomic_across_concurrent_callers(self):
        model = spec()
        ledger = ModelLedger(self.path, "run", model.reservation)
        def reserve(_):
            try:
                ledger.reserve("classify", model, "engineering_test", "hash")
                return True
            except ModelError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(reserve, [1, 2])), 1)
        self.assertEqual(Decimal(ledger.summary()["held_usd"]), model.reservation)

    def test_restart_keeps_pending_reservation_and_rejects_budget_reset(self):
        model = spec()
        ModelLedger(self.path, "run", model.reservation).reserve("classify", model, "engineering_test", "hash")
        reopened = ModelLedger(self.path, "run", model.reservation)
        self.assertEqual(reopened.summary()["engineering_test_calls"], 0)
        with self.assertRaises(ModelError):
            reopened.reserve("diagnose", model, "engineering_test", "hash")
        with self.assertRaises(ModelError):
            ModelLedger(self.path, "run", "100")

    def test_invalid_key_does_not_create_a_request_or_hold(self):
        transport = NebiusTransport("bad\nkey", enabled=True)
        ledger = ModelLedger(self.path, "run", "1")
        with patch.object(transport.opener, "open") as opened:
            with self.assertRaisesRegex(ModelError, "valid API key"):
                ModelCaller(transport, ledger).call("classify", spec(), [{"role": "user", "content": "JSON"}])
        opened.assert_not_called()
        self.assertEqual(ledger.summary()["calls"], [])

    def test_unknown_provider_balance_blocks_real_transport_before_any_http_or_reservation(self):
        from fresh_eyes.spending import SpendGuard
        transport=NebiusTransport('synthetic-key',enabled=True,spend_guard=SpendGuard(self.path.parent/'unconfigured'))
        ledger=ModelLedger(self.path,'run','1')
        with patch.object(transport.opener,'open') as opened,self.assertRaises(ModelError):
            ModelCaller(transport,ledger).call('classify',spec(),[{'role':'user','content':'JSON'}])
        opened.assert_not_called()
        self.assertEqual(ledger.summary()['calls'],[])

    def test_string_false_does_not_enable_paid_requests(self):
        transport = NebiusTransport("synthetic-key", enabled="false")
        with patch.object(transport.opener, "open") as opened:
            with self.assertRaisesRegex(ModelError, "disabled"):
                transport.complete({})
        opened.assert_not_called()

    def test_control_key_is_redacted_even_when_present_in_a_message(self):
        ledger = ModelLedger(self.path, "run", "1")
        transport = SyntheticTransport([{}])
        ModelCaller(transport, ledger).call("classify", spec(), [{"role": "user", "content": "JSON: " + transport.api_key}])
        self.assertNotIn(transport.api_key, json.dumps(transport.payloads))
        self.assertNotIn(transport.api_key, json.dumps(ledger.summary()))

    def test_budget_does_not_accept_nonfinite_or_negative_values(self):
        for amount in ["NaN", "Infinity", "-0.01"]:
            with self.subTest(amount=amount), self.assertRaises(ModelError):
                ModelLedger(self.path, "run", amount)

    def test_reasoning_exhaustion_keeps_usage_and_stop_metadata_without_reasoning_text_or_retry(self):
        def exhausted(response):
            response['usage']['completion_tokens_details']={'reasoning_tokens':50}
            response['choices'][0]={'finish_reason':'length','message':{'content':'','reasoning_content':'PRIVATE_SYNTHETIC_REASONING'}}
            return response
        transport=SyntheticTransport([{}],transform=exhausted)
        ledger=ModelLedger(self.path,'run','1')
        with self.assertRaises(ModelError) as error:
            ModelCaller(transport,ledger).call('diagnose',spec(),[{'role':'user','content':'JSON'}])
        self.assertEqual(error.exception.code,'incomplete_output')
        entry=ledger.summary()['calls'][0]
        self.assertEqual(entry['output_diagnostic'],'reasoning_exhausted_completion')
        self.assertEqual(entry['response_finish_reason'],'length')
        self.assertEqual(entry['response_content_bytes'],0)
        self.assertIsNotNone(entry['calculated_usd'])
        self.assertNotIn('PRIVATE_SYNTHETIC_REASONING',json.dumps(entry))
        self.assertEqual(len(transport.payloads),1)

    def test_truncated_json_is_never_repaired_or_accepted_locally(self):
        def truncated(response):
            response['choices'][0]={'finish_reason':'length','message':{'content':'{"patch":'}}
            return response
        transport=SyntheticTransport([{}],transform=truncated)
        ledger=ModelLedger(self.path,'run','1')
        with self.assertRaises(ModelError):
            ModelCaller(transport,ledger).call('diagnose',spec(),[{'role':'user','content':'JSON'}])
        entry=ledger.summary()['calls'][0]
        self.assertEqual(entry['output_diagnostic'],'completion_truncated')
        self.assertEqual(entry['response_content_bytes'],9)
        self.assertEqual(len(transport.payloads),1)

    def test_completed_stage_cannot_be_dispatched_again_in_the_same_ledger(self):
        transport = SyntheticTransport([{}, {}])
        caller = ModelCaller(transport, ModelLedger(self.path, 'run', '1'))
        messages = [{'role':'user','content':'JSON'}]
        caller.call('classify',spec(),messages)
        with self.assertRaisesRegex(ModelError,'already has a durable reservation'):
            caller.call('classify',spec(),messages)
        self.assertEqual(len(transport.payloads),1)

    def test_expired_deadline_after_reservation_sends_nothing_and_releases_hold(self):
        import time
        transport = SyntheticTransport([{}])
        ledger = ModelLedger(self.path,'run','1')
        caller = ModelCaller(transport,ledger,deadline=time.monotonic()+10)
        original = ledger.reserve
        def expire(*args,**kwargs):
            result = original(*args,**kwargs)
            caller.deadline = time.monotonic()-1
            return result
        with patch.object(ledger,'reserve',side_effect=expire), self.assertRaisesRegex(ModelError,'time limit'):
            caller.call('classify',spec(),[{'role':'user','content':'JSON'}])
        self.assertEqual(transport.payloads,[])
        summary=ledger.summary()
        self.assertEqual(Decimal(summary['held_usd']),0)
        self.assertEqual(summary['calls'][0]['billing_status'],'not_dispatched')

    def test_timeout_is_recorded_once_with_unknown_charge_and_blocks_more_calls(self):
        transport = SyntheticTransport([ModelError("request_error", "Synthetic timeout")])
        ledger = ModelLedger(self.path, "run", "1")
        caller = ModelCaller(transport, ledger)
        with self.assertRaises(ModelError):
            caller.call("classify", spec(), [{"role": "user", "content": "JSON"}])
        with self.assertRaisesRegex(ModelError, "previous charge"):
            caller.call("classify", spec(), [{"role": "user", "content": "JSON"}])
        report = ledger.summary()
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(report["calls"][0]["status"], "request_error")
        self.assertIsNone(report["calculated_usd"])
        self.assertIsNone(report["billed_usd"])
        self.assertEqual(Decimal(report["held_usd"]), spec().reservation)

    def test_usage_cost_and_billing_are_distinct_and_test_calls_are_labelled(self):
        ledger = ModelLedger(self.path, "run", "1")
        caller = ModelCaller(SyntheticTransport([{"category": "documentation", "reason": "test"}]), ledger)
        caller.call("classify", spec(), [{"role": "user", "content": "JSON"}])
        summary = ledger.summary()
        self.assertEqual(Decimal(summary["calculated_usd"]), Decimal("0.000018"))
        self.assertIsNone(summary["billed_usd"])
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["engineering_test_calls"], 1)
        self.assertEqual(summary["calls"][0]["billing_status"], "usage_calculated_pending_reconciliation")

    def test_reasoning_is_not_charged_twice_and_missing_cached_rate_is_unknown(self):
        response = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                              "completion_tokens_details": {"reasoning_tokens": 40}}}
        _, amount, _ = usage_and_cost(response, spec())
        self.assertEqual(Decimal(amount), Decimal("0.000018"))
        response["usage"]["prompt_tokens_details"] = {"cached_tokens": 80}
        _, amount, status = usage_and_cost(response, spec())
        self.assertIsNone(amount)
        self.assertEqual(status, "unknown_cached_rate")
        _, amount, _ = usage_and_cost(response, spec(cached_input_per_million="0.03"))
        self.assertEqual(Decimal(amount), Decimal("0.0000156"))

    def test_invalid_usage_is_never_zero_cost(self):
        for usage in [{}, {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
                      {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 9}]:
            self.assertIsNone(usage_and_cost({"usage": usage}, spec())[1])

    def test_model_mismatch_cannot_use_requested_models_price(self):
        def different(response):
            response["model"] = "different-model"
            return response
        ledger = ModelLedger(self.path, "run", "1")
        transport = SyntheticTransport([{}], different)
        with self.assertRaisesRegex(ModelError, "returned model"):
            ModelCaller(transport, ledger).call("classify", spec(), [{"role": "user", "content": "JSON"}])
        self.assertIsNone(ledger.summary()["calculated_usd"])
        self.assertEqual(ledger.summary()["calls"][0]["returned_model"], "different-model")

    def test_transport_uses_fixed_endpoint_and_completion_cap_with_no_retry(self):
        transport = NebiusTransport("synthetic-key", enabled=True,spend_guard=verified_guard(self.path.parent/"global"))
        transport.data_origin = "engineering_test"
        raw = {"id": "chatcmpl-test", "model": "nano-test", "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        with patch.object(transport.opener, "open", return_value=io.BytesIO(json.dumps(raw).encode())) as opened:
            ModelCaller(transport, ModelLedger(self.path, "run", "1")).call("classify", spec(), [{"role": "user", "content": "JSON"}])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        payload = json.loads(request.data)
        self.assertEqual(payload["max_completion_tokens"], 512)
        self.assertFalse(payload["stream"])
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_reviewed_request_options_reach_the_payload_and_ledger_but_never_override_fixed_fields(self):
        options = {"chat_template_kwargs": {"enable_thinking": True, "medium_effort": True}, "temperature": 1.0, "top_p": 0.95}
        transport = SyntheticTransport([{}])
        ledger = ModelLedger(self.path, "run", "1")
        ModelCaller(transport, ledger).call("diagnose", spec(request_options=options), [{"role": "user", "content": "JSON"}])
        payload = transport.payloads[0]
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": True, "medium_effort": True})
        self.assertEqual((payload["temperature"], payload["top_p"], payload["max_completion_tokens"]), (1.0, 0.95, 512))
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(ledger.summary()["calls"][0]["request_options"], options)
        ModelCaller(SyntheticTransport([{}]), ModelLedger(Path(self.tmp.name) / "plain.sqlite3", "run", "1")).call(
            "classify", spec(), [{"role": "user", "content": "JSON"}])
        self.assertIsNone(ModelLedger(Path(self.tmp.name) / "plain.sqlite3", "run", "1").summary()["calls"][0]["request_options"])

    def test_request_options_outside_the_reviewed_set_are_refused(self):
        for bad in [{}, {"max_completion_tokens": 99999}, {"model": "other"}, {"response_format": None}, {"stream": True},
                    {"chat_template_kwargs": {}}, {"chat_template_kwargs": {"enable_thinking": "no"}},
                    {"chat_template_kwargs": {"reasoning_budget": 100}}, {"chat_template_kwargs": None},
                    {"temperature": 3}, {"temperature": float("nan")}, {"temperature": True}, {"top_p": 0}, {"top_p": 1.5}, []]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ModelError, "Request options"):
                spec(request_options=bad)

    def test_nested_request_options_mutation_is_refused_before_dispatch(self):
        options={'top_p':.9};configured=spec(request_options=options);options['unreviewed_field']=True
        transport=SyntheticTransport([{}]);ledger=ModelLedger(self.path,'run','1')
        with self.assertRaises(ModelError):ModelCaller(transport,ledger).call('diagnose',configured,[{'role':'user','content':'JSON'}])
        self.assertEqual(transport.payloads,[])
        self.assertEqual(ledger.summary()['calls'],[])

    def test_spec_dict_keeps_the_frozen_shape_of_specs_without_request_options(self):
        from fresh_eyes.models import spec_dict
        self.assertNotIn("request_options", spec_dict(spec()))
        self.assertEqual(spec_dict(spec(request_options={"top_p": 0.9}))["request_options"], {"top_p": 0.9})
        self.assertEqual(ModelSpec(**spec_dict(spec(request_options={"top_p": 0.9}))), spec(request_options={"top_p": 0.9}))

    def test_http_errors_are_redacted_and_closed(self):
        transport = NebiusTransport("synthetic-key", enabled=True,spend_guard=verified_guard(self.path.parent/"global"))
        body = io.BytesIO(b"synthetic-secret-error-body")
        error = HTTPError(ENDPOINT, 429, "synthetic-secret-error-body", {}, body)
        reservation=transport.spend_guard.reserve("nebius",".01","synthetic-http-error")
        with patch.object(transport.opener, "open", side_effect=error) as opened:
            with self.assertRaises(ModelError) as raised:
                transport.complete({},reservation=reservation)
        self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(body.closed)


class DiagnosticEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "repo"; self.source.mkdir()
        (self.source / "README.md").write_text("pip install old\nNEBIUS_API_KEY=synthetic-control-secret\n")
        for args in [["git", "init", "-q", str(self.source)], ["git", "-C", str(self.source), "add", "README.md"],
                     ["git", "-C", str(self.source), "-c", "user.name=Engineering Test", "-c", "user.email=test@example.invalid",
                      "-c", "core.hooksPath=/dev/null", "commit", "-qm", "synthetic diagnostic input"]]:
            subprocess.run(args, check=True, capture_output=True)
        sha = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        self.contract = Contract.from_dict(case_data(sha))
        self.wheels = self.root / "wheels"; self.wheels.mkdir()
        self.nano = spec()
        self.ultra = spec("ultra-test", "1", "3")
        self.answers = [{"category": "documentation", "reason": "Synthetic documented install mismatch"},
                        {"explanation": "Synthetic candidate", "patch": {"edits": [{"path": "README.md", "old": "old", "new": "new"}]}}]

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, verdicts, answers=None, *, budget="1", route="nano_ultra", transform=None, rounds=1, time_limit=None):
        self.transport = SyntheticTransport(self.answers if answers is None else answers, transform)
        self.ledger = ModelLedger(self.root / "ledger.sqlite3", "run", budget)
        planner = RepairPlanner(ModelCaller(self.transport, self.ledger), self.nano, self.ultra, route=route)
        self.runner = verdicts if isinstance(verdicts, ScriptedRunner) else ScriptedRunner(verdicts)
        return run_case(self.contract, self.source, self.wheels, self.root / "report", runner=self.runner,
                        development=True, repairer=planner, max_patch_rounds=rounds, time_limit_s=time_limit)

    def test_two_rounds_use_original_patch_base_and_latest_failed_validation(self):
        second = {'explanation':'SYNTHETIC replacement candidate', 'patch':{'edits':[{'path':'README.md','old':'old','new':'final'}]}}
        report=self.execute([False,False,True,True],self.answers+[self.answers[0],second],rounds=2)
        self.assertEqual(report['status'],'verified_fix')
        self.assertEqual([r['status'] for r in report['repair_rounds']],['validation_failed','verified_fix'])
        self.assertEqual([e['stage'] for e in self.ledger.summary()['calls']],['classify','diagnose','classify_r2','diagnose_r2'])
        self.assertEqual([a.get('round') for a in report['attempts']],[None,1,2,2])
        self.assertIn('+pip install final',report['patch']['diff'])
        self.assertNotIn('-pip install new',report['patch']['diff'])
        second_context=json.loads(self.transport.payloads[2]['messages'][1]['content'])['evidence']
        self.assertEqual(second_context['repair_round'],2)
        self.assertIn('pip install old',second_context['files']['README.md']['text'])
        self.assertEqual(second_context['previous_candidate']['status'],'validation_failed')
        self.assertEqual(len({a['acceptance_sha256'] for a in report['attempts']}),1)

    def test_match_rejection_can_use_one_explicit_second_round_but_keeps_raw_proposal(self):
        bad={'explanation':'SYNTHETIC mismatch','patch':{'edits':[{'path':'README.md','old':'absent','new':'x'}]}}
        report=self.execute([False,True,True],[self.answers[0],bad]+self.answers,rounds=2)
        self.assertEqual(report['status'],'verified_fix')
        self.assertEqual(report['repair_rounds'][0]['candidate_patch']['patch'],bad['patch'])
        self.assertEqual(report['repair_rounds'][0]['candidate_patch']['status'],'rejected')
        self.assertEqual(len(self.transport.payloads),4)
        self.assertEqual(len(report['attempts']),3)

    def test_success_stops_before_second_round(self):
        report=self.execute([False,True,True],rounds=2)
        self.assertEqual(len(report['repair_rounds']),1)
        self.assertEqual(len(self.transport.payloads),2)

    def test_all_ultra_two_rounds_have_four_ultra_calls_at_most(self):
        report=self.execute([False,True,False,True,True],self.answers+self.answers,rounds=2,route='all_ultra')
        self.assertEqual(report['status'],'verified_fix')
        self.assertEqual([p['model'] for p in self.transport.payloads],['ultra-test']*4)
        self.assertEqual(len(report['attempts']),5)

    def test_second_round_shares_original_budget_and_stops_before_ultra(self):
        report=self.execute([False,False],self.answers+self.answers,rounds=2,budget='0.0057')
        self.assertEqual(report['status'],'unresolved')
        self.assertEqual(report['reason'],'budget_exhausted')
        self.assertEqual(len(self.transport.payloads),3)
        self.assertLessEqual(Decimal(self.ledger.summary()['held_usd']),Decimal('0.0057'))

    def test_transport_failure_never_enters_another_round(self):
        report=self.execute([False],[ModelError('request_error','SYNTHETIC timeout')],rounds=2)
        self.assertEqual(report['status'],'unresolved')
        self.assertEqual(len(self.transport.payloads),1)
        self.assertEqual(len(report['repair_rounds']),1)

    def test_two_failed_candidates_stop_at_the_second_round(self):
        report=self.execute([False,False,False],self.answers+self.answers,rounds=2)
        self.assertEqual(report['status'],'unresolved')
        self.assertEqual(len(report['repair_rounds']),2)
        self.assertEqual(len(self.transport.payloads),4)
        self.assertEqual(len(report['attempts']),3)

    def test_forbidden_patch_target_stops_instead_of_using_another_round(self):
        bad={'explanation':'SYNTHETIC forbidden target','patch':{'edits':[{'path':'main.py','old':'x','new':'y'}]}}
        report=self.execute([False],[self.answers[0],bad],rounds=2)
        self.assertEqual(report['status'],'unresolved')
        self.assertEqual(report['reason'],'patch_out_of_scope')
        self.assertEqual(len(self.transport.payloads),2)
        self.assertEqual(len(report['repair_rounds']),1)

    def test_infrastructure_failure_during_candidate_stops_without_second_round(self):
        from fresh_eyes.runner import InfrastructureError
        class BrokenCandidate(ScriptedRunner):
            def run(self,bundle):
                if self.payloads:raise InfrastructureError('SYNTHETIC worker failure')
                return super().run(bundle)
        report=self.execute(BrokenCandidate([False]),rounds=2)
        self.assertEqual(report['status'],'infrastructure_error')
        self.assertEqual(report['repair_rounds'][0]['status'],'infrastructure_error')
        self.assertEqual(len(self.transport.payloads),2)

    def test_two_roles_propose_one_patch_but_host_and_final_run_decide_success(self):
        report = self.execute([False, True, True])
        self.assertEqual(report["status"], "verified_fix")
        self.assertEqual(report["repair_author"], "engineering_test")
        self.assertEqual(report["data_origin"], "engineering_test")
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual([p["model"] for p in self.transport.payloads], ["nano-test", "ultra-test"])
        self.assertEqual(len(report["attempts"]), 3)
        self.assertEqual(len({a["acceptance_sha256"] for a in report["attempts"]}), 1)
        self.assertNotIn("synthetic-control-secret", json.dumps(self.transport.payloads))
        self.assertTrue(all("expected" not in json.dumps(p) for p in self.runner.payloads))
        self.assertEqual((self.source / "README.md").read_text().splitlines()[0], "pip install old")
        self.assertIn("engineering_test", (self.root / "report/report.html").read_text())

    def test_context_exposes_supplied_input_metadata_without_input_or_answer_bodies(self):
        data=self.contract.data
        data['fixtures'][0]['input']='PRIVATE_FIXTURE_INPUT_Ω'
        data['fixtures'][0]['expected']={'answer':'PRIVATE_EXPECTED_SENTINEL'}
        contract=Contract.from_dict(data)
        evidence=context(contract,{'result':{'outputs':[{'name':'one.json','exit_code':127}]}},self.source,
                         secrets=('synthetic-control-secret',))
        first,second=evidence['fixture_inputs']
        self.assertEqual(first,{'name':'one.json','utf8_bytes':len('PRIVATE_FIXTURE_INPUT_Ω'.encode()),
                                'workflow_attempt_recorded':True})
        self.assertFalse(second['workflow_attempt_recorded'])
        raw=json.dumps(evidence,ensure_ascii=False)
        for hidden in ('PRIVATE_FIXTURE_INPUT','PRIVATE_EXPECTED_SENTINEL','synthetic-control-secret'):
            self.assertNotIn(hidden,raw)

    def test_input_metadata_never_overrides_a_missing_input_model_decision(self):
        report=self.execute([False],answers=[{'category':'missing_input','reason':'Synthetic missing credential'}])
        self.assertEqual(report['status'],'needs_review')
        self.assertEqual(len(self.transport.payloads),1)
        self.assertEqual(len(report['attempts']),1)
        evidence=json.loads(self.transport.payloads[0]['messages'][1]['content'])['evidence']
        self.assertTrue(all(f['workflow_attempt_recorded'] for f in evidence['fixture_inputs']))

    def test_line_edit_candidate_is_applied_then_proven_by_both_clean_runs(self):
        line = {"explanation": "Synthetic line candidate", "patch": {"edits": [
            {"path": "README.md", "start": 1, "end": 1, "old": "pip install old", "new": "pip install new"}]}}
        report = self.execute([False, True, True], [self.answers[0], line])
        self.assertEqual(report["status"], "verified_fix")
        self.assertIn("+pip install new", report["patch"]["diff"])
        evidence = json.loads(self.transport.payloads[1]["messages"][1]["content"])["evidence"]
        self.assertIn([1, "pip install old"], evidence["files"]["README.md"]["cited_lines"])

    def test_wrong_line_is_refused_and_round_two_sees_what_it_says_without_secrets(self):
        wrong = {"explanation": "Synthetic wrong line", "patch": {"edits": [
            {"path": "README.md", "start": 2, "end": 2, "old": "pip install old", "new": "pip install new"}]}}
        right = {"explanation": "Synthetic corrected line", "patch": {"edits": [
            {"path": "README.md", "start": 1, "end": 1, "old": "pip install old", "new": "pip install new"}]}}
        report = self.execute([False, True, True], [self.answers[0], wrong, self.answers[0], right], rounds=2)
        self.assertEqual([r["status"] for r in report["repair_rounds"]], ["rejected", "verified_fix"])
        self.assertIn("Lines 2-2 of README.md", report["repair_rounds"][0]["reason"])
        second = json.loads(self.transport.payloads[3]["messages"][1]["content"])["evidence"]
        self.assertIn("Lines 2-2 of README.md", second["previous_candidate"]["reason"])
        # The refusal quotes the real line for round 2 and the saved report, redacted like model evidence.
        self.assertIn("NEBIUS_API_KEY=[REDACTED]", report["repair_rounds"][0]["reason"])
        self.assertNotIn("synthetic-control-secret", json.dumps(self.transport.payloads) + report["repair_rounds"][0]["reason"])

    def test_all_ultra_uses_same_flow_with_both_roles_on_ultra(self):
        report = self.execute([False, True, True], route="all_ultra")
        self.assertEqual(report["status"], "verified_fix")
        self.assertEqual([p["model"] for p in self.transport.payloads], ["ultra-test", "ultra-test"])

    def test_removed_setup_command_is_not_executed_and_next_round_can_correct_it(self):
        bad={'explanation':'Synthetic setup mismatch','patch':{'edits':[
            {'path':'README.md','old':'pip install old','new':'pip install new'}],
            'setup':[{'argv':['pip','install','old'],'source':'README.md:L1'},
                     {'argv':['pip','install','new'],'source':'README.md:L1'}]}}
        good={'explanation':'Synthetic corrected setup','patch':{'edits':[
            {'path':'README.md','old':'pip install old','new':'pip install new'}],
            'setup':[{'argv':['pip','install','new'],'source':'README.md:L1'}]}}
        report=self.execute([False,True,True],[self.answers[0],bad,self.answers[0],good],rounds=2)
        self.assertEqual([r['status'] for r in report['repair_rounds']],['rejected','verified_fix'])
        self.assertEqual(len(report['attempts']),3)
        self.assertIn('pip install old',report['repair_rounds'][0]['reason'])
        for payload in self.runner.payloads[1:]:self.assertEqual(payload['setup'],good['patch']['setup'])

    def test_failed_final_verification_is_unresolved_even_with_valid_model_patch(self):
        report = self.execute([False, True, False])
        self.assertEqual(report["status"], "unresolved")

    def test_rejected_model_candidate_is_retained_without_becoming_an_applied_patch(self):
        answers = [self.answers[0], {"explanation": "SYNTHETIC bad target", "patch": {
            "edits": [{"path": "README.md", "old": "not in the source", "new": "replacement"}]}}]
        report = self.execute([False], answers)
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["candidate_patch"]["status"], "rejected")
        self.assertEqual(report["candidate_patch"]["patch"], answers[1]["patch"])
        self.assertIn("README.md matched 0 times", report["candidate_patch"]["reason"])
        self.assertNotIn("patch", report)
        self.assertEqual(len(report["attempts"]), 1)
        self.assertEqual(len(self.runner.payloads), 1)
        self.assertIn("pip install old", (self.source / "README.md").read_text())

    def test_healthy_or_infrastructure_failure_never_calls_a_model(self):
        report = self.execute([True], [])
        self.assertEqual(report["status"], "already_passes")
        self.assertEqual(self.transport.payloads, [])

    def test_infrastructure_failure_never_calls_a_model(self):
        class BrokenRunner(ScriptedRunner):
            def preflight(self):
                from fresh_eyes.runner import InfrastructureError
                raise InfrastructureError("Synthetic unavailable VM")
        report = self.execute(BrokenRunner([]), [])
        self.assertEqual(report["status"], "infrastructure_error")
        self.assertEqual(self.transport.payloads, [])

    def test_unknown_classification_needs_review_without_ultra(self):
        report = self.execute([False], [{"category": "missing_input", "reason": "Synthetic missing data"}])
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(len(self.transport.payloads), 1)
        self.assertNotIn("patch", report)

    def test_budget_stops_before_ultra_not_after(self):
        report = self.execute([False], budget="0.001")
        self.assertEqual(report["status"], "unresolved")
        self.assertEqual(report["reason"], "budget_exhausted")
        self.assertEqual(len(self.transport.payloads), 1)

    def test_missing_usage_stops_next_model_request(self):
        def no_usage(response):
            response.pop("usage")
            return response
        report = self.execute([False], transform=no_usage)
        self.assertEqual(report["reason"], "billing_unresolved")
        self.assertEqual(len(self.transport.payloads), 1)

    def test_source_patch_and_changed_acceptance_are_rejected(self):
        for answer in [{"explanation": "bad", "patch": {"edits": [{"path": "main.py", "old": "x", "new": "y"}]}},
                       {"explanation": "bad", "patch": {"edits": [{"path": "main.py", "start": 1, "end": 1, "old": "x", "new": "y"}]}},
                       {"explanation": "bad", "patch": {"edits": [{"path": "README.md", "start": 3, "end": 2, "old": "x", "new": "y"}]}},
                       {"explanation": "bad", "patch": {"edits": [{"path": "README.md", "start": "1", "end": 1, "old": "x", "new": "y"}]}},
                       {"explanation": "bad", "patch": {"edits": [{"path": "README.md", "start": 1, "old": "x", "new": "y"}]}},
                       {"explanation": "bad", "patch": {"edits": [], "fixtures": []}}]:
            with self.subTest(answer=answer), self.assertRaises(ModelError):
                candidate(json.dumps(answer))

    def test_line_edit_candidate_is_accepted(self):
        answer = {"explanation": "Install the package itself.",
                  "patch": {"edits": [{"path": "README.md", "start": 39, "end": 39, "old": "pip install -r requirements.txt", "new": "pip install ."}]}}
        self.assertEqual(candidate(json.dumps(answer))["patch"]["edits"][0]["start"], 39)

    def test_plan_is_read_only_and_infrastructure_has_no_model_stages(self):
        report = {
            "contract_sha256": self.contract.sha256, "acceptance_sha256": self.contract.acceptance_sha256,
            "status": "infrastructure_error", "attempts": []}
        plan = repair_plan(self.contract, report)
        self.assertEqual(plan["stages"], [])
        self.assertFalse(plan["executed"])
        report["status"] = "unresolved"
        report["attempts"] = [{"phase": "original_readme", "status": "failed", "acceptance_sha256": self.contract.acceptance_sha256}]
        self.assertEqual([s["model_role"] for s in repair_plan(self.contract, report)["stages"]], ["nano", "ultra"])
        report["acceptance_sha256"] = "wrong"
        with self.assertRaises(ModelError):
            repair_plan(self.contract, report)

    def test_examples_cannot_become_model_diagnosis_input(self):
        report = {"data_origin": "example", "contract_sha256": self.contract.sha256,
                  "acceptance_sha256": self.contract.acceptance_sha256, "attempts": []}
        with self.assertRaisesRegex(ModelError, "UI examples"):
            repair_plan(self.contract, report)

    def test_invalid_model_json_is_recorded_as_a_paid_attempt_not_retried(self):
        def incomplete(response):
            response["choices"][0]["message"]["content"] = '{"category":"documentation","category":"unknown","reason":"x"}'
            return response
        report = self.execute([False], transform=incomplete)
        self.assertEqual(report["status"], "unresolved")
        self.assertEqual(len(self.transport.payloads), 1)
        self.assertEqual(self.ledger.summary()["calls"][0]["status"], "invalid_model_output")
        self.assertIsNotNone(self.ledger.summary()["calculated_usd"])


if __name__ == "__main__":
    unittest.main()
