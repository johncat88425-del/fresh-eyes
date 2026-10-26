"""Model-written fixture suggestions stay unapproved; engineering responses only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from fresh_eyes.acceptance_drafts import AcceptanceDraftPlanner, validate_draft
from fresh_eyes.contracts import Contract, InvalidCase
from fresh_eyes.models import ModelCaller, ModelError, ModelLedger
from fresh_eyes.startup import inspect_repo
import test_github_startup as source_tests
import test_models as model_tests

README='Unit returns the input text.\n```sh\npip install .\nunit --help\nunit input.txt\n```\n'
ENV={'platform':'linux/arm64','image':'python:3.12-slim'}


def answer():
    return {'explanation':'Synthetic fixture draft, not a real model outcome.', 'draft':{
        'goal':'Return the input text', 'readme_source':'README.md:L1',
        'workflow':{'argv':['unit','{input}'],'source':'README.md:L5','output':'stdout'},
        'documented_argv':['unit','input.txt'],
        'fixtures':[{'name':'first.txt','input':'first','validator':'text','expected':'first'},
                    {'name':'second.txt','input':'second','validator':'text','expected':'second'}]}}


class AcceptanceDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        raw=source_tests.archive({'README.md':README.encode(),'pyproject.toml':b'[project]\n'})
        self.inspection=inspect_repo('https://github.com/unit/project',None,self.root/'source',reader=source_tests.Reader(raw))
    def tearDown(self):
        self.temp.cleanup()
    def validate(self,value):
        return validate_draft(json.dumps(value),self.inspection,README,ENV)

    def test_valid_model_suggestions_cannot_approve_or_execute_themselves(self):
        draft=self.validate(answer())
        self.assertEqual(draft['status'],'draft')
        self.assertFalse(draft['human_approval_granted'])
        self.assertFalse(draft['executed'])
        self.assertEqual(draft['contract']['review'],{'status':'draft'})
        with self.assertRaises(InvalidCase):
            Contract.from_dict(draft['contract']).check_review()

    def test_invented_command_citation_shell_and_replaced_option_are_rejected(self):
        for mutate in (lambda d:d['workflow'].update(argv=['other','{input}']),
                       lambda d:d['workflow'].update(source='README.md:L999'),
                       lambda d:d.update(documented_argv=['unit','invented.txt']),
                       lambda d:d['workflow'].update(argv=['unit','--help','{input}'])):
            value=answer(); mutate(value['draft'])
            with self.assertRaises((ModelError,InvalidCase)):
                self.validate(value)

    def test_self_approval_and_command_fields_cannot_be_smuggled_into_fixtures(self):
        value=answer(); value['draft']['review']={'status':'approved'}
        with self.assertRaises(ModelError): self.validate(value)
        value=answer(); value['draft']['fixtures'][0]['argv']=['sh','-c','bad']
        with self.assertRaises(ModelError): self.validate(value)

    def test_null_draft_is_a_request_for_input_not_a_fabricated_contract(self):
        result=self.validate({'explanation':'Not enough documentation.','draft':None})
        self.assertEqual(result['status'],'needs_input')
        self.assertIsNone(result['contract'])

    def test_budgeted_planner_uses_one_existing_caller_and_never_registers_a_case(self):
        transport=model_tests.SyntheticTransport([answer()])
        ledger=ModelLedger(self.root/'ledger.sqlite3','synthetic-acceptance','1')
        caller=ModelCaller(transport,ledger)
        planner=AcceptanceDraftPlanner(caller,model_tests.spec())
        result=planner.propose(self.inspection,self.root/'source',environment=ENV)
        self.assertEqual(result['data_origin'],'engineering_test')
        self.assertEqual(result['status'],'draft')
        self.assertEqual(len(transport.payloads),1)
        self.assertFalse((self.root/'cases').exists())
        self.assertIsNone(result['ledger']['billed_usd'])
        with self.assertRaises(ModelError):
            planner.propose(self.inspection,self.root/'source',environment=ENV)
        self.assertEqual(len(transport.payloads),1)

    def test_changed_or_symlinked_readme_stops_before_dispatch(self):
        transport=model_tests.SyntheticTransport([answer()])
        caller=ModelCaller(transport,ModelLedger(self.root/'ledger.sqlite3','synthetic','1'))
        planner=AcceptanceDraftPlanner(caller,model_tests.spec())
        path=self.root/'source/README.md'
        path.write_text('changed')
        with self.assertRaises(ModelError):
            planner.propose(self.inspection,self.root/'source',environment=ENV)
        path.unlink(); path.symlink_to(self.root/'outside')
        with self.assertRaises(ModelError):
            planner.propose(self.inspection,self.root/'source',environment=ENV)
        self.assertEqual(transport.payloads,[])

    def test_missing_startup_plan_never_uses_the_caller(self):
        inspection=copy.deepcopy(self.inspection); inspection['plan']['status']='needs_input'
        result=AcceptanceDraftPlanner(None,None).propose(inspection,self.root/'source',environment=ENV)
        self.assertIsNone(result['contract'])
        self.assertEqual(result['model_calls'],0)


if __name__=='__main__': unittest.main()
