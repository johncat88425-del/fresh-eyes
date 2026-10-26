"""Human decision transfer uses synthetic names/data only; no real approval or execution."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from fresh_eyes.contracts import Contract, InvalidCase
from fresh_eyes.review_decisions import prepare, SUBJECT, merge_previous
from test_core import case_data


class ReviewDecisionIntake(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'cases/holdout').mkdir(parents=True)
        self.case=self.root/'cases/holdout/unit.json'
        self.case.write_text(json.dumps(case_data()))
        self.contract=Contract.load(self.case)
        self.identifier=self.contract.data['case_id']
        row={key:self.contract.data[key] for key in SUBJECT}
        row.update(case_id=self.identifier,file='cases/holdout/unit.json',group='holdout',acceptance_sha256=self.contract.acceptance_sha256)
        self.page={'cases':[row]};self.page_path=self.root/'page.json'
        self.record={'reviewed_by':'SYNTHETIC REVIEWER','account_id':'synthetic-private-id','approvals':[{
            'case_id':self.identifier,'acceptance_sha256':self.contract.acceptance_sha256,'status':'approved',
            'reviewed_by':'SYNTHETIC REVIEWER','approved_at':'2026-10-22T12:00:00-04:00'}],'not_accepted':[]}
        self.record_path=self.root/'decisions.json'
        self.frozen={'payload_sha256':'f'*64,'cases':[{'case_id':self.identifier,'acceptance_sha256':self.contract.acceptance_sha256}]}
        self.boundary={'payload_sha256':'b'*64,'cases':[]}

    def process(self):
        self.page_path.write_text(json.dumps(self.page));self.record_path.write_text(json.dumps(self.record))
        return prepare(self.root,self.record_path,self.page_path,self.frozen,self.boundary)

    def test_preserves_original_reviewer_time_and_never_exports_account_id(self):
        result=self.process()
        row=result['holdout-reviews.json']['approvals'][0]
        self.assertEqual(row['approved_at'],'2026-10-22T12:00:00-04:00')
        self.assertEqual(row['reviewed_by'],'SYNTHETIC REVIEWER')
        self.assertEqual(result['holdout-reviews.json']['freeze_sha256'],'f'*64)
        self.assertNotIn('synthetic-private-id',json.dumps(result))
        self.assertTrue(result['receipt.json']['holdout_execution_ready'])
        self.assertFalse(result['receipt.json']['identity_verified'])

    def test_rejection_is_retained_and_blocks_the_batch(self):
        row=self.record['approvals'].pop()
        self.record['not_accepted']=[{'case_id':row['case_id'],'acceptance_sha256':row['acceptance_sha256'],
            'reason':'SYNTHETIC must revise the contract','decided_at':row['approved_at']}]
        result=self.process()
        self.assertEqual(result['holdout-reviews.json']['approvals'][0]['status'],'rejected')
        self.assertFalse(result['receipt.json']['holdout_execution_ready'])

    def test_changed_setup_is_refused_even_when_acceptance_hash_did_not_change(self):
        data=self.contract.data;data['setup'][0]['argv']=['pip','install','different']
        self.case.write_text(json.dumps(data))
        self.assertEqual(Contract.load(self.case).acceptance_sha256,self.contract.acceptance_sha256)
        with self.assertRaisesRegex(InvalidCase,'instructions or fixtures changed'):self.process()

    def test_changed_decision_hash_duplicate_and_missing_rejection_reason_are_refused(self):
        original=deepcopy(self.record)
        for change in ('hash','duplicate','reason'):
            self.record=deepcopy(original)
            if change=='hash':self.record['approvals'][0]['acceptance_sha256']='0'*64
            elif change=='duplicate':self.record['approvals'].append(deepcopy(self.record['approvals'][0]))
            else:
                row=self.record['approvals'].pop()
                self.record['not_accepted']=[{'case_id':self.identifier,'acceptance_sha256':row['acceptance_sha256'],
                    'decided_at':row['approved_at'],'reason':''}]
            with self.subTest(change=change),self.assertRaises(InvalidCase):self.process()

    def test_missing_decision_stays_draft(self):
        self.record['approvals']=[]
        result=self.process()
        self.assertEqual(result['holdout-reviews.json']['approvals'][0]['status'],'draft')
        self.assertFalse(result['receipt.json']['holdout_execution_ready'])

    def test_ambiguous_timestamp_is_not_reinterpreted(self):
        self.record['approvals'][0]['approved_at']='2026-10-22T12:00:00'
        with self.assertRaisesRegex(ValueError,'timezone'):self.process()

    def test_later_single_case_review_does_not_erase_earlier_unchanged_acceptance(self):
        earlier=self.process()
        self.record['approvals']=[]
        later=self.process()
        result=merge_previous(later,earlier)
        self.assertEqual(result['holdout-reviews.json']['approvals'][0]['status'],'approved')
        self.assertTrue(result['receipt.json']['holdout_execution_ready'])
        self.assertEqual(result['receipt.json']['carried_approvals'],[self.identifier])
        self.assertEqual(later['holdout-reviews.json']['approvals'][0]['status'],'draft')

    def test_previous_decisions_from_another_freeze_cannot_be_silently_carried(self):
        earlier=self.process();later=deepcopy(earlier)
        earlier['holdout-reviews.json']['freeze_sha256']='x'*64
        with self.assertRaisesRegex(InvalidCase,'another freeze'):merge_previous(later,earlier)
