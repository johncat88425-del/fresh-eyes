"""Formal pipeline invariants use a synthetic inventory; no real holdouts run."""
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest

from fresh_eyes.api import Service
from fresh_eyes.contracts import Contract, InvalidCase
from fresh_eyes.evaluation import freeze, load_freeze, run_baselines, export_results, checked_ledger
from fresh_eyes.runner import InfrastructureError
from test_core import case_data, ScriptedRunner
from test_models import SyntheticTransport, spec
from fresh_eyes.models import ModelCaller, ModelLedger
from fresh_eyes.diagnostics import RepairPlanner
from fresh_eyes.protocols import PROTOCOLS


class EvaluationPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        folder = self.project / 'cases/holdout';folder.mkdir(parents=True)
        (self.project / 'src').mkdir()
        (self.project / 'src/diagnostics.py').write_text('# SYNTHETIC frozen source, never executed\n')
        data = case_data();data['review'] = {'status':'draft'}
        self.case = folder / 'synthetic.json';self.case.write_text(json.dumps(data))
        contract = Contract.load(self.case)
        self.manifest = folder / 'freeze.json'
        self.manifest.write_text(json.dumps({'schema_version':1,'kind':'holdout','cases':[
            {'file':self.case.name,'case_id':data['case_id'],'file_sha256':hashlib.sha256(self.case.read_bytes()).hexdigest(),
             'contract_sha256':contract.sha256,'acceptance_sha256':contract.acceptance_sha256}]}))
        self.frozen = self.root / 'evaluation-freeze.json'
        self.reviews = self.root / 'reviews.json'
        self.wheels = self.root / 'wheels';self.wheels.mkdir()
        freeze(self.project,self.manifest,self.frozen,runner_id='sha256:'+'1'*64,dependency_mode='offline_snapshot',reviews_template=self.reviews,wheelhouse=self.wheels)
        self.records = self.root / 'records'

    def tearDown(self):
        self.temp.cleanup()

    def approve_synthetic(self):
        value=json.loads(self.reviews.read_text())
        for row in value['approvals']:
            row.update(status='approved',reviewed_by='SYNTHETIC HUMAN TEST FIXTURE',approved_at='2026-10-19T00:00:00Z')
        self.reviews.write_text(json.dumps(value))

    def prepare_model_fixture(self, *, batch_budget='0.06'):
        source=self.root/'sources/unit';source.mkdir(parents=True)
        (source/'README.md').write_text('pip install old\n')
        for argv in [['git','init','-q',str(source)],['git','-C',str(source),'add','README.md'],
                     ['git','-C',str(source),'-c','user.name=Synthetic Evaluation','-c','user.email=synthetic@example.invalid','-c','core.hooksPath=/dev/null','commit','-qm','synthetic fixture']]:
            subprocess.run(argv,check=True,capture_output=True)
        sha=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
        data=json.loads(self.case.read_text());data['repo']['commit']=sha;self.case.write_text(json.dumps(data))
        c=Contract.load(self.case);inventory=json.loads(self.manifest.read_text())
        inventory['cases'][0].update(file_sha256=hashlib.sha256(self.case.read_bytes()).hexdigest(),contract_sha256=c.sha256,acceptance_sha256=c.acceptance_sha256)
        self.manifest.write_text(json.dumps(inventory))
        self.frozen=self.root/'model-freeze.json';self.reviews=self.root/'model-reviews.json'
        self.specs={'nano':spec(),'ultra':spec('ultra-test','1','3')}
        freeze(self.project,self.manifest,self.frozen,runner_id='sha256:'+'1'*64,dependency_mode='offline_snapshot',reviews_template=self.reviews,
               wheelhouse=self.wheels,model_specs=self.specs,case_budget_usd='0.02',batch_budget_usd=batch_budget,time_limit_s=60)
        self.approve_synthetic()
        self.planners=[]

    def owner_fixture(self, contract, location, **options):
        answer=[{'category':'documentation','reason':'SYNTHETIC failure'},
                {'explanation':'SYNTHETIC candidate','patch':{'edits':[{'path':'README.md','old':'old','new':'new'}]}}]*2
        transport=SyntheticTransport(answer)
        ledger=ModelLedger(location/'private-ledger.sqlite3',options['run_id'],options['max_usd'])
        planner=RepairPlanner(ModelCaller(transport,ledger,deadline=options['deadline']),self.specs['nano'],self.specs['ultra'],route=PROTOCOLS[options['group']]['route'])
        self.planners.append(planner)
        return planner

    def recorded_ledger_fixture(self):
        # Provider-shaped RECORD FIXTURE ONLY; no transport or network call.
        self.prepare_model_fixture()
        location=self.root/'recorded-ledger-fixture';location.mkdir()
        calls=[]
        for stage,role,cost in [('classify','nano','0.000018'),('diagnose','ultra','0.00025')]:
            model=self.specs[role]
            calls.append({'stage':stage,'run_id':'SYNTHETIC-RECORDED-FIXTURE','data_origin':'nebius_api',
                'dispatch_started_at':'2026-10-19T00:00:00Z','requested_model':model.model_id,'returned_model':model.model_id,
                'price_reference':model.price_reference,'input_per_million':model.input_per_million,'output_per_million':model.output_per_million,
                'cached_input_per_million':None,'calculated_usd':cost,'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}})
        ledger={'run_id':'SYNTHETIC-RECORDED-FIXTURE','max_usd':'0.02','model_calls':2,'calls':calls,
                'held_usd':'0.000268','calculated_usd':'0.000268'}
        binding={'group':'C','ledger_run_id':'SYNTHETIC-RECORDED-FIXTURE','model_calls':2}
        self.write_ledger_fixture(location,binding,ledger)
        return location,binding,ledger,json.loads(self.frozen.read_text())

    def write_ledger_fixture(self,location,binding,ledger):
        path=location/'model-ledger.json';path.write_text(json.dumps(ledger))
        binding['ledger_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()

    def run_infrastructure_fixture(self):
        class MissingRuntime:
            def preflight(self):raise InfrastructureError('SYNTHETIC unavailable runtime')
        return run_baselines(self.project,self.manifest,self.frozen,self.reviews,source_root=self.root/'sources',
                             output_root=self.records,wheelhouse=self.wheels,runner_factory=lambda c:MissingRuntime())

    def test_freeze_does_not_approve_or_expose_holdouts(self):
        self.assertEqual(Contract.load(self.case).data['review']['status'],'draft')
        service=Service(self.project,self.root/'api-state')
        try:self.assertEqual(service.contracts,{})
        finally:service.close()
        with self.assertRaises(InvalidCase):
            freeze(self.project,self.manifest,self.frozen,runner_id='sha256:'+'1'*64,dependency_mode='offline_snapshot')

    def test_draft_review_and_paid_groups_stop_before_any_execution(self):
        with self.assertRaisesRegex(InvalidCase,'human approval'):
            self.run_infrastructure_fixture()
        self.assertFalse(self.records.exists())
        self.approve_synthetic()
        with self.assertRaisesRegex(InvalidCase,'Owner planner'):
            run_baselines(self.project,self.manifest,self.frozen,self.reviews,source_root=self.root,output_root=self.records,group='C')
        self.assertFalse(self.records.exists())

    def test_changed_code_and_case_are_rejected(self):
        src=self.project/'src/diagnostics.py';raw=src.read_bytes();src.write_bytes(raw+b'# changed')
        with self.assertRaisesRegex(InvalidCase,'Code/prompts'):
            load_freeze(self.project,self.manifest,self.frozen,execution=True)
        src.write_bytes(raw)
        self.case.write_bytes(self.case.read_bytes()+b' ')
        with self.assertRaisesRegex(InvalidCase,'bytes changed'):
            load_freeze(self.project,self.manifest,self.frozen,execution=True)

    def test_changed_wheel_bytes_stop_before_execution(self):
        self.approve_synthetic()
        (self.wheels/'synthetic-1-py3-none-any.whl').write_bytes(b'SYNTHETIC wheel bytes, not installed')
        with self.assertRaisesRegex(InvalidCase,'wheelhouse differs'):
            self.run_infrastructure_fixture()
        self.assertFalse(self.records.exists())

    def test_infrastructure_failure_is_kept_and_cost_unknown_not_zero(self):
        self.approve_synthetic();raw=self.case.read_bytes()
        self.assertEqual(self.run_infrastructure_fixture()['executed_cases'],1)
        output=self.root/'results.json'
        export_results(self.project,self.manifest,self.frozen,self.records,output)
        data=json.loads(output.read_text())
        self.assertEqual(data['runs'][0]['result_status'],'infrastructure_error')
        self.assertIsNone(data['runs'][0]['cost_usd'])
        self.assertEqual(data['runs'][0]['model_usd'],0)
        self.assertEqual([r['group'] for r in data['unrun']],['B','C','D'])
        self.assertEqual(self.case.read_bytes(),raw)
        with self.assertRaisesRegex(InvalidCase,'never overwrite'):
            self.run_infrastructure_fixture()
        with self.assertRaisesRegex(InvalidCase,'Results already exist'):
            export_results(self.project,self.manifest,self.frozen,self.records,output)
        export_results(self.project,self.manifest,self.frozen,self.records,output,replace=True)
        self.assertEqual(len(list(self.root.glob('results.previous-*.json'))),1)

    def test_changed_report_and_test_double_origin_cannot_publish(self):
        self.approve_synthetic();self.run_infrastructure_fixture()
        path=next(self.records.glob('*/report.json'))
        raw=path.read_bytes();path.write_bytes(raw+b' ')
        with self.assertRaisesRegex(InvalidCase,'does not match'):
            export_results(self.project,self.manifest,self.frozen,self.records,self.root/'bad.json')
        report=json.loads(raw);report['environment']={'test_double':True}
        path.write_text(json.dumps(report))
        bind=path.parent/'binding.json';body=json.loads(bind.read_text())
        body['report_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();bind.write_text(json.dumps(body))
        with self.assertRaisesRegex(InvalidCase,'does not match'):
            export_results(self.project,self.manifest,self.frozen,self.records,self.root/'bad.json')

    def test_interrupted_slot_is_in_progress_without_fabricated_result(self):
        self.approve_synthetic();self.run_infrastructure_fixture()
        bind=next(self.records.glob('*/binding.json'));body=json.loads(bind.read_text());body['run_state']='running'
        bind.write_text(json.dumps(body))
        output=self.root/'progress.json';export_results(self.project,self.manifest,self.frozen,self.records,output)
        row=json.loads(output.read_text())['runs'][0]
        self.assertEqual(row['run_state'],'running')
        self.assertIsNone(row['result_status'])
        self.assertIsNone(row['duration_s'])

    def test_interleaved_four_group_protocols_remain_engineering_evidence(self):
        self.prepare_model_fixture()
        runners=iter([ScriptedRunner([False]),ScriptedRunner([False,False]),
                      ScriptedRunner([False,False,True,True]),ScriptedRunner([False,False,True,True])])
        result=run_baselines(self.project,self.manifest,self.frozen,self.reviews,source_root=self.root/'sources',output_root=self.records,
            wheelhouse=self.wheels,groups=['A','B','C','D'],runner_factory=lambda c:next(runners),repairer_factory=self.owner_fixture)
        self.assertEqual(result['executed_slots'],4)
        self.assertEqual(result['model_calls'],0)  # All responses were engineering doubles.
        reports={p.parent.name[-1]:json.loads(p.read_text()) for p in self.records.glob('*/report.json')}
        self.assertEqual([reports[g]['max_patch_rounds'] for g in 'ABCD'],[0,1,2,2])
        self.assertEqual([reports[g]['status'] for g in 'ABCD'],['unresolved','unresolved','verified_fix','verified_fix'])
        self.assertEqual([len(p.caller.transport.payloads) for p in self.planners],[2,4,4])
        self.assertTrue(all(p['model']=='ultra-test' for p in self.planners[-1].caller.transport.payloads))
        with self.assertRaisesRegex(InvalidCase,'origin'):
            export_results(self.project,self.manifest,self.frozen,self.records,self.root/'not-real-results.json')
        self.assertFalse((self.root/'not-real-results.json').exists())

    def test_batch_budget_stops_before_any_group_runs(self):
        self.prepare_model_fixture(batch_budget='0.02')
        with self.assertRaisesRegex(InvalidCase,'batch cannot reserve'):
            run_baselines(self.project,self.manifest,self.frozen,self.reviews,source_root=self.root/'sources',output_root=self.records,
                wheelhouse=self.wheels,groups=['B','C'],repairer_factory=self.owner_fixture)
        self.assertFalse(self.records.exists())
        self.assertEqual(self.planners,[])

    def test_unknown_charge_stops_the_whole_batch_before_next_group(self):
        self.prepare_model_fixture()
        def unknown(*args,**options):
            planner=self.owner_fixture(*args,**options)
            planner.caller.transport.transform=lambda r:{k:v for k,v in r.items() if k!='usage'}
            return planner
        with self.assertRaisesRegex(InvalidCase,'batch stopped'):
            run_baselines(self.project,self.manifest,self.frozen,self.reviews,source_root=self.root/'sources',output_root=self.records,
                wheelhouse=self.wheels,groups=['C','D'],runner_factory=lambda c:ScriptedRunner([False]),repairer_factory=unknown)
        self.assertEqual(len(self.planners),1)
        self.assertEqual(len(self.planners[0].caller.transport.payloads),1)
        self.assertEqual(json.loads((self.records/'batch-stop.json').read_text())['reason'],'billing_unresolved')
        self.assertEqual(len(list(self.records.glob('*/binding.json'))),1)

    def test_owner_route_mismatch_is_rejected_before_model_dispatch(self):
        self.prepare_model_fixture()
        def wrong(*args,**options):
            planner=self.owner_fixture(*args,**options);planner.route='all_ultra';return planner
        with self.assertRaisesRegex(InvalidCase,'route differs'):
            run_baselines(self.project,self.manifest,self.frozen,self.reviews,source_root=self.root/'sources',output_root=self.records,
                wheelhouse=self.wheels,group='B',runner_factory=lambda c:ScriptedRunner([False]),repairer_factory=wrong)
        self.assertEqual(self.planners[0].caller.transport.payloads,[])

    def test_unfrozen_research_cannot_be_injected_into_a_formal_comparison(self):
        self.prepare_model_fixture()
        def injected(contract, location, **options):
            planner = self.owner_fixture(contract, location, **options)
            planner.researcher = object()
            return planner
        with self.assertRaisesRegex(InvalidCase, 'Web research is not enabled'):
            run_baselines(self.project,self.manifest,self.frozen,self.reviews,source_root=self.root/'sources',output_root=self.records,
                wheelhouse=self.wheels,group='C',runner_factory=lambda c:ScriptedRunner([False]),repairer_factory=injected)
        self.assertEqual(self.planners[0].caller.transport.payloads,[])

    def test_same_case_group_from_two_roots_cannot_be_silently_replaced(self):
        self.approve_synthetic();self.run_infrastructure_fixture()
        import shutil
        other=self.root/'duplicate-records';shutil.copytree(self.records,other)
        with self.assertRaisesRegex(InvalidCase,'Duplicate'):
            export_results(self.project,self.manifest,self.frozen,[self.records,other],self.root/'not-merged.json')

    def test_relabelled_group_cannot_hide_different_round_budget(self):
        self.approve_synthetic();self.run_infrastructure_fixture()
        path=next(self.records.glob('*/binding.json'));body=json.loads(path.read_text());body['group']='C'
        path.write_text(json.dumps(body))
        with self.assertRaisesRegex(InvalidCase,'relabelled'):
            export_results(self.project,self.manifest,self.frozen,self.records,self.root/'not-relabeled.json')

    def test_recorded_ledger_fixture_costs_are_checked_against_frozen_rates(self):
        location,binding,ledger,frozen=self.recorded_ledger_fixture()
        self.assertEqual(checked_ledger(location,binding,frozen)['calculated_usd'],'0.000268')
        ledger['calls'][0]['calculated_usd']='0.000001'
        self.write_ledger_fixture(location,binding,ledger)
        with self.assertRaisesRegex(InvalidCase,'cannot be reproduced'):
            checked_ledger(location,binding,frozen)

    def test_unknown_model_charge_is_retained_as_unknown(self):
        location,binding,ledger,frozen=self.recorded_ledger_fixture()
        ledger['calls'][1]['calculated_usd']=None;ledger['calculated_usd']=None;ledger['held_usd']='0.00565'
        self.write_ledger_fixture(location,binding,ledger)
        result=checked_ledger(location,binding,frozen)
        self.assertIsNone(result['calculated_usd'])
        self.assertEqual(result['model_calls'],2)

    def test_all_ultra_group_rejects_a_nano_classification_ledger(self):
        location,binding,ledger,frozen=self.recorded_ledger_fixture()
        binding['group']='D'
        with self.assertRaisesRegex(InvalidCase,'role'):
            checked_ledger(location,binding,frozen)


if __name__=='__main__':unittest.main()
