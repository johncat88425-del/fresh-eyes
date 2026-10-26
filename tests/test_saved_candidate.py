"""Synthetic saved-candidate provenance fixtures, with no provider or container execution."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from fresh_eyes.contracts import Contract,digest,InvalidCase
from test_core import case_data

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('saved_candidate',ROOT/'scripts/validate-saved-model-candidate.py')
saved=importlib.util.module_from_spec(spec);spec.loader.exec_module(saved)


class SavedCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'cases').mkdir();self.trial=self.root/'trial';self.trial.mkdir()
        data=case_data('a'*40);data['review']={'status':'approved','reviewed_by':'synthetic-reviewer',
            'approved_at':'2026-10-01T12:00:00+00:00','scope':'team_acceptance_review'}
        contract=Contract.from_dict(data);data['review']['acceptance_sha256']=contract.acceptance_sha256
        contract=Contract.from_dict(data);(self.root/'cases/case.json').write_text(json.dumps(data))
        candidate={'edits':[{'path':'README.md','old':'old','new':'new'}]}
        plan={'review':data['review'],'execution_contract_sha256':contract.sha256};plan['payload_sha256']=digest(plan)
        ledger={'calls':[{'call_id':'synthetic-'+str(i),'data_origin':'nebius_api',
            'dispatch_started_at':'2026-10-01T12:01:00+00:00','status':'completed'} for i in range(2)],
            'model_calls':2,'calculated_usd':'.01'}
        report={'run_id':'a'*32,'case_id':data['case_id'],'repair_author':'model','candidate_patch':{
            'status':'accepted_for_validation','patch':candidate},'repo':data['repo'],
            'contract_sha256':contract.sha256,'acceptance_sha256':contract.acceptance_sha256,
            'model_ledger':ledger,'patch':{'sha256':'b'*64}}
        self.write('trial-plan.json',plan);self.write('model-ledger.json',ledger);self.write('report.json',report)
        self.binding={'scope':'development','evaluation_eligible':False,'run_id':report['run_id'],
            'report_sha256':self.sha('report.json'),'ledger_sha256':self.sha('model-ledger.json'),
            'plan_sha256':plan['payload_sha256']};self.write('trial-binding.json',self.binding)
    def write(self,name,value):(self.trial/name).write_text(json.dumps(value))
    def sha(self,name):return hashlib.sha256((self.trial/name).read_bytes()).hexdigest()

    def test_preview_returns_exact_candidate_and_separate_zero_call_provenance(self):
        contract,candidate,proof,report=saved.checked_candidate(self.root,self.trial)
        self.assertEqual(candidate,report['candidate_patch']['patch'])
        self.assertEqual(proof['candidate_sha256'],digest(candidate))
        self.assertEqual(proof['new_model_calls'],0)
        self.assertEqual(proof['source_model_calls'],2)
        self.assertFalse(proof['evaluation_eligible'])

    def test_changed_report_or_ledger_is_rejected_before_execution(self):
        for name in ('report.json','model-ledger.json'):
            path=self.trial/name;original=path.read_bytes();path.write_bytes(original+b' ')
            with self.subTest(name=name),self.assertRaises(InvalidCase):saved.checked_candidate(self.root,self.trial)
            path.write_bytes(original)

    def test_synthetic_call_cannot_become_model_provenance_by_rebinding_hashes(self):
        ledger=json.loads((self.trial/'model-ledger.json').read_text());ledger['calls'][0]['data_origin']='engineering_test'
        report=json.loads((self.trial/'report.json').read_text());report['model_ledger']=ledger
        self.write('report.json',report);self.write('model-ledger.json',ledger)
        self.binding.update(report_sha256=self.sha('report.json'),ledger_sha256=self.sha('model-ledger.json'))
        self.write('trial-binding.json',self.binding)
        with self.assertRaises(InvalidCase):saved.checked_candidate(self.root,self.trial)

    def test_changed_acceptance_is_not_inherited_from_the_old_trial(self):
        path=self.root/'cases/case.json';d=json.loads(path.read_text());d['fixtures'][0]['expected']='changed'
        path.write_text(json.dumps(d))
        with self.assertRaises(InvalidCase):saved.checked_candidate(self.root,self.trial)
